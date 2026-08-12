"""
Universal document extractor for structured data extraction.

Architecture:
1. OpenAI Structured - Fast, accurate extraction with schema-driven parsing
2. Multi-Model QA/QC - Validates results using multiple AI models (separate command)

Supports any document type with a defined JSON schema - fully domain-agnostic.

Cost: ~$0.002-0.004 per document (varies by length and model)
Accuracy: 90%+ on validation set
"""

import logging
import time
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

from .llm_client import LLMClient
from .llm_factory import DEFAULT_MODEL
from .text_processor import TextProcessor

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Result from document extraction."""

    data: Dict[str, Any]
    completeness_score: float  # 0-1 score based on fields populated
    cost: float
    processing_time: float
    validation_notes: List[str]
    validation_report: Optional[Any] = None  # Optional QA/QC validation report
    input_tokens: Optional[int] = None  # Prompt tokens (from provider usage)
    output_tokens: Optional[int] = None  # Completion tokens (from provider usage)


class DocumentExtractor:
    """
    Universal document extractor with traceability and configurable context.

    Architecture:
    1. LLM Structured Extraction: Fast extraction with schema-driven parsing
    2. Multi-Model QA/QC: Validates via multiple models (run separately via `psweep validate`)

    Works with any document type and JSON schema - fully domain-agnostic.

    Simple usage:
        >>> extractor = DocumentExtractor(api_key="sk-...")
        >>> result = extractor.extract(text, schema)
        >>> print(f"Extracted: {result.data}")
        >>> print(f"Confidence: {result.completeness_score:.0%}")

    Architecture:
        - LLMClient: Handles all API calls via LiteLLM (100+ providers)
        - TextProcessor: Optimizes text and normalizes output
    """

    def __init__(
        self,
        api_key: str = None,
        model: str = DEFAULT_MODEL,
        max_context_chars: int = 400000,
        schema_metadata=None,
        provider: str = None,
        azure_endpoint: str = None,
        azure_api_version: str = None,
        context_windows: Optional[Dict[str, int]] = None,
        base_url: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        """
        Initialize document extractor.

        Args:
            api_key: API key for the LLM provider
            model: Model to use (default: gpt-4o-mini)
            max_context_chars: Maximum characters to extract from document (default: 400000)
            schema_metadata: Optional SchemaMetadata for metadata-driven processing
            provider: LLM provider ("openai", "azure", "anthropic", "gemini", etc.)
            azure_endpoint: Azure OpenAI endpoint URL (for Azure provider)
            azure_api_version: Azure API version (for Azure provider)
            context_windows: Optional {model-name -> max prompt tokens} map for the
                fail-fast context-budget guard (from ``model_context_windows`` in
                config). No model names are hardcoded.
            base_url: Optional endpoint override for OpenAI-compatible proxies
                (LiteLLM, OpenRouter, vLLM, etc.). When set, all calls are routed
                through this URL regardless of model name.
            timeout: Per-request LLM timeout in seconds. If omitted, LLMClient
                uses LLM_TIMEOUT env var or its default timeout.
        """
        self.api_key = api_key
        self.model = model
        self.max_context_chars = max_context_chars
        self.schema_metadata = schema_metadata

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
        )

        # Initialize text processor
        self.processor = TextProcessor(max_chars=max_context_chars)

    def extract(self, text: str, schema: Dict[str, Any]) -> ExtractionResult:
        """
        Extract structured data from document.

        This method orchestrates:
        1. LLM Structured Extraction - Fast, schema-driven parsing
        2. Post-processing - Normalization and sanity checks

        For multi-model QA/QC validation, use the CLI validate stage.

        Args:
            text: Full document text to extract from
            schema: JSON schema defining structure to extract (must have 'type' and 'properties')

        Returns
        -------
            ExtractionResult containing:
                - data: Extracted structured data matching schema
                - completeness_score: 0-1 confidence score
                - cost: Total API cost in USD
                - processing_time: Total time in seconds
                - validation_notes: List of warnings/insights
                - validation_report: Reserved for future use

        Example:
            >>> result = extractor.extract(pdf_text, tariff_schema)
            >>> if result.completeness_score > 0.8:
            >>>     print("High confidence extraction!")
        """
        start_time = time.time()
        total_cost = 0.0
        validation_notes = []

        # Stage 1: LLM Structured Extraction
        logger.info("🤖 Stage 1: LLM Structured Extraction")
        extraction = self._extract_structured(text, schema)
        total_cost += extraction["cost"]
        validated_data = extraction["data"]

        # Stage 2: Post-extraction sanity checks and normalization
        validated_data = self.processor.normalize_string_fields(validated_data)
        sanity_warnings = self.processor.run_sanity_checks(
            validated_data, schema_metadata=self.schema_metadata
        )
        validation_notes.extend(sanity_warnings)

        processing_time = time.time() - start_time

        completeness = self.processor.calculate_completeness(
            validated_data, validation_notes
        )

        return ExtractionResult(
            data=validated_data,
            completeness_score=completeness,
            cost=total_cost,
            processing_time=processing_time,
            validation_notes=validation_notes,
            validation_report=None,
            input_tokens=extraction.get("input_tokens"),
            output_tokens=extraction.get("output_tokens"),
        )

    def _extract_structured(
        self, text: str, schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Extract using LLM structured outputs (provider-agnostic via LLMClient).

        Delegates to LLMClient for API calls and TextProcessor for optimization.

        Args:
            text: Document text to extract from
            schema: JSON schema for validation

        Returns
        -------
            Dict with 'data', 'cost' (USD), and 'input_tokens'/'output_tokens'
        """
        # Use text processor to optimize text for extraction
        text_excerpt, _ = self.processor.optimize(text)

        # Use LLM client for extraction
        result = self.client.extract(text_excerpt, schema)

        # Ensure all schema fields are present (fill missing with null)
        if result["data"]:
            result["data"] = self.processor.normalize_with_schema(
                result["data"], schema
            )

        return result
