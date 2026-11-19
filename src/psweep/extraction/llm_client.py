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
import threading
import time
from dataclasses import dataclass
from typing import Any

# Pricing metadata must not initiate an unapproved network request at import time.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

import litellm
import openai
from litellm import completion, completion_cost, cost_per_token

from psweep.security import (
    ca_bundle,
    protected_mode,
    provider_error_category,
    require_model_endpoint,
    resolve_tls_verification,
    tls_context,
)
from psweep.exceptions import ExtractionError, ResponseOutcomeError
from psweep.exceptions import AttemptBudgetExceeded
from .llm_factory import DEFAULT_LLM_TIMEOUT_SECONDS, DEFAULT_MODEL, detect_provider
from psweep.utils.retry import compute_backoff, is_transient_error
from .request_policy import (
    RequestNotAdmittedError,
    RouteLimits,
    assess_request,
    image_block,
    route_limits,
)
from .schema_utils import strict_compatible
from .text_processor import find_data_arrays

logger = logging.getLogger(__name__)


# Substring markers that identify "reasoning" models — those that reject a
# non-default temperature. Provider-neutral defaults; a deployment
# can extend this via the top-level ``reasoning_models`` run.yaml list (unioned
# with these defaults) so a new reasoning model works without a code change.
DEFAULT_REASONING_MODEL_MARKERS: tuple[str, ...] = (
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "thinking",
)


