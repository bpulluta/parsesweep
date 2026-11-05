"""Unit tests for SchemaMetadata class."""

import json
import pytest
from pathlib import Path
from psweep.utils.schema_metadata import SchemaMetadata
from psweep.exceptions import SchemaMetadataError


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
                "document_type": "Test Document"
            },
            "identity": {
                "deduplication": {
                    "key_fields": ["field1", "field2"],
                    "ignore_fields": ["notes", "timestamp"]
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
            ("get_output_formats", ["csv", "excel"]),
            ("get_column_order", []),
            ("get_output_fields", ["id", "name"]),
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
    
    def test_metadata_overrides_merge_compilation_output(self, temp_schema_with_metadata):
        """Runtime metadata overrides should drive output settings."""
        meta = SchemaMetadata(
            temp_schema_with_metadata,
            metadata_overrides={
                "compilation": {
                    "output": {
                        "exclude_fields": ["runtime_only"],
                        "default_format": "csv",
                        "column_order": ["name"],
                    },
                },
            },
        )

        assert meta.get_output_exclude_fields() == ["runtime_only"]
        assert meta.get_output_formats() == ["csv"]
        assert meta.get_column_order() == ["name"]


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
                    "$metadata": {"extraction": {"context_objects": []}},
                    "type": "object",
                },
                "no_main_array.json",
                "main_data_array",
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
                    "main_data_array": "items"
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
        assert meta.get_main_data_array() == "items"
    
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
        
        assert SchemaMetadata(schema_path).get_main_data_array() == "items"


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


# --- Strict validation of schema-side compilation / identity blocks -----------
# Previously these blocks were unchecked: a mistyped key (e.g. key_field for
# key_fields, or expand_max_item for expand_max_items) silently reverted to a
# default and produced wrong output with no error. These lock the guards.


def _write_schema(tmp_path, metadata_extra):
    """Write a minimal-valid schema, merging metadata_extra onto $metadata."""
    metadata = {
        "extraction": {
            "main_data_array": "items",
        }
    }
    metadata.update(metadata_extra)
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": metadata,
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "title": "State"},
                        "county": {"type": "string"},
                        "units": {"type": ["string", "null"]},
                        "count": {"type": ["integer", "null"]},
                    },
                },
            }
        },
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


