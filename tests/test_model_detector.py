"""
Unit tests for QA/QC Model Detector.

Tests the model detection and configuration for multi-model QA/QC validation.
"""

import os
import pytest
from unittest.mock import patch


class TestModelDetector:
    """Tests for ModelDetector class."""

    @pytest.fixture(autouse=True)
    def clean_env(self):
        """Clean environment before each test."""
        # Save original values
        original_qaqc = os.environ.get("QAQC_MODELS")
        original_azure_key = os.environ.get("AZURE_OPENAI_API_KEY")
        original_azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        original_openai_key = os.environ.get("OPENAI_API_KEY")
        
        # Clear for test
        for var in ["QAQC_MODELS", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "OPENAI_API_KEY"]:
            if var in os.environ:
                del os.environ[var]
        
        yield
        
        # Restore
        if original_qaqc is not None:
            os.environ["QAQC_MODELS"] = original_qaqc
        elif "QAQC_MODELS" in os.environ:
            del os.environ["QAQC_MODELS"]
            
        if original_azure_key is not None:
            os.environ["AZURE_OPENAI_API_KEY"] = original_azure_key
        if original_azure_endpoint is not None:
            os.environ["AZURE_OPENAI_ENDPOINT"] = original_azure_endpoint
        if original_openai_key is not None:
            os.environ["OPENAI_API_KEY"] = original_openai_key

    def test_import_model_detector(self):
        """Test that ModelDetector can be imported."""
        from psweep.qa_qc.model_detector import ModelDetector
        assert ModelDetector is not None

    def test_import_from_init(self):
        """Test that ModelDetector can be imported from qa_qc package."""
        from psweep.qa_qc import ModelDetector
        assert ModelDetector is not None


