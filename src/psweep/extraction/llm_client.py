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
from typing import Dict, Any, Optional, List

import litellm
from litellm import completion, completion_cost

from psweep.utils.model_pricing import get_pricing
from psweep.utils.exceptions import ExtractionError
from .llm_factory import detect_provider

logger = logging.getLogger(__name__)


class LLMClient:
    """
    Universal LLM client for structured data extraction.

    Uses LiteLLM for unified interface across all providers.
    Automatically tracks costs and handles provider-specific parameters.
    """

    CONTEXT_RESPONSE_RESERVE_TOKENS = 4096

    def __init__(
        self,
        api_key: str = None,
        model: str = "gpt-4o-mini",
        provider: str = None,
        azure_endpoint: str = None,
        azure_api_version: str = None,
        context_windows: Optional[Dict[str, int]] = None,
    ):
        """
        Initialize LLM client.

        Args:
            api_key: API key for the provider (will auto-set environment variables)
            model: Model name (e.g., "gpt-4o-mini", "claude-3.5-sonnet", "gemini-1.5-pro")
            provider: Explicit provider ("openai", "azure", "anthropic", "gemini")
                     If None, auto-detects from model name
            azure_endpoint: Azure OpenAI endpoint (for Azure provider)
            azure_api_version: Azure API version (for Azure provider)
            context_windows: Optional {model-name -> max prompt tokens} map used to
                     fail fast before an over-budget request. Domain/deployment
                     names are NOT hardcoded — supply this from config
                     (``model_context_windows`` in run.yaml) for deployments whose
                     context window LiteLLM cannot infer. Unset models are not
                     guarded.
        """
        self.raw_model = model  # Keep original for cost tracking
        self.context_windows = dict(context_windows or {})
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

        logger.info(
            f"Initialized LLM client: provider={self.provider}, model={self.model}"
        )

    def _format_model_for_litellm(self, model: str, provider: str) -> str:
        """
        Format model name for LiteLLM.

        LiteLLM requires provider prefixes for some models:
        - Azure: "azure/deployment-name"
        - Anthropic: "claude-3.5-sonnet" (no prefix needed)
        - Gemini: "gemini/gemini-1.5-pro" (optional)
        """
        if provider == "azure":
            # Azure models must be prefixed with "azure/"
            if not model.startswith("azure/"):
                return f"azure/{model}"
            return model
        elif provider == "gemini":
            # Gemini can optionally have "gemini/" prefix
            if not model.startswith("gemini/") and not model.startswith(
                "google/"
            ):
                return f"gemini/{model}"
            return model
        else:
            # OpenAI, Anthropic, etc. don't need prefixes
            return model

    def _configure_environment(
        self,
        api_key: Optional[str],
        azure_endpoint: Optional[str],
        azure_api_version: Optional[str],
    ):
        """Configure environment variables for LiteLLM."""
        if not api_key:
            return

        # Set provider-specific environment variables
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
    ) -> Dict[str, Any]:
        """
        Extract structured data using LLM.

        Args:
            text: Document text to extract from
            schema: JSON schema for extraction
            system_prompt: Optional custom system prompt
            user_prompt: Optional custom user prompt (overrides default)

        Returns:
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
            x in self.model.lower()
            for x in ["gpt-5", "o1", "o3", "o4", "thinking"]
        )

        # Prepare API call parameters
        api_params = {
            "model": self.model,
            "messages": messages,
        }

        if not is_reasoning_model:
            api_params["temperature"] = 0
            api_params["response_format"] = {"type": "json_object"}

        try:
            # Call LiteLLM
            response = completion(**api_params)

            # Validate response
            if not response.choices or not response.choices[0].message.content:
                logger.error(f"Empty response from API (model={self.model})")
                raise ExtractionError(
                    f"Empty response from provider={self.provider}, model={self.model}"
                )

            # Parse JSON response
            content = response.choices[0].message.content
            data = json.loads(content)

            # Calculate cost using LiteLLM's built-in tracking
            try:
                cost = completion_cost(completion_response=response)
            except Exception as e:
                logger.debug(
                    f"LiteLLM cost calculation failed, using pricing DB: {e}"
                )
                cost = self.pricing_db.get_cost(
                    self.raw_model,  # Use raw model name for pricing lookup
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                )

            # Count extracted items
            total_items = sum(
                len(value)
                for value in data.values()
                if isinstance(value, list)
            )

            usage = getattr(response, "usage", None)
            input_tokens = getattr(usage, "prompt_tokens", None)
            output_tokens = getattr(usage, "completion_tokens", None)

            logger.info(
                f"✓ Extracted {total_items} items "
                f"(tokens: {input_tokens}+{output_tokens}, "
                f"cost: ${cost:.4f})"
            )

            return {
                "data": data,
                "cost": cost,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}")
            logger.debug(f"Response content: {content[:500]}...")
            raise ExtractionError(
                f"Failed to parse JSON response from provider={self.provider}, model={self.model}: {e}"
            ) from e

        except Exception as e:
            logger.exception(
                f"LLM extraction failed (provider={self.provider}, model={self.model})"
            )
            raise ExtractionError(str(e)) from e

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
