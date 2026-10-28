"""Schema-aware synthesis validation happens before paid or destructive work."""

import json
from unittest.mock import Mock

import pytest
import yaml
from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.config.runtime_config_loader import RuntimeConfigError, resolve_compilation_metadata


@pytest.fixture
def synthesis_schema(tmp_path):
    schema = {
        "type": "object",
        "properties": {
            "catalog": {"type": "object", "properties": {
                "id": {"type": "string"}, "url": {"type": "string"},
            }},
            "observations": {"type": "array", "items": {"type": "object", "properties": {
                "price": {"type": ["number", "null"]},
                "quote": {"type": "string"},
            }}},
        },
        "$metadata": {"extraction": {
            "main_data_array": "observations", "context_objects": ["catalog"],
            "identifier_fields": ["catalog.id"],
        }},
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    return path


def _synthesis():
    return {"enabled": True, "group_by": ["catalog.id"], "citation_field": "catalog.url",
            "reconcile_fields": [{"field": "price", "evidence": "quote"}],
            "narrative_fields": ["generated_summary"]}


@pytest.mark.parametrize("setting", ["group_by", "citation_field", "item_array", "reconcile_fields"])
def test_schema_paths_validated_before_llm_factory(tmp_path, synthesis_schema, monkeypatch, setting):
    syn = _synthesis()
    syn[setting] = [{"field": "typo"}] if setting == "reconcile_fields" else (["catalog.typo"] if setting == "group_by" else "typo")
    cfg = tmp_path / "run.yaml"
    cfg.write_text(yaml.safe_dump({"domain": "synthetic", "compilation": {
        "schema": str(synthesis_schema), "input_dir": str(tmp_path / "input"),
        "output_dir": str(tmp_path / "output"), "synthesis": syn,
    }}))
    factory = Mock(side_effect=AssertionError("must not call provider"))
    monkeypatch.setattr("psweep.extraction.llm_factory.build_llm_client", factory)
    result = CliRunner().invoke(cli, ["compile", "--config", str(cfg), "--fresh"])
    assert result.exit_code != 0
    assert "typo" in result.output
    factory.assert_not_called()
    assert not (tmp_path / "output").exists()


def test_generated_narrative_output_is_not_required_in_extraction_schema(synthesis_schema):
    metadata = resolve_compilation_metadata({"schema": str(synthesis_schema), "synthesis": _synthesis()})
    assert metadata.metadata["compilation"]["synthesis"]["narrative_fields"] == ["generated_summary"]


def test_schema_owned_synthesis_gets_same_validation(synthesis_schema):
    schema = json.loads(synthesis_schema.read_text())
    syn = _synthesis()
    syn["reconcile_fields"][0]["evidence"] = "missing_quote"
    schema["$metadata"]["compilation"] = {"synthesis": syn}
    synthesis_schema.write_text(json.dumps(schema))
    with pytest.raises(RuntimeConfigError, match="missing_quote"):
        resolve_compilation_metadata({"schema": str(synthesis_schema)})


def test_synthesis_payload_paths_need_not_be_compilation_context(synthesis_schema):
    schema = json.loads(synthesis_schema.read_text())
    schema["properties"]["applicability"] = {"type": "object", "properties": {"relevant": {"type": "boolean"}}}
    schema["$metadata"]["extraction"]["context_objects"] = []
    synthesis_schema.write_text(json.dumps(schema))
    syn = _synthesis()
    syn["relevance_flag"] = "applicability.relevant"
    assert resolve_compilation_metadata({"schema": str(synthesis_schema), "synthesis": syn})


def test_synthesis_group_by_rejects_item_only_alias(synthesis_schema):
    syn = _synthesis()
    syn["group_by"] = ["price"]
    with pytest.raises(RuntimeConfigError, match="price"):
        resolve_compilation_metadata({"schema": str(synthesis_schema), "synthesis": syn})
