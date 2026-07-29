"""
Model Detector for QA/QC Multi-Model Validation.

Auto-detects QA/QC models from environment or provides smart defaults.

Usage:
    from psweep.qa_qc.model_detector import ModelDetector

    # Get models for QA/QC (auto-detects from environment or uses defaults)
    models = ModelDetector.get_qa_models()
    # Returns: ['gpt-4o', 'gpt-4.1', 'gpt-4o-mini']

    # Get current provider
    provider = ModelDetector.get_provider()
    # Returns: 'azure' or 'openai'

Environment Variables:
    QAQC_MODELS: Comma-separated list of 2+ models to use for QA/QC validation
                 Leave empty to auto-select diverse models based on provider

    Examples:
        - 2 models: QAQC_MODELS=my-deployment-gpt-5-mini,my-deployment-gpt-4o
        - 3 models: QAQC_MODELS=my-deployment-gpt-5,my-deployment-gpt-4.1,my-deployment-gpt-4o
        - 5 models: QAQC_MODELS=my-deployment-gpt-5,my-deployment-gpt-5-mini,my-deployment-gpt-4.1,my-deployment-gpt-4.1-mini,my-deployment-gpt-4o

    Note: Azure OpenAI deployment names are environment-specific and must be
    configured via QAQC_MODELS (or the runtime config pack's `models:` block)
    rather than hardcoded here.
"""

import os
import logging
from typing import List

logger = logging.getLogger(__name__)


class ModelDetector:
    """Auto-detect QA/QC models from environment."""

    # Default Azure OpenAI models (generic model names; actual deployment
    # names are environment-specific and should be set via QAQC_MODELS or
    # the runtime config pack's `models:` block).
    AZURE_QA_MODELS = [
        "gpt-4o",  # Optimized
        "gpt-4.1",  # High accuracy
    ]

    # Default OpenAI models
    OPENAI_QA_MODELS = [
        "gpt-4o",  # Latest optimized
        "gpt-4-turbo",  # High capability
        "gpt-3.5-turbo",  # Fast baseline
    ]

    # Minimum number of models required for meaningful comparison
    MIN_MODELS = 2

    @staticmethod
    def get_qa_models() -> List[str]:
        """
        Get QA/QC models from QAQC_MODELS env var or auto-detect.

        Returns:
            List of model names (minimum 2)

        Raises:
            ValueError: If less than 2 models specified

        Examples:
            QAQC_MODELS="gpt-5,gpt-4.1" → ['gpt-5', 'gpt-4.1']
            QAQC_MODELS="" → Auto-detect based on provider
        """
        # Check for custom QAQC_MODELS first
        custom_models = os.getenv("QAQC_MODELS", "").strip()

        if custom_models:
            # Parse comma-separated models
            models = [m.strip() for m in custom_models.split(",") if m.strip()]

            # Validate minimum
            if len(models) < ModelDetector.MIN_MODELS:
                raise ValueError(
                    f"QA/QC requires at least {ModelDetector.MIN_MODELS} models. "
                    f"Found {len(models)}: {models}\n"
                    "Set QAQC_MODELS with 2+ comma-separated models or leave empty for defaults."
                )

            # Remove duplicates (preserve order)
            seen = set()
            unique_models = []
            for m in models:
                if m not in seen:
                    seen.add(m)
                    unique_models.append(m)

            if len(unique_models) < len(models):
                logger.warning(
                    f"Removed duplicate models. Using: {unique_models}"
                )

            logger.info(
                f"QA/QC using custom models from QAQC_MODELS: {unique_models}"
            )
            return unique_models

        # Fall back to auto-detection based on provider
        provider = ModelDetector.get_provider()

        if provider == "azure":
            models = ModelDetector.AZURE_QA_MODELS.copy()
            logger.info(
                f"QA/QC auto-detected Azure OpenAI. Using default models: {models}"
            )
        else:
            models = ModelDetector.OPENAI_QA_MODELS.copy()
            logger.info(
                f"QA/QC auto-detected OpenAI. Using default models: {models}"
            )

        return models

    @staticmethod
    def get_provider() -> str:
        """
        Get current provider (azure or openai).

        Returns:
            'azure' if Azure OpenAI credentials are present, else 'openai'
        """
        if os.getenv("AZURE_OPENAI_API_KEY") and os.getenv(
            "AZURE_OPENAI_ENDPOINT"
        ):
            return "azure"
        return "openai"

    @staticmethod
    def validate_models(models: List[str]) -> None:
        """
        Validate that the model list meets requirements.

        Args:
            models: List of model names to validate

        Raises:
            ValueError: If validation fails
        """
        if not models:
            raise ValueError("No models provided for QA/QC validation")

        if len(models) < ModelDetector.MIN_MODELS:
            raise ValueError(
                f"QA/QC requires at least {ModelDetector.MIN_MODELS} models. "
                f"Found {len(models)}: {models}"
            )

        # Check for duplicates
        if len(models) != len(set(models)):
            duplicates = [m for m in models if models.count(m) > 1]
            raise ValueError(
                f"Duplicate models found: {list(set(duplicates))}. "
                "Each model should only appear once."
            )

    @staticmethod
    def get_model_info() -> dict:
        """
        Get information about current QA/QC configuration.

        Returns:
            Dict with configuration info for display/logging
        """
        provider = ModelDetector.get_provider()
        custom_models = os.getenv("QAQC_MODELS", "").strip()

        return {
            "provider": provider,
            "custom_models_set": bool(custom_models),
            "models": ModelDetector.get_qa_models(),
            "azure_endpoint": os.getenv("AZURE_OPENAI_ENDPOINT", "Not set"),
            "has_azure_key": bool(os.getenv("AZURE_OPENAI_API_KEY")),
            "has_openai_key": bool(os.getenv("OPENAI_API_KEY")),
        }
