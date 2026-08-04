"""Unit tests for SchemaMetadata class."""

import json
import pytest
from pathlib import Path
from psweep.utils.schema_metadata import SchemaMetadata
from psweep.utils.exceptions import SchemaMetadataError


@pytest.fixture
def temp_schema_with_metadata(tmp_path):
    """Create a temporary schema file with full metadata."""
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": {
            "domain": "Test Domain",
            "version": "1.0.0",
            "description": "Test schema",
            "extraction": {
                "main_data_array": "test_items",
                "context_objects": ["metadata_obj", "info_obj"],
                "identifier_fields": ["metadata_obj.id", "metadata_obj.name"],
                "display_name_template": "{name} - {id}",
                "document_type": "Test Document"
            },
            "identity": {
                "deduplication": {
                    "key_fields": ["field1", "field2"],
                    "ignore_fields": ["notes", "timestamp"],
                    "strategy": "latest",
                    "comparison_mode": "fuzzy"
                }
            },
            "validation": {
                "required_fields": ["metadata_obj", "test_items"],
                "quality_checks": [
                    {
                        "field": "value",
                        "type": "numeric",
                        "range": [0, 100]
                    }
                ],
                "completeness_threshold": 0.85
            }
        },
        "type": "object",
        "properties": {
            "metadata_obj": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"}
                }
            },
            "test_items": {
                "type": "array",
                "items": {"type": "object"}
            }
        }
    }
    
    schema_path = tmp_path / "test_schema.json"
    with open(schema_path, 'w') as f:
        json.dump(schema, f)
    
    return schema_path


@pytest.fixture
def temp_schema_without_metadata(tmp_path):
    """Create a temporary schema file without metadata."""
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object"}
            }
        }
    }
    
    schema_path = tmp_path / "no_metadata_schema.json"
    with open(schema_path, 'w') as f:
        json.dump(schema, f)
    
    return schema_path


class TestSchemaMetadataBasics:
    """Test basic SchemaMetadata functionality."""
    
    def test_has_metadata_true(self, temp_schema_with_metadata):
        """Test has_metadata returns True when metadata exists."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.has_metadata() is True
    
    def test_has_metadata_false(self, temp_schema_without_metadata):
        """Test SchemaMetadata raises error when no metadata (v2.0+)."""
        with pytest.raises(SchemaMetadataError, match="missing required \\$metadata section"):
            SchemaMetadata(temp_schema_without_metadata)
    
    def test_schema_path_stored(self, temp_schema_with_metadata):
        """Test schema path is properly stored."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.schema_path == temp_schema_with_metadata
    
    def test_schema_loaded(self, temp_schema_with_metadata):
        """Test schema is properly loaded."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert "properties" in meta.schema
        assert "$metadata" in meta.schema


class TestExtractionMetadata:
    """Test extraction metadata accessor methods."""

    @pytest.mark.parametrize(
        ("method_name", "expected"),
        [
            ("get_main_data_array", "test_items"),
            ("get_deduplication_key_fields", ["field1", "field2"]),
            ("get_deduplication_ignore_fields", ["notes", "timestamp"]),
            ("get_deduplication_strategy", "latest"),
            ("get_comparison_mode", "fuzzy"),
            ("get_output_format", "excel"),
            ("get_column_order", []),
        ],
    )
    def test_schema_metadata_accessors(self, temp_schema_with_metadata, method_name, expected):
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert getattr(meta, method_name)() == expected


class TestCompilationMetadata:
    """Test compilation metadata accessor methods."""

    def test_get_output_exclude_fields(self, temp_schema_with_metadata):
        """Test getting runtime output exclude fields from metadata overrides."""
        meta = SchemaMetadata(
            temp_schema_with_metadata,
            metadata_overrides={
                "compilation": {
                    "output": {"exclude_fields": ["notes", "details"]}
                }
            },
        )
        assert meta.get_output_exclude_fields() == ["notes", "details"]
    
    def test_get_column_renames(self, temp_schema_with_metadata):
        """Test getting runtime output column rename mapping from metadata overrides."""
        meta = SchemaMetadata(
            temp_schema_with_metadata,
            metadata_overrides={
                "compilation": {
                    "output": {"column_renames": {"Name": "charge_name"}}
                }
            },
        )
        assert meta.get_column_renames() == {"Name": "charge_name"}

    def test_metadata_overrides_merge_compilation_output(self, temp_schema_with_metadata):
        """Runtime metadata overrides should drive output settings."""
        meta = SchemaMetadata(
            temp_schema_with_metadata,
            metadata_overrides={
                "compilation": {
                    "output": {
                        "exclude_fields": ["runtime_only"],
                        "default_format": "csv",
                        "column_renames": {"Name": "charge_name"},
                    },
                },
                "identity": {
                    "deduplication": {
                        "comparison_mode": "exact",
                    }
                },
            },
        )

        assert meta.get_output_exclude_fields() == ["runtime_only"]
        assert meta.get_output_format() == "csv"
        assert meta.get_column_renames() == {"Name": "charge_name"}
        assert meta.get_comparison_mode() == "exact"


class TestValidationMetadata:
    """Test validation metadata accessor methods."""
    
    def test_get_required_fields(self, temp_schema_with_metadata):
        """Test getting required fields."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.get_required_fields() == ["metadata_obj", "test_items"]
    
    def test_get_quality_checks(self, temp_schema_with_metadata):
        """Test getting quality checks."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        checks = meta.get_quality_checks()
        assert len(checks) == 1
        assert checks[0]["field"] == "value"
        assert checks[0]["type"] == "numeric"
    
    def test_get_completeness_threshold(self, temp_schema_with_metadata):
        """Test getting completeness threshold."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.get_completeness_threshold() == 0.85


