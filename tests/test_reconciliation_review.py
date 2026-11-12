"""Verified edge cases from independent reconciliation review."""

import json

import pytest

from psweep.compilation.data_compiler import DataCompiler
from psweep.utils.schema_metadata import SchemaMetadata

pytest_plugins = ["test_reconciliation"]


def test_roster_passthrough_cannot_overwrite_supported_fields(setup):
    records, inputs, calls, write, compiler, *_ = setup
    inputs["targets"][0]["answer"] = "roster note"
    write("a", "A", "unit")
    with pytest.raises(ValueError, match="collide"):
        compiler().compile_from_directory(records)
    assert not calls


def test_prompt_contains_required_output_contract_for_json_only_providers(setup):
    records, _inputs, calls, write, compiler, *_ = setup
    write("a", "A", "unit", "2024-01-01")
    write("b", "B", "other", "2025-01-01")
    compiler().compile_from_directory(records)
    assert calls[0]["response_schema"]["required"] == ["selections", "reason"]
    assert "Return only JSON" in calls[0]["response_instructions"]


def test_absent_optional_evidence_is_not_synthesized(setup):
    records, inputs, calls, _write, _compiler, schema, path = setup
    item = schema["properties"]["items"]["items"]
    item["required"] = ["name"]
    item["properties"]["qualifier"] = {"type": "string"}
    item["dependencies"] = {"answer": ["qualifier"]}
    path.write_text(json.dumps(schema))
    (records / "one.json").write_text(
        json.dumps(
            {
                "document": {"source_url": "https://example.org/local"},
                "payload": {"items": [{"name": "subject"}]},
            }
        )
    )
    c = DataCompiler(SchemaMetadata(path), inputs=inputs, verbose=False)
    frame, _ = c.compile_from_directory(records)
    assert not calls and c.status == "success"
    assert frame.iloc[0]["answer"] == "NA"


def test_local_supported_answers_count_as_usable_without_urls(setup):
    records, inputs, _calls, write, compiler, *_ = setup
    inputs["targets"].append({"name": "other"})
    inputs["reconciliation"]["max_context"] = 1
    write("local", "Supported", "unit")
    p = records / "local.json"
    data = json.loads(p.read_text())
    data["document"]["source_url"] = None
    p.write_text(json.dumps(data))
    for name, value in [("a", "A"), ("b", "B")]:
        write(name, value, "unit")
        p = records / f"{name}.json"
        data = json.loads(p.read_text())
        data["payload"]["items"][0]["name"] = "other"
        p.write_text(json.dumps(data))
    c = compiler()
    frame, _ = c.compile_from_directory(records)
    assert c.status == "partial_failure"
    assert c.roster_stats["targets_with_evidence"] == 1
    assert frame.iloc[0]["answer"] == "Supported" and frame.iloc[0]["sources"] == "NA"
