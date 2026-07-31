"""
Unit tests for QA/QC Model Detector.

Tests the model detection and configuration for multi-model QA/QC validation.
"""

import os
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def clean_env():
    """Clean relevant env vars before each test and restore afterward."""
    env_vars = [
        "QAQC_MODELS",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "OPENAI_API_KEY",
    ]
    original = {var: os.environ.get(var) for var in env_vars}

    for var in env_vars:
        os.environ.pop(var, None)

    yield

    for var, val in original.items():
        if val is not None:
            os.environ[var] = val
        else:
            os.environ.pop(var, None)


class TestModelDetector:
    """Tests for ModelDetector class."""

    pass


class TestGetQaModels:
    """Tests for get_qa_models() method."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("model-a,model-b", ["model-a", "model-b"]),
            ("gpt-4o,gpt-4-turbo,gpt-3.5-turbo", ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]),
            ("m1,m2,m3,m4,m5", ["m1", "m2", "m3", "m4", "m5"]),
        ],
    )
    def test_custom_models_lists(self, raw, expected):
        from psweep.qa_qc.model_detector import ModelDetector

        os.environ["QAQC_MODELS"] = raw
        models = ModelDetector.get_qa_models()
        assert models == expected
        assert len(models) == len(expected)

    def test_custom_models_strips_whitespace(self):
        """Test that whitespace is stripped from model names."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["QAQC_MODELS"] = "  model-a , model-b  ,  model-c  "
        models = ModelDetector.get_qa_models()
        
        assert models == ["model-a", "model-b", "model-c"]

    def test_custom_models_removes_duplicates(self):
        """Test that duplicate models are removed."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["QAQC_MODELS"] = "model-a,model-b,model-a,model-c,model-b"
        models = ModelDetector.get_qa_models()
        
        # Order preserved, duplicates removed
        assert models == ["model-a", "model-b", "model-c"]

    def test_custom_models_single_fails(self):
        """Test that single model raises ValueError."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["QAQC_MODELS"] = "only-one-model"
        
        with pytest.raises(ValueError) as exc_info:
            ModelDetector.get_qa_models()
        
        assert "at least 2 models" in str(exc_info.value)

    def test_empty_qaqc_models_raises(self):
        """Test empty QAQC_MODELS raises ValueError (no silent fallback)."""
        from psweep.qa_qc.model_detector import ModelDetector

        os.environ["QAQC_MODELS"] = ""

        with pytest.raises(ValueError) as exc_info:
            ModelDetector.get_qa_models()

        assert "QAQC_MODELS" in str(exc_info.value)

    def test_missing_qaqc_models_raises_with_azure_creds(self):
        """Test that Azure credentials alone do not supply defaults — QAQC_MODELS required."""
        from psweep.qa_qc.model_detector import ModelDetector

        os.environ["AZURE_OPENAI_API_KEY"] = "azure-test-key"
        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://test.openai.azure.com"
        # QAQC_MODELS intentionally not set

        with pytest.raises(ValueError) as exc_info:
            ModelDetector.get_qa_models()

        assert "QAQC_MODELS" in str(exc_info.value)

    def test_missing_qaqc_models_raises_with_openai_creds(self):
        """Test that OpenAI credentials alone do not supply defaults — QAQC_MODELS required."""
        from psweep.qa_qc.model_detector import ModelDetector

        os.environ["OPENAI_API_KEY"] = "sk-test"
        # QAQC_MODELS intentionally not set

        with pytest.raises(ValueError) as exc_info:
            ModelDetector.get_qa_models()

        assert "QAQC_MODELS" in str(exc_info.value)

class TestGetProvider:
    """Tests for get_provider() method."""

    @pytest.mark.parametrize(
        ("azure_key", "azure_endpoint", "expected_provider"),
        [
            ("azure-test-key", "https://test.openai.azure.com", "azure"),
            (None, None, "openai"),
            ("azure-test-key", None, "openai"),
        ],
    )
    def test_get_provider(self, azure_key, azure_endpoint, expected_provider):
        from psweep.qa_qc.model_detector import ModelDetector

        if azure_key is not None:
            os.environ["AZURE_OPENAI_API_KEY"] = azure_key
        if azure_endpoint is not None:
            os.environ["AZURE_OPENAI_ENDPOINT"] = azure_endpoint

        assert ModelDetector.get_provider() == expected_provider


class TestValidateModels:
    """Tests for validate_models() method."""

    def test_validate_valid_models(self):
        """Test validation passes with valid models."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        # Should not raise
        ModelDetector.validate_models(["model-a", "model-b"])
        ModelDetector.validate_models(["m1", "m2", "m3"])

    def test_validate_empty_list_fails(self):
        """Test validation fails with empty list."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        with pytest.raises(ValueError) as exc_info:
            ModelDetector.validate_models([])
        
        assert "No models provided" in str(exc_info.value)

    def test_validate_single_model_fails(self):
        """Test validation fails with single model."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        with pytest.raises(ValueError) as exc_info:
            ModelDetector.validate_models(["only-one"])
        
        assert "at least 2 models" in str(exc_info.value)

    def test_validate_duplicates_fails(self):
        """Test validation fails with duplicate models."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        with pytest.raises(ValueError) as exc_info:
            ModelDetector.validate_models(["model-a", "model-b", "model-a"])
        
        assert "Duplicate models" in str(exc_info.value)


class TestGetModelInfo:
    """Tests for get_model_info() method."""

    def test_get_model_info_structure(self):
        """Test get_model_info returns expected structure."""
        from psweep.qa_qc.model_detector import ModelDetector

        os.environ["QAQC_MODELS"] = "model-x,model-y"

        info = ModelDetector.get_model_info()

        assert "provider" in info
        assert "custom_models_set" in info
        assert "models" in info
        assert "models_error" in info
        assert "has_azure_key" in info
        assert "has_openai_key" in info
        assert info["models"] == ["model-x", "model-y"]
        assert info["models_error"] is None

    def test_get_model_info_without_qaqc_models(self):
        """Test get_model_info reports error gracefully when QAQC_MODELS is missing."""
        from psweep.qa_qc.model_detector import ModelDetector

        # QAQC_MODELS not set
        info = ModelDetector.get_model_info()

        assert info["custom_models_set"] is False
        assert info["models"] is None
        assert info["models_error"] is not None
        assert "QAQC_MODELS" in info["models_error"]

    def test_get_model_info_with_custom_models(self):
        """Test get_model_info shows custom models are set."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["QAQC_MODELS"] = "custom-model-1,custom-model-2"
        
        info = ModelDetector.get_model_info()
        
        assert info["custom_models_set"] is True
        assert info["models"] == ["custom-model-1", "custom-model-2"]