def prompt_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the schema the model sees: everything except ``$metadata``."""
    return {key: value for key, value in schema.items() if key != "$metadata"}


RETRY_ATTEMPTS = 4


@dataclass(frozen=True)
class Completion:
    """One answered request: the first choice and its measured usage."""

    content: str | None
    finish_reason: str | None
    usage: dict[str, Any]
    refused: bool = False


class LLMClient:
    """
    Universal LLM client for structured data extraction.

    Uses LiteLLM for unified interface across all providers.
    Automatically tracks costs and handles provider-specific parameters.
    """

    DEFAULT_TIMEOUT = DEFAULT_LLM_TIMEOUT_SECONDS  # seconds; overridable via LLM_TIMEOUT env var

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        provider: str | None = None,
        azure_endpoint: str | None = None,
        azure_api_version: str | None = None,
        context_windows: dict[str, int] | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        reasoning_models: list[str] | None = None,
    ):
        """
        Initialize LLM client.

        Parameters
        ----------
        api_key : str
            API key passed directly to this client, without mutating environment variables
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
                "(extraction:, discovery.document_review:, etc.), "
                "or set LLM_MODEL / AZURE_OPENAI_MODEL / OPENAI_MODEL in your .env file."
            )
        self.raw_model = model  # Keep original for cost tracking
        self.base_url = base_url
        require_model_endpoint(base_url)
        self.ssl_verify = resolve_tls_verification("PSWEEP_SSL_VERIFY")
        self._tls_context = tls_context(self.ssl_verify)
        self._provider_tls = ca_bundle() if self.ssl_verify else False
        self._azure_endpoint = azure_endpoint
        self._azure_api_version = azure_api_version
        self._api_key = api_key  # Stored for direct-pass in endpoint-override mode
        self.usage = {
            "calls": 0,
            "cost": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "unknown_cost_calls": 0,
        }
        self._usage_lock = threading.Lock()
        self.context_windows = dict(context_windows or {})
        # Reasoning-model markers: built-in defaults unioned with any
        # config-supplied ``reasoning_models`` (extra markers, lowercased).
        extra_markers = [m.lower().strip() for m in (reasoning_models or []) if m.strip()]
        self.reasoning_model_markers: tuple[str, ...] = (
            *DEFAULT_REASONING_MODEL_MARKERS,
            *(m for m in extra_markers if m not in DEFAULT_REASONING_MODEL_MARKERS),
        )
        self.timeout = (
            timeout
            if timeout is not None
            else int(os.environ.get("LLM_TIMEOUT", self.DEFAULT_TIMEOUT))
        )
        self._explicit_provider = provider is not None and not model.startswith("openrouter/")
        self.provider = provider or detect_provider(model)

        # Format model name for LiteLLM
        self.model = self._format_model_for_litellm(model, self.provider)

        # Configure LiteLLM
        litellm.drop_params = True  # Drop unsupported params
        litellm.suppress_debug_info = True
        litellm.set_verbose = False
        litellm.turn_off_message_logging = True

        logger.info("Initialized LLM client: provider=%s, model=%s", self.provider, self.model)

    @property
    def usage_snapshot(self) -> dict[str, int | float]:
        """Return a thread-safe copy of the canonical physical-attempt counters."""
        with self._usage_lock:
            return dict(self.usage)

    def _count(self, **deltas) -> None:
        with self._usage_lock:
            for key, value in deltas.items():
                self.usage[key] += value

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
        if provider == "gemini":
            if not model.startswith("gemini/") and not model.startswith("google/"):
                return f"gemini/{model}"
            return model
        return model

    def _complete(self, params: dict[str, Any]):
        """Use client-scoped TLS and disable hidden provider retries."""
        require_model_endpoint(self.base_url)
        protected = protected_mode()
        custom_tls = self._provider_tls is not True
        if (
            not protected
            and not self.base_url
            and not (custom_tls and self.provider in {"openai", "azure"})
        ):
            return completion(**params, num_retries=0)
        if protected and not self._api_key:
            raise ValueError("Protected model endpoint requires explicit credentials")
        import httpx
        from openai import AzureOpenAI, OpenAI

        logging.getLogger("openai").setLevel(logging.WARNING)
        request = dict(params)
        request["model"] = request["model"].removeprefix("openai/")
        for key in ("base_url", "api_key", "timeout", "ssl_verify"):
            request.pop(key, None)
        with httpx.Client(
            verify=self._tls_context,
            trust_env=False,
            follow_redirects=False,
            timeout=self.timeout,
        ) as transport:
            client: OpenAI
            if not self.base_url and self.provider == "azure":
                client = AzureOpenAI(
                    azure_endpoint=self._azure_endpoint,
                    api_version=self._azure_api_version,
                    api_key=self._api_key,
                    timeout=self.timeout,
                    max_retries=0,
                    http_client=transport,
                )
            else:
                client = OpenAI(
                    base_url=self.base_url,
                    api_key=self._api_key,
                    timeout=self.timeout,
                    max_retries=0,
                    http_client=transport,
                )
            with client:
                if protected:
                    # Sensitive requests never enter provider plugins/callbacks.
                    return client.chat.completions.create(**request)
                return completion(**params, client=client, num_retries=0)

    def _complete_with_backoff(self, params: dict[str, Any], before_request=None):
        """Retry transient provider failures (``is_transient_error``) with the shared backoff.

        Each retry is logged with the sanitized failure category only, so slow
        or failing calls are visible without exposing provider text.
        """
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            if attempt > 1 and before_request is not None:
                before_request()
                self._count(calls=1)
            try:
                return self._complete(params)
            except AttemptBudgetExceeded:
                raise
            except Exception as exc:
                if attempt == RETRY_ATTEMPTS or not is_transient_error(exc):
                    raise
                self._count(calls=int(before_request is None), unknown_cost_calls=1)
                wait = compute_backoff(
                    attempt + 1, initial_backoff_seconds=2.0, max_backoff_seconds=30.0
                )
                reason = (
                    f"did not finish within the {self.timeout}s timeout"
                    if isinstance(exc, (openai.APITimeoutError, TimeoutError))
                    else f"request failed: {provider_error_category(exc)} ({type(exc).__name__})"
                )
                logger.warning(
                    "%s %s; retrying in %.0fs (attempt %d/%d)",
                    self.raw_model,
                    reason,
                    wait,
                    attempt + 1,
                    RETRY_ATTEMPTS,
                )
                time.sleep(wait)
        return None

    def _route(self) -> dict[str, Any]:
        """Provider routing and credentials for one request."""
        params: dict[str, Any] = {
            "model": self.model,
            "timeout": self.timeout,
            "ssl_verify": self._provider_tls,
        }
        if self._api_key:
            params["api_key"] = self._api_key
        if self.base_url:
            # The "openai/" model prefix (see _format_model_for_litellm)
            # selects /chat/completions; no custom_llm_provider wanted.
            params["base_url"] = self.base_url
            return params
        if self.provider == "azure":
            if self._azure_endpoint:
                params["api_base"] = self._azure_endpoint
            if self._azure_api_version:
                params["api_version"] = self._azure_api_version
        if self._explicit_provider:
            params["custom_llm_provider"] = (
                "meta_llama" if self.provider == "meta" else self.provider
            )
        return params

    def _cost(self, response: Any) -> float | None:
        """Cost from litellm's bundled price map; ``None`` when unknown.

        A proxy route (``openai/<model>``) is unmapped, so its usage is
        priced as the configured model. Estimates are never substituted.
        """
        try:
            return completion_cost(completion_response=response)
        except Exception as exc:  # noqa: BLE001 - unpriced: unknown cost
            logger.debug("Response cost unavailable (%s)", type(exc).__name__)
        usage = getattr(response, "usage", None)
        if not self.base_url or usage is None:
            return None
        try:
            return sum(cost_per_token(model=self.raw_model, usage_object=usage))
        except Exception as exc:  # noqa: BLE001 - unpriced: unknown cost
            logger.debug("Model price unavailable (%s)", type(exc).__name__)
            return None

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        before_request=None,
        admit: bool = False,
        **params: Any,
    ) -> Completion:
        """Send one chat request and account for it.

        Owns provider routing, transient-failure backoff, usage/cost accounting and
        sanitized ``ExtractionError`` failures; ``params`` are request
        options such as ``temperature`` or ``response_format``. A reply
        without choices raises ``ExtractionError`` carrying its usage.

        The exact request is admitted before any attempt: it must fit the
        operator's ``model_context_windows``. With ``admit=True`` (extraction
        and locator callers) the pinned catalog limits also apply and the
        output is capped at the route's output limit, sent as ``max_tokens``.
        """
        limits = self._route_limits(admit)
        output_cap = limits.output_tokens if admit else None
        if admit or all(isinstance(m.get("content"), str) for m in messages):
            assess_request(messages, params.get("response_format"), limits, output_cap=output_cap)
        if output_cap is not None:
            params = {**params, "max_tokens": output_cap}
        if before_request is not None:
            before_request()
        request = {**self._route(), "messages": messages, **params}
        # Split from content parsing so the two failure modes produce distinct
        # error messages: proxy/network errors vs model returned non-JSON text.
        self._count(calls=1)
        logger.debug(
            "Sending %s request (%d prompt chars, %ss timeout)",
            self.raw_model,
            sum(len(m["content"]) for m in messages if isinstance(m.get("content"), str)),
            self.timeout,
        )
        started = time.monotonic()
        try:
            response = (
                self._complete_with_backoff(request, before_request=before_request)
                if before_request is not None
                else self._complete_with_backoff(request)
            )
        except AttemptBudgetExceeded:
            raise
        except json.JSONDecodeError:
            # LiteLLM failed to parse the HTTP response body from the proxy.
            # Common causes: empty body, SSE stream when non-streaming expected,
            # or an HTML/text error page from a gateway.
            self._count(unknown_cost_calls=1)
            error = ExtractionError(
                f"Proxy returned a non-JSON response body for model={self.raw_model!r}. "
                "Verify the model and endpoint configuration."
            )
            error.provider_failure = True
            raise error from None
        except Exception as e:  # noqa: BLE001 - provider boundary: raw errors are sanitized below
            # Some routed/provider-backed models reject `temperature` entirely.
            # Retry once without temperature when the provider explicitly says the
            # parameter is deprecated/unsupported.
            self._count(unknown_cost_calls=1)
            if not self._should_retry_without_temperature(e, request):
                # Not logger.exception: the traceback would carry unsanitized provider text.
                logger.error(  # noqa: TRY400
                    "LLM request failed (provider=%s, model=%s)", self.provider, self.model
                )
                raise self._provider_error(e) from None
            request = dict(request)
            request.pop("temperature", None)
            if before_request is not None:
                before_request()
            self._count(calls=1)
            try:
                response = self._complete(request)
            except AttemptBudgetExceeded:
                raise
            except Exception as retry_error:  # noqa: BLE001 - provider boundary: raw errors are sanitized below
                self._count(unknown_cost_calls=1)
                logger.error(  # noqa: TRY400 - see above
                    "LLM request failed after retry without temperature (provider=%s, model=%s)",
                    self.provider,
                    self.model,
                )
                raise self._provider_error(retry_error) from None

        usage = getattr(response, "usage", None)
        measured = {
            "cost": self._cost(response),
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
        }
        # Output tokens drive response time: every extracted item is written out.
        logger.debug(
            "%s answered in %.1fs (%s prompt, %s output tokens)",
            self.raw_model,
            time.monotonic() - started,
            measured["input_tokens"],
            measured["output_tokens"],
        )
        self._count(
            cost=measured["cost"] or 0,
            input_tokens=measured["input_tokens"] or 0,
            output_tokens=measured["output_tokens"] or 0,
            unknown_cost_calls=int(measured["cost"] is None),
        )
        choices = getattr(response, "choices", None)
        if not choices:
            error = ExtractionError(
                f"Empty response from provider={self.provider}, model={self.model}"
            )
            error.usage = measured
            error.provider_failure = True
            raise error
        message = choices[0].message
        finish_reason = getattr(choices[0], "finish_reason", None)
        return Completion(
            content=message.content,
            finish_reason=finish_reason,
            usage=measured,
            refused=bool(getattr(message, "refusal", None)) or finish_reason == "content_filter",
        )

    def extract(
        self,
        text: str,
        schema: dict[str, Any],
        system_prompt: str | None = None,
        user_prompt: str | None = None,
        suppress_errors: bool = False,
        before_request=None,
        admit: bool = False,
        *,
        strict_outcomes: bool = False,
        follow_up: list[dict[str, Any]] | None = None,
        page_images: list | None = None,
    ) -> dict[str, Any]:
        """
        Extract structured data using LLM.

        ``strict_outcomes`` (primary extraction only) rejects refused and
        length-terminated answers, and raises empty/non-JSON answers as
        ``ResponseOutcomeError``; other callers keep accepting what they always
        did. ``follow_up`` messages are appended after the unchanged request.
        ``page_images`` (rendered PDF pages) are sent after the text through the
        same ``complete`` path, each labeled with its original page number; a
        route not known to accept images holds before any request.

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

        # Reasoning models reject a non-default temperature; JSON mode stays on
        # for every model (LiteLLM's drop_params removes it where unsupported).
        is_reasoning_model = any(x in self.model.lower() for x in self.reasoning_model_markers)
        params: dict[str, Any] = {"response_format": self._response_format(schema)}
        if not is_reasoning_model:
            params["temperature"] = 0
        # Azure's json_object mode requires the word JSON in the messages.
        all_content = " ".join(m["content"] for m in messages if isinstance(m["content"], str))
        if "json" not in all_content.lower():
            messages[-1]["content"] += (
                "\n\nRespond with ONLY a raw JSON object. "
                "No markdown, no code fences, no explanation."
            )

        if page_images:
            self._require_vision()
            messages[-1]["content"] = [
                {"type": "text", "text": messages[-1]["content"]},
                *(
                    block
                    for image in page_images
                    for block in (
                        {"type": "text", "text": f"[Image of PDF page {image.page}]"},
                        image_block(image),
                    )
                ),
            ]

        messages.extend(follow_up or [])
        if admit:
            params["admit"] = True
        reply = (
            self.complete(messages, before_request=before_request, **params)
            if before_request is not None
            else self.complete(messages, **params)
        )
        measured = reply.usage
        cost = measured["cost"]
        input_tokens = measured["input_tokens"]
        output_tokens = measured["output_tokens"]

        def rejected(message: str, code: str) -> ExtractionError:
            return (
                ResponseOutcomeError(message, code=code)
                if strict_outcomes
                else ExtractionError(message)
            )

        try:
            if strict_outcomes and reply.refused:
                message = (
                    f"Model refused the request (provider={self.provider}, model={self.model})"
                )
                raise ResponseOutcomeError(message, code="response_refused")
            if strict_outcomes and reply.finish_reason == "length":
                message = (
                    "Model output was truncated at its length limit "
                    f"(provider={self.provider}, model={self.model}); the answer is incomplete"
                )
                raise ResponseOutcomeError(message, code="response_truncated")
            content = self._clean_json_content(reply.content)
            if not content:
                (logger.debug if suppress_errors else logger.error)(
                    "Empty response from API (model=%s)",
                    self.model,
                )
                message = f"Empty response from provider={self.provider}, model={self.model}"
                raise rejected(message, "response_empty")

            # Parse JSON while retaining usage on a rejected response.
            try:
                data = json.loads(content)
            except json.JSONDecodeError as e:
                # Recover JSON before trailing prose.
                data = None
                if "Extra data" in str(e):
                    try:
                        data, _ = json.JSONDecoder().raw_decode(content)
                        logger.debug(
                            "Recovered trailing content (model=%s, at=%s)",
                            self.model,
                            e.pos,
                        )
                    except json.JSONDecodeError:
                        pass
                if data is None:
                    (logger.debug if suppress_errors else logger.error)(
                        "Model returned non-JSON content: %s",
                        e,
                    )
                    message = (
                        "Model returned non-JSON content "
                        f"(provider={self.provider}, model={self.model}): {e}"
                    )
                    raise rejected(message, "response_not_json") from None

        except ExtractionError as exc:
            exc.usage = measured
            raise

        total_items = sum(len(value) for _, value in find_data_arrays(data))

        logger.info(
            "✓ Extracted %s items (tokens: %s+%s, cost: %s)",
            total_items,
            input_tokens,
            output_tokens,
            cost,
        )

        return {
            "data": data,
            "cost": cost,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    def _require_vision(self) -> None:
        """Hold unless the pinned catalog says this route accepts images."""
        for name in (self.model, self.raw_model):
            try:
                if litellm.supports_vision(model=name):
                    return
            except Exception:  # noqa: BLE001, S112 - unmapped route: support stays unknown
                continue
        message = (
            f"request_not_admitted: route {self.model!r} is not known to accept image "
            "input (vision); use a vision-capable model or extraction.input_mode: text"
        )
        raise RequestNotAdmittedError(message)

    def _response_format(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Use strict json_schema only when the provider and schema both allow it."""
        sent = prompt_schema(schema)
        try:
            supported = litellm.supports_response_schema(model=self.model, custom_llm_provider=None)
        except Exception:  # noqa: BLE001 - unknown models fall back to JSON mode
            supported = False
        if not self.base_url and supported and strict_compatible(sent):
            return {
                "type": "json_schema",
                "json_schema": {"name": "extraction", "schema": sent, "strict": True},
            }
        return {"type": "json_object"}

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
        stripped = re.sub(
            r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$",
            r"\1",
            text,
            flags=re.DOTALL,
        ).strip()
        if stripped == text and text.startswith("```"):
            # Prose after the closing fence: drop only the opening fence; extract()
            # recovers the JSON value before the closing fence and trailing prose.
            stripped = re.sub(r"^```(?:json)?\s*", "", text)
        return stripped

    @staticmethod
    def _provider_error(error: Exception) -> ExtractionError:
        """Wrap a provider failure without its untrusted text, keeping its retryability."""
        wrapped = ExtractionError(
            f"LLM request failed: {provider_error_category(error)} ({type(error).__name__})"
        )
        wrapped.retryable = is_transient_error(error)
        wrapped.provider_failure = True
        wrapped.route_failure = is_transient_error(error) or getattr(
            error, "status_code", None
        ) in {401, 403}
        return wrapped

    @staticmethod
    def _should_retry_without_temperature(error: Exception, api_params: dict[str, Any]) -> bool:
        """Return True when provider rejected `temperature` and retry is safe."""
        if "temperature" not in api_params:
            return False
        message = str(error).lower()
        if "temperature" not in message:
            return False
        return any(
            token in message for token in ("deprecated", "unsupported", "invalid_request_error")
        )

    def _route_limits(self, admit: bool) -> RouteLimits:
        """Known limits: operator windows always; pinned catalog only when ``admit``."""
        names = (self.model, self.raw_model)
        if admit:
            return route_limits(*names, configured_windows=self.context_windows)
        configured = route_limits(
            *(n for n in names if n in self.context_windows),
            configured_windows=self.context_windows,
        )
        return RouteLimits(input_tokens=configured.input_tokens)

    @staticmethod
    def _build_extraction_prompt(text: str, schema: dict[str, Any]) -> str:
        """Build a domain-neutral prompt from the schema without ``$metadata``."""
        array = schema.get("$metadata", {}).get("extraction", {}).get("main_data_array")
        target = f'every "{array}" item' if array else "all data"
        return f"""Extract {target} from the document text into JSON matching the schema below.

RULES
1. Extract exactly as written. Do not normalize, convert units, paraphrase, or infer
   unless a schema field description explicitly requires that conversion; then apply
   only that conversion to a value the document states.
2. Search the entire document, including tables, lists, and appendices.
3. Follow the schema descriptions; use null where the document does not state a value
   or a required conversion cannot be made from stated values. Never invent a value.
4. Evidence quotes are always verbatim document text, never converted or reworded.
5. Blocks marked "added by ParseSweep" are guidance, not document content.

JSON SCHEMA
{json.dumps(prompt_schema(schema), indent=2)}

DOCUMENT TEXT
{text}

Return ONLY valid JSON matching the schema above. No markdown, no explanations.
"""
