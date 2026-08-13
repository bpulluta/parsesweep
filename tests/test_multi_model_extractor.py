"""
Tests for validation/multi_model_extractor.py

Tests the multi-model extraction functionality that runs the same document
through multiple AI models for QA/QC validation.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import asdict

from psweep.validation.multi_model_extractor import (
    run_multi_model_extraction,
    ModelExtractionResult,
)
from psweep.config.model_registry import ModelRegistry


def _registry(provider="openai", api_key="test-key", **extra):
    """Build a registry whose to_llm_kwargs yields deterministic LLM kwargs."""
    llm_config = {"provider": provider, "api_key": api_key}
    llm_config.update(extra)
    return ModelRegistry(llm_config=llm_config)


# Patch target - the import inside run_multi_model_extraction
EXTRACTOR_PATCH_PATH = "psweep.extraction.DocumentExtractor"


class TestModelExtractionResult:
    """Tests for the ModelExtractionResult dataclass."""
    
    def test_successful_result(self):
        """Test creating a successful extraction result."""
        result = ModelExtractionResult(
            model="gpt-4o",
            success=True,
            output_path=Path("/tmp/gpt-4o.json"),
            data={"items": [{"name": "test"}]},
            cost=0.0025,
            processing_time=1.5,
        )
        
        assert result.model == "gpt-4o"
        assert result.success is True
        assert result.output_path == Path("/tmp/gpt-4o.json")
        assert result.data == {"items": [{"name": "test"}]}
        assert result.cost == 0.0025
        assert result.processing_time == 1.5
        assert result.error is None
    
    def test_failed_result(self):
        """Test creating a failed extraction result."""
        result = ModelExtractionResult(
            model="gpt-4o",
            success=False,
            output_path=None,
            data=None,
            cost=0.0,
            processing_time=0.5,
            error="API rate limit exceeded",
        )
        
        assert result.model == "gpt-4o"
        assert result.success is False
        assert result.output_path is None
        assert result.data is None
        assert result.error == "API rate limit exceeded"
    
    def test_result_to_dict(self):
        """Test converting result to dictionary."""
        result = ModelExtractionResult(
            model="gpt-4o",
            success=True,
            output_path=Path("/tmp/test.json"),
            data={"test": "data"},
            cost=0.001,
            processing_time=1.0,
        )
        
        result_dict = asdict(result)
        assert result_dict["model"] == "gpt-4o"
        assert result_dict["success"] is True


class TestRunMultiModelExtraction:
    """Tests for the run_multi_model_extraction function."""
    
    @pytest.fixture
    def sample_schema(self):
        """Provide a sample schema for testing."""
        return {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "value": {"type": "number"}
                        }
                    }
                }
            }
        }
    
    @pytest.fixture
    def sample_text(self):
        """Provide sample document text."""
        return "This is a sample document with some test data. Item 1: value 100. Item 2: value 200."
    
    @pytest.fixture
    def mock_extraction_result(self):
        """Create a mock ExtractionResult."""
        mock_result = MagicMock()
        mock_result.data = {"items": [{"name": "Item 1", "value": 100}]}
        mock_result.cost = 0.0025
        mock_result.processing_time = 1.5
        mock_result.completeness_score = 0.9
        mock_result.validation_notes = []
        return mock_result
    
    @patch("psweep.extraction.DocumentExtractor")
    def test_successful_extraction_with_two_models(
        self, mock_extractor_class, sample_schema, sample_text, mock_extraction_result, tmp_path
    ):
        """Test successful extraction with two models."""
        # Setup mock
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = mock_extraction_result
        mock_extractor_class.return_value = mock_extractor
        
        # Run extraction
        results = run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="test_doc",
            schema=sample_schema,
            registry=_registry(),
            model_tiers=["gpt-4o", "gpt-3.5-turbo"],
            output_dir=tmp_path,
        )
        
        # Verify results
        assert len(results) == 2
        assert "gpt-4o" in results
        assert "gpt-3.5-turbo" in results
        assert results["gpt-4o"].success is True
        assert results["gpt-3.5-turbo"].success is True
        
        # Verify extractor was called twice with different models
        assert mock_extractor_class.call_count == 2
    
    @patch("psweep.extraction.DocumentExtractor")
    def test_output_directory_created(
        self, mock_extractor_class, sample_schema, sample_text, mock_extraction_result, tmp_path
    ):
        """Test that output directory is created correctly."""
        # Setup mock
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = mock_extraction_result
        mock_extractor_class.return_value = mock_extractor
        
        # Run extraction
        run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="my_document",
            schema=sample_schema,
            registry=_registry(),
            model_tiers=["gpt-4o"],
            output_dir=tmp_path,
        )
        
        # Verify directory structure
        expected_dir = tmp_path / "validation" / "my_document"
        assert expected_dir.exists()
        assert expected_dir.is_dir()
    
    @patch("psweep.extraction.DocumentExtractor")
    def test_json_files_saved(
        self, mock_extractor_class, sample_schema, sample_text, mock_extraction_result, tmp_path
    ):
        """Test that JSON files are saved for each model."""
        # Setup mock
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = mock_extraction_result
        mock_extractor_class.return_value = mock_extractor
        
        # Run extraction
        results = run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="test_doc",
            schema=sample_schema,
            registry=_registry(),
            model_tiers=["gpt-4o", "gpt-4-turbo"],
            output_dir=tmp_path,
        )
        
        # Verify JSON files exist
        output_dir = tmp_path / "validation" / "test_doc"
        assert (output_dir / "gpt-4o.json").exists()
        assert (output_dir / "gpt-4-turbo.json").exists()
        
        # Verify file content
        with open(output_dir / "gpt-4o.json") as f:
            data = json.load(f)
            assert data["contract_version"] == "1.0.0"
            assert data["lineage"]["model"] == "gpt-4o"
            assert data["quality"]["errors"] == []
            assert "payload" in data
            assert "items" in data["payload"]
    
    @patch("psweep.extraction.DocumentExtractor")
    def test_metadata_file_saved(
        self, mock_extractor_class, sample_schema, sample_text, mock_extraction_result, tmp_path
    ):
        """Test that metadata.json is saved with run info."""
        # Setup mock
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = mock_extraction_result
        mock_extractor_class.return_value = mock_extractor
        
        # Run extraction
        run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="test_doc",
            schema=sample_schema,
            registry=_registry(),
            model_tiers=["gpt-4o", "gpt-3.5-turbo"],
            output_dir=tmp_path,
        )
        
        # Verify metadata file
        metadata_path = tmp_path / "validation" / "test_doc" / "metadata.json"
        assert metadata_path.exists()
        
        with open(metadata_path) as f:
            metadata = json.load(f)
            assert metadata["document"] == "test_doc"
            assert metadata["models"] == ["gpt-4o", "gpt-3.5-turbo"]
            assert metadata["version"] == "2.0.0"
            assert metadata["status"] == "completed"
            assert "summary" in metadata
            assert metadata["summary"]["total_models"] == 2
            assert metadata["summary"]["successful"] == 2

    @patch("psweep.extraction.DocumentExtractor")
    def test_runtime_artifact_written_to_model_output_metadata(
        self, mock_extractor_class, sample_schema, sample_text, mock_extraction_result, tmp_path
    ):
        """Test that runtime artifact lineage is included in per-model extraction record."""
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = mock_extraction_result
        mock_extractor_class.return_value = mock_extractor

        runtime_artifact = {
            "artifact_id": "artifact://runtime/abc123def4567890",
            "contract_versions": {
                "extraction_record": "1.0.0",
                "modules_catalog": "1.0.0",
            },
            "lineage": {
                "artifact_id": "artifact://runtime/abc123def4567890",
                "profile_id": "default",
                "pack_name": "tariffs",
                "pack_version": "1.0.0",
                "compiled_at": "2026-03-24T12:00:00Z",
            },
        }

        run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="test_doc",
            schema=sample_schema,
            registry=_registry(),
            model_tiers=["gpt-4o"],
            output_dir=tmp_path,
            runtime_artifact=runtime_artifact,
            run_id="run://abc123def4567890",
        )

        output_path = tmp_path / "validation" / "test_doc" / "gpt-4o.json"
        with open(output_path) as f:
            data = json.load(f)

        assert data["contract_version"] == "1.0.0"
        assert data["lineage"]["run_id"] == "run://abc123def4567890"
        assert data["lineage"]["artifact_id"] == runtime_artifact["artifact_id"]
        assert data["lineage"]["profile_id"] == "default"
        assert data["lineage"]["provider"] == "openai"
        assert data["payload"]["items"][0]["name"] == "Item 1"
        assert data["processing_metrics"]["cost_usd"] == mock_extraction_result.cost

    @patch("psweep.extraction.DocumentExtractor")
    def test_runtime_artifact_written_to_run_metadata(
        self, mock_extractor_class, sample_schema, sample_text, mock_extraction_result, tmp_path
    ):
        """Test that runtime artifact lineage is included in metadata.json."""
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = mock_extraction_result
        mock_extractor_class.return_value = mock_extractor

        runtime_artifact = {
            "artifact_id": "artifact://runtime/abc123def4567890",
            "contract_versions": {
                "extraction_record": "1.0.0",
                "modules_catalog": "1.0.0",
            },
            "lineage": {
                "artifact_id": "artifact://runtime/abc123def4567890",
                "profile_id": "default",
                "pack_name": "tariffs",
                "pack_version": "1.0.0",
                "compiled_at": "2026-03-24T12:00:00Z",
            },
        }

        run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="test_doc",
            schema=sample_schema,
            registry=_registry(),
            model_tiers=["gpt-4o", "gpt-3.5-turbo"],
            output_dir=tmp_path,
            runtime_artifact=runtime_artifact,
            run_id="run://abc123def4567890",
        )

        metadata_path = tmp_path / "validation" / "test_doc" / "metadata.json"
        with open(metadata_path) as f:
            metadata = json.load(f)

        assert metadata["run_id"] == "run://abc123def4567890"
        assert metadata["artifact_id"] == runtime_artifact["artifact_id"]
        assert metadata["lineage"]["profile_id"] == "default"
        assert metadata["lineage"]["run_id"] == "run://abc123def4567890"
        assert metadata["contract_versions"]["extraction_record"] == "1.0.0"
    
    @patch("psweep.extraction.DocumentExtractor")
    def test_partial_failure(
        self, mock_extractor_class, sample_schema, sample_text, mock_extraction_result, tmp_path
    ):
        """Test handling when one model fails."""
        # Setup mock to succeed for first, fail for second
        mock_extractor = MagicMock()
        mock_extractor.extract.side_effect = [
            mock_extraction_result,
            Exception("API error: Model not available"),
        ]
        mock_extractor_class.return_value = mock_extractor
        
        # Run extraction
        results = run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="test_doc",
            schema=sample_schema,
            registry=_registry(),
            model_tiers=["gpt-4o", "gpt-nonexistent"],
            output_dir=tmp_path,
        )
        
        # Verify mixed results
        assert results["gpt-4o"].success is True
        assert results["gpt-nonexistent"].success is False
        assert "API error" in results["gpt-nonexistent"].error
        
        # Verify metadata shows partial status
        metadata_path = tmp_path / "validation" / "test_doc" / "metadata.json"
        with open(metadata_path) as f:
            metadata = json.load(f)
            assert metadata["status"] == "partial"
            assert metadata["summary"]["successful"] == 1
            assert metadata["summary"]["failed"] == 1
            assert metadata["summary"]["total_errors"] == 1
            assert metadata["errors"]["by_category"] == {"internal": 1}
            assert metadata["model_errors"]["gpt-nonexistent"]["code"] == "unexpected_processing_error"
    
    @patch("psweep.extraction.DocumentExtractor")
    def test_azure_provider_config(
        self, mock_extractor_class, sample_schema, sample_text, mock_extraction_result, tmp_path
    ):
        """Test that Azure provider config is passed correctly."""
        # Setup mock
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = mock_extraction_result
        mock_extractor_class.return_value = mock_extractor
        
        # Run extraction with Azure config
        run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="test_doc",
            schema=sample_schema,
            registry=_registry(provider="azure", api_key="azure-key", azure_endpoint="https://test.openai.azure.com/", azure_api_version="2024-02-15-preview"),
            model_tiers=["compassop-gpt-4o"],
            output_dir=tmp_path,
        )
        
        # Verify extractor was created with Azure params
        mock_extractor_class.assert_called_once()
        call_kwargs = mock_extractor_class.call_args.kwargs
        assert call_kwargs["provider"] == "azure"
        assert call_kwargs["azure_endpoint"] == "https://test.openai.azure.com/"
        assert call_kwargs["azure_api_version"] == "2024-02-15-preview"
    
    @patch("psweep.extraction.DocumentExtractor")
    def test_model_name_sanitization(
        self, mock_extractor_class, sample_schema, sample_text, mock_extraction_result, tmp_path
    ):
        """Test that model names with special characters are sanitized for filenames."""
        # Setup mock
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = mock_extraction_result
        mock_extractor_class.return_value = mock_extractor
        
        # Run extraction with model name containing special chars
        results = run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="test_doc",
            schema=sample_schema,
            registry=_registry(provider="azure"),
            model_tiers=["azure/gpt-4o"],
            output_dir=tmp_path,
        )
        
        # Verify file was created with sanitized name
        output_dir = tmp_path / "validation" / "test_doc"
        assert (output_dir / "azure-gpt-4o.json").exists()  # Slash replaced with dash
    
    @patch("psweep.extraction.DocumentExtractor")
    def test_cost_tracking(
        self, mock_extractor_class, sample_schema, sample_text, tmp_path
    ):
        """Test that costs are tracked correctly across models."""
        # Setup mock with different costs per model
        mock_result_1 = MagicMock()
        mock_result_1.data = {"items": []}
        mock_result_1.cost = 0.01
        mock_result_1.processing_time = 1.0
        mock_result_1.completeness_score = 0.9
        mock_result_1.validation_notes = []
        
        mock_result_2 = MagicMock()
        mock_result_2.data = {"items": []}
        mock_result_2.cost = 0.005
        mock_result_2.processing_time = 0.5
        mock_result_2.completeness_score = 0.85
        mock_result_2.validation_notes = []
        
        mock_extractor = MagicMock()
        mock_extractor.extract.side_effect = [mock_result_1, mock_result_2]
        mock_extractor_class.return_value = mock_extractor
        
        # Run extraction
        results = run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="test_doc",
            schema=sample_schema,
            registry=_registry(),
            model_tiers=["gpt-4o", "gpt-3.5-turbo"],
            output_dir=tmp_path,
        )
        
        # Verify individual costs
        assert results["gpt-4o"].cost == 0.01
        assert results["gpt-3.5-turbo"].cost == 0.005
        
        # Verify total cost in metadata
        metadata_path = tmp_path / "validation" / "test_doc" / "metadata.json"
        with open(metadata_path) as f:
            metadata = json.load(f)
            assert metadata["summary"]["total_cost_usd_incurred_this_run"] == pytest.approx(0.015, rel=0.01)
    
    @patch("psweep.extraction.DocumentExtractor")
    def test_extract_called_correctly(
        self, mock_extractor_class, sample_schema, sample_text, mock_extraction_result, tmp_path
    ):
        """Test that extract is called correctly without old enable_validation param."""
        # Setup mock
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = mock_extraction_result
        mock_extractor_class.return_value = mock_extractor
        
        # Run extraction
        run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="test_doc",
            schema=sample_schema,
            registry=_registry(),
            model_tiers=["gpt-4o"],
            output_dir=tmp_path,
        )
        
        # Verify extract was called with just text and schema (no enable_validation param)
        mock_extractor.extract.assert_called_once()
        call_kwargs = mock_extractor.extract.call_args.kwargs
        assert "text" in call_kwargs
        assert "schema" in call_kwargs
        # enable_validation param no longer exists - verify it's not passed
        assert "enable_validation" not in call_kwargs

    @patch("psweep.extraction.DocumentExtractor")
    def test_timeout_is_passed_to_document_extractor(
        self, mock_extractor_class, sample_schema, sample_text, mock_extraction_result, tmp_path
    ):
        """Test that configured timeout is threaded into each model extractor."""
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = mock_extraction_result
        mock_extractor_class.return_value = mock_extractor

        run_multi_model_extraction(
            doc_text=sample_text,
            doc_name="test_doc",
            schema=sample_schema,
            registry=_registry(),
            model_tiers=["gpt-4o", "gpt-3.5-turbo"],
            output_dir=tmp_path,
            timeout_seconds=600,
        )

        assert mock_extractor_class.call_count == 2
        for call in mock_extractor_class.call_args_list:
            assert call.kwargs["timeout"] == 600