class TestGetQaModels:
    """Tests for get_qa_models() method."""

    @pytest.fixture(autouse=True)
    def clean_env(self):
        """Clean environment before each test."""
        original_qaqc = os.environ.get("QAQC_MODELS")
        original_azure_key = os.environ.get("AZURE_OPENAI_API_KEY")
        original_azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        original_openai_key = os.environ.get("OPENAI_API_KEY")
        
        for var in ["QAQC_MODELS", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "OPENAI_API_KEY"]:
            if var in os.environ:
                del os.environ[var]
        
        yield
        
        if original_qaqc is not None:
            os.environ["QAQC_MODELS"] = original_qaqc
        elif "QAQC_MODELS" in os.environ:
            del os.environ["QAQC_MODELS"]
        if original_azure_key is not None:
            os.environ["AZURE_OPENAI_API_KEY"] = original_azure_key
        if original_azure_endpoint is not None:
            os.environ["AZURE_OPENAI_ENDPOINT"] = original_azure_endpoint
        if original_openai_key is not None:
            os.environ["OPENAI_API_KEY"] = original_openai_key

    def test_custom_models_two(self):
        """Test custom QAQC_MODELS with 2 models."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["QAQC_MODELS"] = "model-a,model-b"
        models = ModelDetector.get_qa_models()
        
        assert models == ["model-a", "model-b"]
        assert len(models) == 2

    def test_custom_models_three(self):
        """Test custom QAQC_MODELS with 3 models."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["QAQC_MODELS"] = "gpt-4o,gpt-4-turbo,gpt-3.5-turbo"
        models = ModelDetector.get_qa_models()
        
        assert models == ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]
        assert len(models) == 3

    def test_custom_models_five(self):
        """Test custom QAQC_MODELS with 5 models."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["QAQC_MODELS"] = "m1,m2,m3,m4,m5"
        models = ModelDetector.get_qa_models()
        
        assert models == ["m1", "m2", "m3", "m4", "m5"]
        assert len(models) == 5

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

    def test_custom_models_empty_falls_back_to_defaults(self):
        """Test empty QAQC_MODELS falls back to defaults."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["QAQC_MODELS"] = ""
        os.environ["OPENAI_API_KEY"] = "sk-test"  # Use OpenAI defaults
        
        models = ModelDetector.get_qa_models()
        
        assert models == ModelDetector.OPENAI_QA_MODELS

    def test_azure_defaults(self):
        """Test Azure OpenAI defaults are returned."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["AZURE_OPENAI_API_KEY"] = "azure-test-key"
        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://test.openai.azure.com"
        
        models = ModelDetector.get_qa_models()
        
        assert models == ModelDetector.AZURE_QA_MODELS

    def test_openai_defaults(self):
        """Test OpenAI defaults are returned when no Azure config."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["OPENAI_API_KEY"] = "sk-test"
        
        models = ModelDetector.get_qa_models()
        
        assert models == ModelDetector.OPENAI_QA_MODELS

    def test_defaults_have_at_least_two_models(self):
        """Test that both default model lists have at least 2 models."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        assert len(ModelDetector.AZURE_QA_MODELS) >= 2
        assert len(ModelDetector.OPENAI_QA_MODELS) >= 2


class TestGetProvider:
    """Tests for get_provider() method."""

    @pytest.fixture(autouse=True)
    def clean_env(self):
        """Clean environment before each test."""
        original_azure_key = os.environ.get("AZURE_OPENAI_API_KEY")
        original_azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
        
        for var in ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"]:
            if var in os.environ:
                del os.environ[var]
        
        yield
        
        if original_azure_key is not None:
            os.environ["AZURE_OPENAI_API_KEY"] = original_azure_key
        if original_azure_endpoint is not None:
            os.environ["AZURE_OPENAI_ENDPOINT"] = original_azure_endpoint

    def test_azure_provider(self):
        """Test Azure provider detection."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["AZURE_OPENAI_API_KEY"] = "azure-test-key"
        os.environ["AZURE_OPENAI_ENDPOINT"] = "https://test.openai.azure.com"
        
        provider = ModelDetector.get_provider()
        
        assert provider == "azure"

    def test_openai_provider_default(self):
        """Test OpenAI provider is default."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        provider = ModelDetector.get_provider()
        
        assert provider == "openai"

    def test_openai_when_only_key_no_endpoint(self):
        """Test OpenAI is returned when only Azure key but no endpoint."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["AZURE_OPENAI_API_KEY"] = "azure-test-key"
        # No endpoint set
        
        provider = ModelDetector.get_provider()
        
        assert provider == "openai"


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

    @pytest.fixture(autouse=True)
    def clean_env(self):
        """Clean environment before each test."""
        original = {
            "QAQC_MODELS": os.environ.get("QAQC_MODELS"),
            "AZURE_OPENAI_API_KEY": os.environ.get("AZURE_OPENAI_API_KEY"),
            "AZURE_OPENAI_ENDPOINT": os.environ.get("AZURE_OPENAI_ENDPOINT"),
            "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
        }
        
        for var in original:
            if var in os.environ:
                del os.environ[var]
        
        yield
        
        for var, val in original.items():
            if val is not None:
                os.environ[var] = val
            elif var in os.environ:
                del os.environ[var]

    def test_get_model_info_structure(self):
        """Test get_model_info returns expected structure."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["OPENAI_API_KEY"] = "sk-test"
        
        info = ModelDetector.get_model_info()
        
        assert "provider" in info
        assert "custom_models_set" in info
        assert "models" in info
        assert "has_azure_key" in info
        assert "has_openai_key" in info

    def test_get_model_info_with_custom_models(self):
        """Test get_model_info shows custom models are set."""
        from psweep.qa_qc.model_detector import ModelDetector
        
        os.environ["QAQC_MODELS"] = "custom-model-1,custom-model-2"
        
        info = ModelDetector.get_model_info()
        
        assert info["custom_models_set"] is True
        assert info["models"] == ["custom-model-1", "custom-model-2"]