class TestExtractionShapeNormalization:
    """Test normalization for array-like object-map outputs."""

    def test_extract_main_data_array_normalizes_numeric_key_object_map(
        self, temp_schema_with_metadata
    ):
        meta = SchemaMetadata(temp_schema_with_metadata)
        data = {
            "test_items": {
                "10": {"id": "c"},
                "2": {"id": "b"},
                "0": {"id": "a"},
            }
        }
        items = meta.extract_main_data_array(data)
        assert [item["id"] for item in items] == ["a", "b", "c"]

    def test_extract_main_data_array_rejects_non_numeric_object_map(
        self, temp_schema_with_metadata
    ):
        meta = SchemaMetadata(temp_schema_with_metadata)
        data = {"test_items": {"first": {"id": "a"}, "second": {"id": "b"}}}
        assert meta.extract_main_data_array(data) == []


class TestV2Requirements:
    """Test v2.0 strict validation requirements."""

    @pytest.mark.parametrize(
        ("schema_payload", "filename", "match"),
        [
            (
                {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "$metadata": {},
                    "type": "object",
                },
                "empty_metadata.json",
                "main_data_array",
            ),
            (
                {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "$metadata": {"domain": "Test"},
                    "type": "object",
                },
                "no_extraction.json",
                "main_data_array",
            ),
            (
                {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "$metadata": {"extraction": {"identifier_fields": ["id"]}},
                    "type": "object",
                },
                "no_main_array.json",
                "main_data_array",
            ),
            (
                {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "$metadata": {"extraction": {"main_data_array": "items"}},
                    "type": "object",
                },
                "no_identifiers.json",
                "identifier_fields",
            ),
        ],
    )
    def test_schema_validation_errors(self, tmp_path, schema_payload, filename, match):
        schema_path = tmp_path / filename
        schema_path.write_text(json.dumps(schema_payload), encoding="utf-8")
        with pytest.raises(SchemaMetadataError, match=match):
            SchemaMetadata(schema_path)
    
    def test_minimal_valid_metadata(self, tmp_path):
        """Test that minimal valid metadata works in v2.0."""
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$metadata": {
                "extraction": {
                    "main_data_array": "items",
                    "identifier_fields": ["id"]
                }
            },
            "type": "object",
            "properties": {
                "items": {"type": "array"}
            }
        }
        schema_path = tmp_path / "minimal_valid.json"
        with open(schema_path, 'w') as f:
            json.dump(schema, f)
        
        # Should not raise - this is minimal valid v2.0 schema
        meta = SchemaMetadata(schema_path)
        assert meta.has_metadata() is True
        assert meta.get_main_data_array() == "items"
        assert meta.get_identifier_fields() == ["id"]

    def test_legacy_compilation_deduplication_raises(self, tmp_path):
        """Legacy compilation.deduplication should fail fast with migration guidance."""
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$metadata": {
                "extraction": {
                    "main_data_array": "items",
                    "identifier_fields": ["id"],
                },
                "compilation": {
                    "deduplication": {
                        "key_fields": ["name"],
                    }
                },
            },
            "type": "object",
            "properties": {"items": {"type": "array"}},
        }
        schema_path = tmp_path / "legacy_dedup_schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")

        with pytest.raises(
            SchemaMetadataError, match="compilation.deduplication"
        ):
            SchemaMetadata(schema_path)
    
    def test_error_message_helpful_for_migration(self, temp_schema_without_metadata):
        """Test that error messages provide helpful migration guidance."""
        try:
            SchemaMetadata(temp_schema_without_metadata)
            assert False, "Should have raised SchemaMetadataError"
        except SchemaMetadataError as e:
            error_msg = str(e)
            # Check that error message mentions what's missing
            assert "$metadata" in error_msg or "metadata" in error_msg.lower()


class TestEdgeCases:
    """Test edge cases and error handling."""
    
    def test_partial_metadata_sections(self, tmp_path):
        """Test schema with partial metadata (only some sections)."""
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$metadata": {
                "extraction": {
                    "main_data_array": "items"
                    # Missing other extraction fields
                }
                # Missing compilation and validation sections
            },
            "type": "object",
            "properties": {}
        }
        
        schema_path = tmp_path / "partial_schema.json"
        with open(schema_path, 'w') as f:
            json.dump(schema, f)
        
        # In v2.0, schema with missing identifier_fields should raise error
        with pytest.raises(SchemaMetadataError, match="identifier_fields"):
            SchemaMetadata(schema_path)


# --- Phase 8: Tests for Expected Requirements Methods ---

class TestExpectedRequirementsMethods:
    """QA/QC expected_requirements live in run config lanes, not schema metadata."""

    def test_schema_has_no_qa_qc_section(self, temp_schema_with_metadata):
        """Schema metadata must not carry any qa_qc section."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert "qa_qc" not in meta.metadata

    def test_schema_metadata_has_no_qa_qc_methods(self, temp_schema_with_metadata):
        """SchemaMetadata exposes no QA/QC accessor methods."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert not hasattr(meta, "get_expected_requirements")
        assert not hasattr(meta, "get_expected_count_range")
        assert not hasattr(meta, "get_qa_qc_match_fields")
        assert not hasattr(meta, "get_qa_qc_compare_fields")
