"""Universal LLM client using LiteLLM for multi-provider support.

Supports:
- OpenAI (gpt-4o, gpt-4.1, gpt-5, etc.)
- Azure OpenAI (custom deployments)
- Anthropic Claude (claude-3.5-sonnet, claude-opus-4.5, etc.)
- Google Gemini (gemini-1.5-pro, gemini-3-flash, etc.)
- 100+ other models via LiteLLM

No legacy fallbacks - requires LiteLLM to be installed.
"""

import json
import logging
import os
import re
from typing import Dict, Any, Optional, List

import litellm
from litellm import completion, completion_cost

from ..utils.model_pricing import get_pricing
from ..exceptions import ExtractionError
from .llm_factory import DEFAULT_MODEL, detect_provider
from .text_processor import find_data_arrays

logger = logging.getLogger(__name__)


# Substring markers that identify "reasoning" models — those that reject the
# temperature / response_format params. Provider-neutral defaults; a deployment
# can extend this via the top-level ``reasoning_models`` run.yaml list (unioned
# with these defaults) so a new reasoning model works without a code change.
DEFAULT_REASONING_MODEL_MARKERS: tuple[str, ...] = (
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "thinking",
)


class LLMClient:
    """
    Universal LLM client for structured data extraction.

    Uses LiteLLM for unified interface across all providers.
    Automatically tracks costs and handles provider-specific parameters.
    """

    CONTEXT_RESPONSE_RESERVE_TOKENS = 4096

    DEFAULT_TIMEOUT = 120  # seconds; overridable via LLM_TIMEOUT env var

    def __init__(
        self,
        api_key: str = None,
        model: str = DEFAULT_MODEL,
        provider: str = None,
        azure_endpoint: str = None,
        azure_api_version: str = None,
        context_windows: Optional[Dict[str, int]] = None,
        base_url: str = None,
        timeout: int = None,
        reasoning_models: Optional[List[str]] = None,
    ):
        """
        Initialize LLM client.

        Parameters
        ----------
        api_key : str
            API key for the provider (will auto-set environment variables)
        model : str
            Model name (e.g., "gpt-4o-mini", "claude-3.5-sonnet", "gemini-1.5-pro")
        provider : str
            Explicit provider ("openai", "azure", "anthropic", "gemini")
            If None, auto-detects from model name
        azure_endpoint : str
            Azure OpenAI endpoint (for Azure provider)
        azure_api_version : str
            Azure API version (for Azure provider)
        context_windows : Optional[Dict[str, int]]
            Optional {model-name -> max prompt tokens} map used to
            fail fast before an over-budget request. Domain/deployment
            names are NOT hardcoded — supply this from config
            (``model_context_windows`` in run.yaml) for deployments whose
            context window LiteLLM cannot infer. Unset models are not
            guarded.
        base_url : str
            Optional endpoint override. When set, all calls are routed
            through this OpenAI-compatible URL regardless of model name.
            Works with any proxy (LiteLLM, OpenRouter, vLLM, etc.).
        timeout : int
            Request timeout in seconds. Defaults to LLM_TIMEOUT env var,
            then DEFAULT_TIMEOUT (120 s). Set higher for very large docs
            or slow proxy routes; set lower for fast fail-fast behaviour.
        """
        if not model:
            raise ValueError(
                "No model name configured for this stage. "
                "Add a 'models:' block at the top of your run config "
                "(e.g. 'primary: your-deployment') and set "
                "'model: primary' under the relevant stage "
                "(extraction:, compilation.synthesis:, etc.), "
                "or set LLM_MODEL / AZURE_OPENAI_MODEL / OPENAI_MODEL in your .env file."
            )
        self.raw_model = model  # Keep original for cost tracking
        self.base_url = base_url
        self._api_key = api_key  # Stored for direct-pass in endpoint-override mode
        self.context_windows = dict(context_windows or {})
        # Reasoning-model markers: built-in defaults unioned with any
        # config-supplied ``reasoning_models`` (extra markers, lowercased).
        extra_markers = [
            m.lower().strip() for m in (reasoning_models or []) if m.strip()
        ]
        self.reasoning_model_markers: tuple[str, ...] = (
            *DEFAULT_REASONING_MODEL_MARKERS,
            *(
                m
                for m in extra_markers
                if m not in DEFAULT_REASONING_MODEL_MARKERS
            ),
        )
        self.timeout = timeout if timeout is not None else int(
            os.environ.get("LLM_TIMEOUT", self.DEFAULT_TIMEOUT)
        )
        self.provider = provider or detect_provider(model)

        # Format model name for LiteLLM
        self.model = self._format_model_for_litellm(model, self.provider)

        # Configure LiteLLM
        litellm.drop_params = True  # Drop unsupported params
        litellm.suppress_debug_info = True

        # Set up environment variables for LiteLLM
        self._configure_environment(api_key, azure_endpoint, azure_api_version)

        # Load pricing database
        self.pricing_db = get_pricing()

        endpoint_info = f", base_url={self.base_url}" if self.base_url else ""
        logger.info(
            f"Initialized LLM client: provider={self.provider}, model={self.model}{endpoint_info}"
        )

    def _format_model_for_litellm(self, model: str, provider: str) -> str:
        """
        Format model name for LiteLLM.

        In endpoint-override mode (base_url set): model names are prefixed with
        "openai/" so LiteLLM uses /chat/completions instead of a provider-specific
        path. LiteLLM strips the prefix before forwarding, so the proxy receives
        the bare model name the user configured.

        In direct-provider mode:

        - Azure: "azure/deployment-name"
        - Anthropic: "claude-3.5-sonnet" (no prefix needed)
        - Gemini: "gemini/gemini-1.5-pro" (optional)
        """
        if self.base_url:
            # Proxy mode: force OpenAI-compatible routing (/chat/completions).
            # LiteLLM sniffs provider from the model name: "claude-*" → Anthropic
            # handler → /v1/messages appended to base_url (doubles any /v1 already
            # in the URL). Prefixing with "openai/" forces the OpenAI handler.
            # LiteLLM strips the outer "openai/" before forwarding, so the proxy
            # receives the model name exactly as configured:
            #   "claude-haiku-4-5"          → proxy gets "claude-haiku-4-5"
            #   "anthropic/claude-3-sonnet" → proxy gets "anthropic/claude-3-sonnet"
            #   "openai/gpt-4o"             → unchanged (already prefixed)
            if not model.startswith("openai/"):
                return f"openai/{model}"
            return model
        if provider == "azure":
            if not model.startswith("azure/"):
                return f"azure/{model}"
            return model
        elif provider == "gemini":
            if not model.startswith("gemini/") and not model.startswith(
                "google/"
            ):
                return f"gemini/{model}"
            return model
        else:
            return model

    def _configure_environment(
        self,
        api_key: Optional[str],
        azure_endpoint: Optional[str],
        azure_api_version: Optional[str],
    ):
        """Configure environment variables for LiteLLM."""
        if self.base_url:
            # Endpoint-override mode: set the proxy URL and key as the OpenAI
            # environment so LiteLLM routes all calls through this endpoint.
            os.environ["OPENAI_BASE_URL"] = self.base_url
            if api_key:
                os.environ["OPENAI_API_KEY"] = api_key
            return

        if not api_key:
            return

        # Direct-provider mode: set provider-specific environment variables.
        if self.provider == "azure":
            os.environ["AZURE_API_KEY"] = api_key
            if azure_endpoint:
                os.environ["AZURE_API_BASE"] = azure_endpoint
            if azure_api_version:
                os.environ["AZURE_API_VERSION"] = azure_api_version
            else:
                os.environ["AZURE_API_VERSION"] = "2024-02-15-preview"
        elif self.provider == "anthropic":
            os.environ["ANTHROPIC_API_KEY"] = api_key
        elif self.provider == "gemini":
            os.environ["GEMINI_API_KEY"] = api_key
        else:
            os.environ["OPENAI_API_KEY"] = api_key

    def extract(
        self,
        text: str,
        schema: Dict[str, Any],
        system_prompt: str = None,
        user_prompt: str = None,
        suppress_errors: bool = False,
    ) -> Dict[str, Any]:
        """
        Extract structured data using LLM.

        Parameters
        ----------
        text : str
            Document text to extract from
        schema : Dict[str, Any]
            JSON schema for extraction
        system_prompt : str
            Optional custom system prompt
        user_prompt : str
            Optional custom user prompt (overrides default)

        Returns
        -------
        Dict[str, Any]
            dict with 'data' (extracted data) and 'cost' (API cost in USD)
        """
        # Use default prompts if not provided
        if system_prompt is None:
            system_prompt = (
                "You are an expert at extracting structured data from documents. "
                "Extract exactly as shown in source document."
            )

        if user_prompt is None:
            user_prompt = self._build_extraction_prompt(text, schema)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        self._validate_context_budget(messages)

        # Check if this is a reasoning model (doesn't support temperature/response_format)
        is_reasoning_model = any(
            x in self.model.lower() for x in self.reasoning_model_markers
        )

        # Prepare API call parameters
        api_params = {
            "model": self.model,
            "messages": messages,
            "timeout": self.timeout,
        }

        # Endpoint-override mode: route through the proxy's base_url.
        # The model name already carries an "openai/" prefix (set by
        # _format_model_for_litellm) so LiteLLM uses /chat/completions —
        # no custom_llm_provider kwarg needed or wanted.
        if self.base_url:
            api_params["base_url"] = self.base_url
            if self._api_key:
                api_params["api_key"] = self._api_key

        if not is_reasoning_model:
            api_params["temperature"] = 0
            # response_format is a standard OpenAI-compatible parameter.
            # Always send it for non-reasoning models — LiteLLM proxies translate
            # it to each backend's native JSON mode, and litellm.drop_params=True
            # silently drops it for backends that don't support it.
            # Diagnostic confirmed: proxies forward this correctly.
            api_params["response_format"] = {"type": "json_object"}
            # Inject a JSON instruction when the messages don't already mention JSON.
            # Required by Azure's json_object mode; also reinforces intent for
            # models that add markdown fences or preamble despite response_format.
            all_content = " ".join(m["content"] for m in messages)
            if "json" not in all_content.lower():
                messages[-1]["content"] += (
                    "\n\nRespond with ONLY a raw JSON object. "
                    "No markdown, no code fences, no explanation."
                )

        # ── API call ─────────────────────────────────────────────────────────
        # Split from content parsing so the two failure modes produce distinct
        # error messages: proxy/network errors vs model returned non-JSON text.
        try:
            response = completion(**api_params)
        except json.JSONDecodeError as e:
            # LiteLLM failed to parse the HTTP response body from the proxy.
            # Common causes: empty body, SSE stream when non-streaming expected,
            # or an HTML/text error page from a gateway.
            hint = (
                f" Verify '{self.raw_model}' is configured in the proxy at {self.base_url!r}."
                if self.base_url
                else ""
            )
            raise ExtractionError(
                f"Proxy returned a non-JSON response body for model={self.raw_model!r}.{hint}"
                f" Raw parse error: {e}"
            ) from e
        except Exception as e:
            # Some routed/provider-backed models reject `temperature` entirely.
            # Retry once without temperature when the provider explicitly says the
            # parameter is deprecated/unsupported.
            if self._should_retry_without_temperature(e, api_params):
                retry_params = dict(api_params)
                retry_params.pop("temperature", None)
                try:
                    response = completion(**retry_params)
                except Exception as retry_error:
                    logger.exception(
                        f"LLM extraction failed after retry without temperature "
                        f"(provider={self.provider}, model={self.model})"
                    )
                    raise ExtractionError(str(retry_error)) from retry_error
            else:
                logger.exception(
                    f"LLM extraction failed (provider={self.provider}, model={self.model})"
                )
                raise ExtractionError(str(e)) from e

        # ── Response content ─────────────────────────────────────────────────
        raw_content = (
            response.choices[0].message.content if response.choices else None
        )
        content = self._clean_json_content(raw_content)
        if not content:
            (logger.debug if suppress_errors else logger.error)(
                f"Empty response from API (model={self.model}): raw={repr(raw_content)}"
            )
            raise ExtractionError(
                f"Empty response from provider={self.provider}, model={self.model}"
            )

        # ── JSON parsing ──────────────────────────────────────────────────────
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            # "Extra data" means valid JSON followed by trailing prose/content.
            # raw_decode() parses the first complete JSON object and stops,
            # making it robust to models that append explanatory text after JSON.
            data = None
            if "Extra data" in str(e):
                try:
                    data, _ = json.JSONDecoder().raw_decode(content)
                    logger.debug(
                        f"Recovered from trailing content after JSON "
                        f"(model={self.model}, extra_at={e.pos})"
                    )
                except json.JSONDecodeError:
                    pass
            if data is None:
                (logger.debug if suppress_errors else logger.error)(f"Model returned non-JSON content: {e}")
                logger.debug(f"Response content (after cleaning): {repr(content[:500])}")
                logger.debug(f"Raw content: {repr((raw_content or '')[:500])}")
                raise ExtractionError(
                    f"Model returned non-JSON content "
                    f"(provider={self.provider}, model={self.model}): {e}"
                ) from e

        # ── Cost tracking ─────────────────────────────────────────────────────
        try:
            cost = completion_cost(completion_response=response)
        except Exception as e:
            logger.debug(f"LiteLLM cost calculation failed, using pricing DB: {e}")
            cost = self.pricing_db.get_cost(
                self.raw_model,
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
            )

        total_items = sum(len(value) for _, value in find_data_arrays(data))

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None)
        output_tokens = getattr(usage, "completion_tokens", None)

        logger.info(
            f"✓ Extracted {total_items} items "
            f"(tokens: {input_tokens}+{output_tokens}, cost: ${cost:.4f})"
        )

        return {
            "data": data,
            "cost": cost,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    @staticmethod
    def _clean_json_content(raw: str | None) -> str:
        """Strip markdown fences and whitespace from a model response.

        Some models/proxies wrap JSON in ```json ... ``` code fences even when
        asked not to (notably Claude via proxies that don't enforce JSON mode
        natively). This normalises the content before json.loads so those
        responses are parsed successfully.
        """
        text = (raw or "").strip()
        if not text:
            return text
        # Strip ```json ... ``` or ``` ... ``` fences
        cleaned = re.sub(
            r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
            r"\1",
            text,
            flags=re.DOTALL,
        ).strip()
        return cleaned

    @staticmethod
    def _should_retry_without_temperature(
        error: Exception, api_params: Dict[str, Any]
    ) -> bool:
        """Return True when provider rejected `temperature` and retry is safe."""
        if "temperature" not in api_params:
            return False
        message = str(error).lower()
        if "temperature" not in message:
            return False
        return any(
            token in message
            for token in ("deprecated", "unsupported", "invalid_request_error")
        )

    def _get_context_window_tokens(self) -> Optional[int]:
        """Return the configured context window (prompt tokens) for this model.

        Sourced from the caller-supplied ``context_windows`` map only (populated
        from ``model_context_windows`` in run.yaml). No model names are
        hardcoded; models absent from the map are simply not guarded.
        """
        for candidate in (self.model, self.raw_model):
            if candidate in self.context_windows:
                return self.context_windows[candidate]
        return None

    def _estimate_message_tokens(self, messages: List[Dict[str, str]]) -> int:
        """Estimate prompt token usage conservatively from message content."""
        content_tokens = sum(
            (len(message.get("content", "")) + 3) // 4 for message in messages
        )
        per_message_overhead = 16 * len(messages)
        return content_tokens + per_message_overhead

    def _validate_context_budget(self, messages: List[Dict[str, str]]) -> None:
        """Fail fast when the estimated request exceeds a known model context window."""
        context_window = self._get_context_window_tokens()
        if context_window is None:
            return

        estimated_prompt_tokens = self._estimate_message_tokens(messages)
        estimated_total_tokens = (
            estimated_prompt_tokens + self.CONTEXT_RESPONSE_RESERVE_TOKENS
        )
        if estimated_total_tokens <= context_window:
            return

        raise ExtractionError(
            "context_window_exceeded: estimated request size "
            f"{estimated_prompt_tokens} prompt tokens + {self.CONTEXT_RESPONSE_RESERVE_TOKENS} reserved output tokens "
            f"exceeds model context window {context_window} for provider={self.provider}, model={self.model}. "
            "Reduce max_context_chars or use page ranges/chunking for large documents."
        )

    def _build_extraction_prompt(
        self, text: str, schema: Dict[str, Any]
    ) -> str:
        """Build the extraction prompt with schema and document text."""
        prompt = f"""Extract ALL data from this document into valid JSON matching the schema below.

══════════════════════════════════════════════════════════════════════════════
CORE PRINCIPLES
══════════════════════════════════════════════════════════════════════════════

1. EXTRACT EXACTLY AS WRITTEN - Do not normalize, convert units, paraphrase, or infer
2. PRESERVE DOCUMENT STRUCTURE - Extract requirements as organized in the document
3. SEARCH ENTIRE DOCUMENT - Information may appear in tables, narrative sections, appendices
4. VERIFY COMPLETENESS - Review all sections before returning
5. FOLLOW SCHEMA GUIDANCE - Pay attention to schema descriptions and exclusion rules

EXTRACTION STRATEGY:
1. Read schema carefully - defines what to extract and exclude
2. Scan document structure - identify relevant sections
3. Extract systematically - work through document section by section
4. Cross-reference - check for related information in different locations
5. Validate completeness - ensure nothing was missed

══════════════════════════════════════════════════════════════════════════════
JSON SCHEMA
══════════════════════════════════════════════════════════════════════════════

{json.dumps(schema, indent=2)}

══════════════════════════════════════════════════════════════════════════════
DOCUMENT TEXT
══════════════════════════════════════════════════════════════════════════════

{text}

══════════════════════════════════════════════════════════════════════════════
OUTPUT INSTRUCTIONS
══════════════════════════════════════════════════════════════════════════════

Return ONLY valid JSON matching the schema above. No markdown, no explanations.
"""
        return prompt
