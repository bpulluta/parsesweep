"""
Model Detector for QA/QC Multi-Model Validation.

Resolves QA/QC models exclusively from the QAQC_MODELS environment variable.
There are no hardcoded defaults — Azure deployment names are environment-specific
and must always be configured explicitly.

Usage:
    from psweep.qa_qc.model_detector import ModelDetector

    # Get models for QA/QC (requires QAQC_MODELS to be set)
    models = ModelDetector.get_qa_models()
    # Returns: ['my-gpt-4o-deployment', 'my-gpt-4.1-deployment']

    # Get current provider
    provider = ModelDetector.get_provider()
    # Returns: 'azure' or 'openai'

Environment Variables:
    QAQC_MODELS: Comma-separated list of 2+ models to use for QA/QC validation.
                 REQUIRED — raises ValueError if not set or empty.

    Examples:
        - 2 models: QAQC_MODELS=my-deployment-gpt-5-mini,my-deployment-gpt-4o
        - 3 models: QAQC_MODELS=my-deployment-gpt-5,my-deployment-gpt-4.1,my-deployment-gpt-4o
        - 5 models: QAQC_MODELS=my-deployment-gpt-5,my-deployment-gpt-5-mini,my-deployment-gpt-4.1,my-deployment-gpt-4.1-mini,my-deployment-gpt-4o
"""

import os
import logging
from typing import List

logger = logging.getLogger(__name__)


class ModelDetector:
    """Resolve QA/QC models from environment. QAQC_MODELS must be set explicitly."""

    # Minimum number of models required for meaningful comparison
    MIN_MODELS = 2

    @staticmethod
    def get_qa_models() -> List[str]:
        """
        Get QA/QC models from QAQC_MODELS env var.

        Returns:
            List of model names (minimum 2)

        Raises:
            ValueError: If QAQC_MODELS is not set, empty, or contains fewer than 2 models

        Examples:
            QAQC_MODELS="gpt-5,gpt-4.1" → ['gpt-5', 'gpt-4.1']
        """
        custom_models = os.getenv("QAQC_MODELS", "").strip()

        if not custom_models:
            raise ValueError(
                "QAQC_MODELS environment variable is not set. "
                "QA/QC requires at least 2 explicitly configured models.\n"
                "Set QAQC_MODELS with 2+ comma-separated deployment names, e.g.:\n"
                "  QAQC_MODELS=my-gpt-4o-deployment,my-gpt-4.1-deployment"
            )

        # Parse comma-separated models
        models = [m.strip() for m in custom_models.split(",") if m.strip()]

        # Validate minimum
        if len(models) < ModelDetector.MIN_MODELS:
            raise ValueError(
                f"QA/QC requires at least {ModelDetector.MIN_MODELS} models. "
                f"Found {len(models)}: {models}\n"
                "Set QAQC_MODELS with 2+ comma-separated models."
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
            f"QA/QC using models from QAQC_MODELS: {unique_models}"
        )
        return unique_models

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
            Dict with configuration info for display/logging.
            'models' will be None and 'error' will be set if QAQC_MODELS is not configured.
        """
        provider = ModelDetector.get_provider()
        custom_models = os.getenv("QAQC_MODELS", "").strip()

        try:
            models = ModelDetector.get_qa_models()
            models_error = None
        except ValueError as exc:
            models = None
            models_error = str(exc)

        return {
            "provider": provider,
            "custom_models_set": bool(custom_models),
            "models": models,
            "models_error": models_error,
            "azure_endpoint": os.getenv("AZURE_OPENAI_ENDPOINT", "Not set"),
            "has_azure_key": bool(os.getenv("AZURE_OPENAI_API_KEY")),
            "has_openai_key": bool(os.getenv("OPENAI_API_KEY")),
        }