class TestCompilationBlockValidation:
    """Strict validation for schema-side compilation sub-blocks."""

    def test_valid_flattening_block_accepted(self, tmp_path):
        path = _write_schema(
            tmp_path,
            {
                "compilation": {
                    "flattening": {
                        "unit_fields": ["units"],
                        "unit_normalizations": {"ft": "feet"},
                    }
                }
            },
        )
        meta = SchemaMetadata(path)
        assert meta.get_flattening_config()["unit_fields"] == ["units"]

    def test_output_fields_titles_and_types_come_from_schema(self, tmp_path):
        meta = SchemaMetadata(_write_schema(tmp_path, {}))
        assert meta.get_output_fields() == ["state", "county", "units", "count"]
        assert meta.get_column_titles() == {"state": "State"}
        assert meta.get_field_types()["count"] == {"integer", "null"}

    def test_valid_output_and_normalization_accepted(self, tmp_path):
        path = _write_schema(
            tmp_path,
            {
                "compilation": {
                    "output": {
                        "default_format": "excel",
                        "annotation_column": "Notes",
                        "exclude_fields": ["internal"],
                        "freeze_columns": 2,
                        "auto_width": True,
                    },
                    "normalization": {
                        "state_column": "state",
                        "county_fips": {"state": "state", "county": "county"},
                    },
                }
            },
        )
        meta = SchemaMetadata(path)
        assert meta.get_output_formats() == ["excel"]
        assert meta.get_state_normalization_column() == "state"
        assert meta.get_county_fips_columns() == {"state": "state", "county": "county"}

    def test_normalization_is_opt_in(self, tmp_path):
        meta = SchemaMetadata(_write_schema(tmp_path, {}))
        assert meta.get_state_normalization_column() is None
        assert meta.get_county_fips_columns() is None

    def test_unrelated_compilation_keys_left_untouched(self, tmp_path):
        """Non-framework compilation keys (e.g. temporal_precision) still load."""
        path = _write_schema(
            tmp_path,
            {
                "compilation": {
                    "description": "notes",
                    "strategy": "temporal",
                    "temporal_precision": {"precision_field_pattern": "*_date"},
                }
            },
        )
        # Must not raise — only flattening/normalization/output are validated.
        SchemaMetadata(path)

    @pytest.mark.parametrize(
        ("compilation", "match"),
        [
            (
                {"flattening": {"expand_max_items": 20}},
                "unknown key",
            ),
            (
                {"flattening": {"unit_fields": "units"}},
                "unit_fields must be an array of strings",
            ),
            (
                {"flattening": {"unit_fields": ["unit"]}},
                "unknown schema field",
            ),
            (
                {"flattening": {"unit_fields": ["units"], "unit_normalizations": ["ft"]}},
                "unit_normalizations must map strings to strings",
            ),
            (
                {"flattening": {"unit_normalizations": {"ft": "feet"}}},
                "requires compilation.flattening.unit_fields",
            ),
            (
                {"output": {"column_renames": {"A": "b"}}},
                "unknown key",
            ),
            (
                {"normalization": {"state_column": "State"}},
                "unknown schema field",
            ),
            (
                {"normalization": {"county_fips": True}},
                "county_fips must be an object",
            ),
            (
                {"normalization": {"county_fips": {"state": "state", "county": "parish"}}},
                "unknown schema field",
            ),
            (
                {"output": {"annotaton_column": "Notes"}},
                "unknown key",
            ),
            (
                {"output": {"freeze_columns": -1}},
                "freeze_columns must be a non-negative integer",
            ),
            (
                {"normalization": {"state_colum": "State"}},
                "unknown key",
            ),
        ],
    )
    def test_invalid_compilation_block_rejected(
        self, tmp_path, compilation, match
    ):
        path = _write_schema(tmp_path, {"compilation": compilation})
        with pytest.raises(SchemaMetadataError, match=match):
            SchemaMetadata(path)


class TestIdentityDeduplicationValidation:
    """Strict validation for identity.deduplication (excluding CFR block)."""

    def test_valid_deduplication_block_accepted(self, tmp_path):
        path = _write_schema(
            tmp_path,
            {
                "identity": {
                    "deduplication": {
                        "key_fields": ["a", "b"],
                        "ignore_fields": ["notes"],
                        "partition_fields": ["state"],
                        "fuzzy_key_fields": [
                            "applies_to",
                            {"field": "condition", "normalize": "words"},
                        ],
                    }
                }
            },
        )
        meta = SchemaMetadata(path)
        assert meta.get_deduplication_key_fields() == ["a", "b"]
        assert meta.get_deduplication_fuzzy_fields() == [
            "applies_to",
            "condition",
        ]

    @pytest.mark.parametrize(
        ("dedup", "match"),
        [
            ({"key_field": ["a"]}, "unknown key"),
            ({"key_fields": "a"}, "key_fields must be"),
            ({"key_fields": [""]}, "must contain only non-empty"),
            ({"strategy": "latest"}, "unknown key"),
            ({"comparison_mode": "exact"}, "unknown key"),
            (
                {"fuzzy_key_fields": [{"normalize": "words"}]},
                "require a non-empty string 'field'",
            ),
        ],
    )
    def test_invalid_deduplication_block_rejected(self, tmp_path, dedup, match):
        path = _write_schema(tmp_path, {"identity": {"deduplication": dedup}})
        with pytest.raises(SchemaMetadataError, match=match):
            SchemaMetadata(path)
