"""Tests for the extracted schema/config scaffolding core module."""

import json
from pathlib import Path

import pytest

from psweep.scaffolding import (
    ConfigValidationError,
    apply_main_array_field_selection,
    available_main_array_fields,
    build_schema_starter_from_reference,
    default_schema_output_path,
)


def _reference_schema() -> dict:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "title": "Widget Doc",
        "$metadata": {
            "domain": "Widgets",
            "version": "1.2.0",
            "extraction": {
                "main_data_array": "widgets",
                "context_objects": [],
                "display_name_template": "{widget_id}",
                "document_type": "Widget Catalog",
            },
            "identity": {
                "deduplication": {
                    "key_fields": ["widget_id"],
                    "ignore_fields": ["notes"],
                }
            },
        },
        "properties": {
            "widgets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "widget_id": {"type": "string", "examples": ["W1"]},
                        "price": {"type": "number"},
                        "notes": {"type": "string"},
                    },
                    "required": ["widget_id"],
                },
            }
        },
    }


def _write_reference(tmp_path: Path) -> Path:
    ref = tmp_path / "reference_schema.json"
    ref.write_text(json.dumps(_reference_schema()), encoding="utf-8")
    return ref


def test_build_schema_starter_from_reference_derives_lean_metadata(tmp_path):
    ref = _write_reference(tmp_path)
    starter = build_schema_starter_from_reference(
        ref,
        schema_name="gadget",
        domain_name=None,
        document_type=None,
    )

    meta = starter["$metadata"]
    assert meta["version"] == "0.1.0"
    assert meta["extraction"]["main_data_array"] == "widgets"
    # Inherited identity/deduplication carried over from the reference.
    assert meta["identity"]["deduplication"]["key_fields"] == ["widget_id"]
    # Example values are pruned out of the starter.
    widget_props = starter["properties"]["widgets"]["items"]["properties"]
    assert "examples" not in widget_props["widget_id"]
    # All main-array fields available before selection.
    assert available_main_array_fields(starter) == ["widget_id", "price", "notes"]


def test_build_schema_starter_field_selection_filters_fields_and_dedup(tmp_path):
    ref = _write_reference(tmp_path)
    starter = build_schema_starter_from_reference(
        ref,
        schema_name="gadget",
        domain_name="Gadgets",
        document_type="Gadget Catalog",
        include_fields=["widget_id", "price"],
    )
    assert available_main_array_fields(starter) == ["widget_id", "price"]
    dedup = starter["$metadata"]["identity"]["deduplication"]
    # 'notes' was dropped from selection -> dropped from ignore_fields too.
    assert "ignore_fields" not in dedup
    assert dedup["key_fields"] == ["widget_id"]


def test_apply_main_array_field_selection_rejects_unknown_fields():
    schema = _reference_schema()
    with pytest.raises(ConfigValidationError):
        apply_main_array_field_selection(schema, ["does_not_exist"])


def test_default_schema_output_path_is_domain_config_sibling(tmp_path):
    assert default_schema_output_path(tmp_path, "widgets") == (
        tmp_path / "config" / "widgets" / "schema.json"
    )
