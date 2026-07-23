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
            "compilation": {
                "deduplication": {
                    "key_fields": ["field1", "field2"],
                    "ignore_fields": ["notes", "timestamp"],
                    "strategy": "latest",
                    "comparison_mode": "fuzzy"
                },
                "output": {
                    "default_format": "excel",
                    "column_order": ["id", "name", "value"],
                    "freeze_columns": 2,
                    "auto_width": True
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
    
    def test_get_main_data_array(self, temp_schema_with_metadata):
        """Test getting main data array key."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.get_main_data_array() == "test_items"


class TestCompilationMetadata:
    """Test compilation metadata accessor methods."""
    
    def test_get_deduplication_key_fields(self, temp_schema_with_metadata):
        """Test getting deduplication key fields."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.get_deduplication_key_fields() == ["field1", "field2"]
    
    def test_get_deduplication_ignore_fields(self, temp_schema_with_metadata):
        """Test getting deduplication ignore fields."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.get_deduplication_ignore_fields() == ["notes", "timestamp"]
    
    def test_get_deduplication_strategy(self, temp_schema_with_metadata):
        """Test getting deduplication strategy."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.get_deduplication_strategy() == "latest"
    
    def test_get_comparison_mode(self, temp_schema_with_metadata):
        """Test getting comparison mode."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.get_comparison_mode() == "fuzzy"
    
    def test_get_output_format(self, temp_schema_with_metadata):
        """Test getting output format."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.get_output_format() == "excel"

    def test_get_output_exclude_fields(self, temp_schema_with_metadata):
        """Test getting output exclude fields."""
        schema = json.loads(temp_schema_with_metadata.read_text())
        schema["$metadata"]["compilation"]["output"]["exclude_fields"] = ["notes", "details"]
        temp_schema_with_metadata.write_text(json.dumps(schema), encoding="utf-8")

        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.get_output_exclude_fields() == ["notes", "details"]
    
    def test_get_column_order(self, temp_schema_with_metadata):
        """Test getting column order."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.get_column_order() == ["id", "name", "value"]

    def test_get_column_renames(self, temp_schema_with_metadata):
        """Test getting output column rename mapping."""
        schema = json.loads(temp_schema_with_metadata.read_text())
        schema["$metadata"]["compilation"]["output"]["column_renames"] = {"Name": "charge_name"}
        temp_schema_with_metadata.write_text(json.dumps(schema), encoding="utf-8")

        meta = SchemaMetadata(temp_schema_with_metadata)
        assert meta.get_column_renames() == {"Name": "charge_name"}

    def test_metadata_overrides_merge_compilation_output(self, temp_schema_with_metadata):
        """Runtime metadata overrides should replace schema-owned compilation settings."""
        meta = SchemaMetadata(
            temp_schema_with_metadata,
            metadata_overrides={
                "compilation": {
                    "output": {
                        "exclude_fields": ["runtime_only"],
                        "default_format": "csv",
                        "column_renames": {"Name": "charge_name"},
                    },
                    "deduplication": {
                        "comparison_mode": "exact",
                    },
                }
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


class TestIdentifierExtraction:
    """Test identifier extraction from data."""
    
    def test_extract_identifier_from_data_with_metadata(self, temp_schema_with_metadata):
        """Test extracting identifier using metadata."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        data = {
            "metadata_obj": {
                "id": "12345",
                "name": "Test Item"
            },
            "test_items": []
        }
        identifier = meta.extract_identifier_from_data(data)
        assert identifier == "12345 - Test Item"
    
    def test_extract_identifier_partial_data(self, temp_schema_with_metadata):
        """Test extracting identifier with partial data."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        data = {
            "metadata_obj": {
                "id": "12345"
                # name is missing
            },
            "test_items": []
        }
        identifier = meta.extract_identifier_from_data(data)
        assert identifier == "12345"

class TestV2Requirements:
    """Test v2.0 strict validation requirements."""
    
    def test_schema_without_metadata_raises_error(self, temp_schema_without_metadata):
        """Test that schemas without $metadata raise SchemaMetadataError in v2.0."""
        with pytest.raises(SchemaMetadataError, match="missing required \\$metadata section"):
            SchemaMetadata(temp_schema_without_metadata)
    
    def test_schema_with_empty_metadata_raises_error(self, tmp_path):
        """Test that schemas with empty $metadata raise SchemaMetadataError."""
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$metadata": {},  # Empty metadata
            "type": "object"
        }
        schema_path = tmp_path / "empty_metadata.json"
        with open(schema_path, 'w') as f:
            json.dump(schema, f)
        
        with pytest.raises(SchemaMetadataError, match="main_data_array"):
            SchemaMetadata(schema_path)
    
    def test_schema_missing_extraction_section_raises_error(self, tmp_path):
        """Test that schemas missing extraction section raise SchemaMetadataError."""
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$metadata": {
                "domain": "Test",
                # Missing extraction section
            },
            "type": "object"
        }
        schema_path = tmp_path / "no_extraction.json"
        with open(schema_path, 'w') as f:
            json.dump(schema, f)
        
        with pytest.raises(SchemaMetadataError, match="main_data_array"):
            SchemaMetadata(schema_path)
    
    def test_schema_missing_main_data_array_raises_error(self, tmp_path):
        """Test that schemas missing main_data_array raise SchemaMetadataError."""
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$metadata": {
                "extraction": {
                    # Missing main_data_array
                    "identifier_fields": ["id"]
                }
            },
            "type": "object"
        }
        schema_path = tmp_path / "no_main_array.json"
        with open(schema_path, 'w') as f:
            json.dump(schema, f)
        
        with pytest.raises(SchemaMetadataError, match="main_data_array"):
            SchemaMetadata(schema_path)
    
    def test_schema_missing_identifier_fields_raises_error(self, tmp_path):
        """Test that schemas missing identifier_fields raise SchemaMetadataError."""
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$metadata": {
                "extraction": {
                    "main_data_array": "items"
                    # Missing identifier_fields
                }
            },
            "type": "object"
        }
        schema_path = tmp_path / "no_identifiers.json"
        with open(schema_path, 'w') as f:
            json.dump(schema, f)
        
        with pytest.raises(SchemaMetadataError, match="identifier_fields"):
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
    
    def test_nested_value_extraction_deep_nesting(self, temp_schema_with_metadata):
        """Test nested value extraction with deep nesting."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        data = {
            "level1": {
                "level2": {
                    "level3": {
                        "value": "deep_value"
                    }
                }
            }
        }
        value = meta._get_nested_value(data, "level1.level2.level3.value")
        assert value == "deep_value"
    
    def test_nested_value_extraction_missing_path(self, temp_schema_with_metadata):
        """Test nested value extraction with missing path."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        data = {
            "level1": {
                "level2": {}
            }
        }
        value = meta._get_nested_value(data, "level1.level2.level3.value")
        assert value is None
    
    def test_nested_value_extraction_non_dict(self, temp_schema_with_metadata):
        """Test nested value extraction when encountering non-dict."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        data = {
            "level1": "not_a_dict"
        }
        value = meta._get_nested_value(data, "level1.level2.value")
        assert value is None
    
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
    """Test Phase 8 expected requirements functionality with compound keys."""
    
    @pytest.fixture
    def temp_schema_with_qaqc(self, tmp_path):
        """Create schema with full QA/QC metadata using compound key approach."""
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$metadata": {
                "extraction": {
                    "main_data_array": "key_requirements",
                    "context_objects": ["document_info"],
                    "identifier_fields": ["document_info.state"]
                },
                "compilation": {
                    "deduplication": {
                        "key_fields": ["requirement_type"],
                        "ignore_fields": []
                    }
                },
                "qa_qc": {
                    "record_matching": {
                        "key_fields": ["requirement_type"]
                    },
                    "comparison": {
                        "primary_fields": ["value", "unit"]
                    },
                    "expected_requirements": [
                        "setback__property_line_ft",
                        "setback__residence_ft",
                        "noise__at_property_line_dba"
                    ],
                    "expected_count_range": [6, 20]
                }
            },
            "type": "object",
            "properties": {}
        }
        
        schema_path = tmp_path / "qaqc_schema.json"
        with open(schema_path, 'w') as f:
            json.dump(schema, f)
        
        return schema_path

    def test_get_expected_requirements(self, temp_schema_with_qaqc):
        """Test getting expected requirements as compound key strings."""
        meta = SchemaMetadata(temp_schema_with_qaqc)
        expected = meta.get_expected_requirements()
        
        assert len(expected) == 3
        assert "setback__property_line_ft" in expected
        assert "setback__residence_ft" in expected
        assert "noise__at_property_line_dba" in expected

    def test_get_expected_requirements_empty_when_not_specified(self, temp_schema_with_metadata):
        """Test expected requirements returns empty list when not specified."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        expected = meta.get_expected_requirements()
        
        assert expected == []

    def test_get_expected_count_range(self, temp_schema_with_qaqc):
        """Test getting expected count range."""
        meta = SchemaMetadata(temp_schema_with_qaqc)
        min_count, max_count = meta.get_expected_count_range()
        
        assert min_count == 6
        assert max_count == 20

    def test_get_expected_count_range_default(self, temp_schema_with_metadata):
        """Test expected count range returns default when not specified."""
        meta = SchemaMetadata(temp_schema_with_metadata)
        min_count, max_count = meta.get_expected_count_range()
        
        assert min_count == 1
        assert max_count == 100

    def test_get_qa_qc_match_fields_requires_explicit_config(self, temp_schema_with_metadata):
        """QA/QC match fields must be explicitly configured in qa_qc.record_matching.key_fields."""
        meta = SchemaMetadata(temp_schema_with_metadata)

        with pytest.raises(SchemaMetadataError, match=r"qa_qc\.record_matching\.key_fields"):
            meta.get_qa_qc_match_fields()
