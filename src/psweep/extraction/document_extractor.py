"""Schema-driven extraction with measured usage and scope limits.

Structural validation does not certify source grounding or accuracy.
"""

import json
import logging
import time
from typing import Any
from dataclasses import dataclass

from psweep.exceptions import ExtractionError, ResponseOutcomeError, SchemaValidationError
from psweep.extraction.schema_utils import schema_diagnostics, validate_payload
from .llm_client import RETRY_ATTEMPTS, LLMClient
from .llm_factory import DEFAULT_MAX_CONTEXT, DEFAULT_MODEL
from .request_policy import AttemptAllowance
from .text_processor import (
    calculate_completeness,
    normalize_string_fields,
    normalize_with_schema,
)

logger = logging.getLogger(__name__)

_USAGE_KEYS = ("cost", "input_tokens", "output_tokens")
# Names the extraction prompt and the one-schema-feedback-generation policy; bump it
# when either changes what a document is asked or how a rejected answer is repaired.
EXTRACTION_BEHAVIOR_VERSION = "extraction-behavior-v1"


def _total_usage(spent: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    """Sum billed generations; an unknown part keeps the total unknown (``None``)."""
    billed = [usage for usage in spent if usage]
    if not billed:
        return None
    return {
        key: None
        if any(usage.get(key) is None for usage in billed)
        else sum(u[key] for u in billed)
        for key in _USAGE_KEYS
    }


def _feedback(failure: Exception, answer: Any, validator) -> list[dict[str, str]]:
    """Follow-up turns for one feedback generation against the unchanged request."""
    if isinstance(failure, SchemaValidationError):
        problems = "\n".join(
            f"- {line}" for line in schema_diagnostics(validator.iter_errors(answer))
        )
        reason = f"did not satisfy the JSON schema:\n{problems}"
        turns = [{"role": "assistant", "content": json.dumps(answer)}]
    else:
        reason = "was empty or not a single valid JSON object."
        turns = []
    return [
        *turns,
        {
            "role": "user",
            "content": (
                f"Your previous answer {reason}\n"
                "Return the complete corrected JSON object for the same document and schema. "
                "Use only values the document states; use null where the schema allows instead "
                "of inventing one, and add no keys the schema does not define. "
                "Return ONLY valid JSON."
            ),
        },
    ]


@dataclass
class ExtractionResult:
    """Result from document extraction."""

    data: dict[str, Any]
    completeness_score: float  # 0-1 score based on fields populated
    cost: float | None
    processing_time: float
    validation_notes: list[str]
    input_tokens: int | None = None  # Prompt tokens (from provider usage)
    output_tokens: int | None = None  # Completion tokens (from provider usage)
    coverage: dict[str, Any] | None = None
    status: str = "extracted"
    unverified_quotes: int = 0
    off_candidate_items: int = 0


class DocumentExtractor:
    """
    Domain-agnostic, schema-driven document extractor (LLM calls via LiteLLM).

    Examples
    --------
    >>> extractor = DocumentExtractor(api_key="sk-...")
    >>> result = extractor.extract(text, schema)
    >>> print(f"Extracted: {result.data}")
    >>> print(f"Confidence: {result.completeness_score:.0%}")
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        max_context_chars: int = DEFAULT_MAX_CONTEXT,
        provider: str | None = None,
        azure_endpoint: str | None = None,
        azure_api_version: str | None = None,
        context_windows: dict[str, int] | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        reasoning_models: list[str] | None = None,
    ):
        """
        Initialize document extractor.

        Parameters
        ----------
        api_key : str
            API key for the LLM provider
        model : str
            Model to use (default: gpt-4o-mini)
        max_context_chars : int
            Maximum characters to extract from document (default: 400000)
        provider : str
            LLM provider ("openai", "azure", "anthropic", "gemini", etc.)
        azure_endpoint : str
            Azure OpenAI endpoint URL (for Azure provider)
        azure_api_version : str
            Azure API version (for Azure provider)
        context_windows : Optional[Dict[str, int]]
            Optional {model-name -> max prompt tokens} map for the
            fail-fast context-budget guard (from ``model_context_windows`` in
            config). No model names are hardcoded.
        base_url : Optional[str]
            Optional endpoint override for OpenAI-compatible proxies
            (LiteLLM, OpenRouter, vLLM, etc.). When set, all calls are routed
            through this URL regardless of model name.
        timeout : Optional[int]
            Per-request LLM timeout in seconds. If omitted, LLMClient
            uses LLM_TIMEOUT env var or its default timeout.
        reasoning_models : Optional[List[str]]
            Optional extra reasoning-model markers (from ``reasoning_models``
            in config), unioned with the built-in defaults. No model names are
            hardcoded in universal code beyond provider-neutral defaults.
        """
        self.api_key = api_key
        self.model = model
        self.max_context_chars = max_context_chars

        # Initialize LLM client with multi-provider support
        self.client = LLMClient(
            api_key=api_key,
            model=model,
            provider=provider,
            azure_endpoint=azure_endpoint,
            azure_api_version=azure_api_version,
            context_windows=context_windows,
            base_url=base_url,
            timeout=timeout,
            reasoning_models=reasoning_models,
        )

    def extract(
        self, text: str, schema: dict[str, Any], page_images: list | None = None
    ) -> ExtractionResult:
        """
        Extract structured data from document.

        This method orchestrates:

        1. LLM Structured Extraction - Fast, schema-driven parsing
        2. Post-processing - Normalization

        Parameters
        ----------
        text : str
            Full document text to extract from
        schema : Dict[str, Any]
            JSON schema defining structure to extract (must have 'type' and 'properties')
        page_images : list, optional
            Rendered PDF pages sent with the text (``extraction.input_mode``).

        Returns
        -------
        ExtractionResult
            ExtractionResult containing:

            - data: Extracted structured data matching schema
            - completeness_score: 0-1 confidence score
            - cost: Total API cost in USD
            - processing_time: Total time in seconds
            - validation_notes: List of warnings/insights

        Examples
        --------
        >>> result = extractor.extract(pdf_text, tariff_schema)
        >>> if result.completeness_score > 0.8:
        >>>     print("High confidence extraction!")
        """
        start_time = time.time()
        validation_notes = []
        if len(text) > self.max_context_chars:
            # Never recrop silently: a prefix is not a document.
            message = (
                f"context_window_exceeded: input is {len(text)} characters but "
                f"max_context_chars is {self.max_context_chars}. Use page ranges or "
                "targeting to select the relevant part."
            )
            raise ExtractionError(message)
        excerpt = text
        coverage = {
            "input_characters": len(text),
            "extracted_characters": len(text),
        }

        from .schema_utils import build_schema_validator

        validator = build_schema_validator(schema)

        # Stage 1: LLM Structured Extraction (one document-local attempt allowance)
        logger.info("🤖 Stage 1: LLM Structured Extraction")
        validated_data, usage, repaired = self._generate(excerpt, schema, validator, page_images)
        if repaired:
            validation_notes.append(
                "schema_feedback: first answer was rejected by the schema; accepted after "
                "one feedback generation"
            )

        processing_time = time.time() - start_time

        completeness = calculate_completeness(validated_data)

        return ExtractionResult(
            data=validated_data,
            completeness_score=completeness,
            cost=usage["cost"],
            processing_time=processing_time,
            validation_notes=validation_notes,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            coverage=coverage,
        )

    def _generate(self, excerpt, schema, validator, page_images=None):
        """Return ``(validated payload, total usage, repaired)``.

        The original generation plus at most one feedback generation against the
        same contract and evidence, all drawing on one allowance. Truncation,
        refusal and provider failures are never regenerated; every billed
        generation stays in the usage attached to a failure.
        """
        allowance = AttemptAllowance(RETRY_ATTEMPTS)
        spent: list[dict[str, Any] | None] = []
        follow_up = None
        for generation in (1, 2):
            answer = None
            try:
                extraction = self.client.extract(
                    excerpt,
                    schema,
                    admit=True,
                    strict_outcomes=True,
                    page_images=page_images,
                    before_request=allowance.reserve,
                    **({"follow_up": follow_up} if follow_up else {}),
                )
                spent.append({key: extraction.get(key) for key in _USAGE_KEYS})
                answer = extraction["data"]
                # Validate before normalization hides contract violations.
                self._validate_payload(answer, validator)
                validated = normalize_with_schema(answer, schema, validator=validator)
                validated = normalize_string_fields(validated)
                self._validate_payload(validated, validator)
                return validated, _total_usage(spent), generation == 2
            except Exception as exc:
                if answer is None:  # the request itself failed: its usage rides on the error
                    spent.append(getattr(exc, "usage", None))
                repairable = isinstance(exc, SchemaValidationError) or (
                    isinstance(exc, ResponseOutcomeError) and exc.repairable
                )
                if generation == 2 or not repairable or allowance.remaining < 1:
                    if (total := _total_usage(spent)) is not None:
                        exc.usage = total
                    raise
                follow_up = _feedback(exc, answer, validator)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _validate_payload(data, validator) -> None:
        """Reject invalid payloads using the shared error taxonomy."""
        validate_payload(data, validator)
