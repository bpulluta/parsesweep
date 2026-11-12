"""Cross-field validity, cache safety and bounded evidence handling use neutral contracts."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from psweep.compilation.data_compiler import DataCompiler
from psweep.utils.schema_metadata import SchemaMetadata
from test_reconciliation import evidence

pytest_plugins = ["test_reconciliation"]


def test_cache_survives_multiple_reuses(setup):
    records, _inputs, calls, write, compiler, *_ = setup
    write("a", "A", "coarse", "2024-01-01")
    write("b", "B", "exact", "2025-01-01")
    for _ in range(4):
        assert compiler().compile_from_directory(records)[0].iloc[0]["answer"] == "B"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "policy",
    [
        {"field_groups": [["absent"]]},
        {"field_groups": [["name"]]},
        {"source_date_field": "absent.when"},
    ],
)
def test_invalid_schema_paths_fail_before_model(setup, policy):
    _records, inputs, calls, _write, compiler, *_ = setup
    inputs["reconciliation"].update(policy)
    with pytest.raises(ValueError):
        compiler()
    assert not calls


def test_corrupt_cache_is_not_authoritative(setup):
    records, inputs, calls, write, compiler, *_ = setup
    write("a", "A", "coarse", "2024-01-01")
    write("b", "B", "exact", "2025-01-01")
    compiler().compile_from_directory(records)
    cache = next((inputs["_reconciliation_cache"] / "decisions").glob("*.json"))
    cache.write_text("[]")
    assert compiler().compile_from_directory(records)[0].iloc[0]["answer"] == "B"
    assert len(calls) == 2


def test_changed_instructions_invalidate_decision(setup):
    records, inputs, calls, write, compiler, *_ = setup
    write("a", "A", "coarse", "2024-01-01")
    write("b", "B", "exact", "2025-01-01")
    compiler().compile_from_directory(records)
    inputs["reconciliation"]["instructions"] = "A different policy"
    compiler().compile_from_directory(records)
    assert len(calls) == 2


def test_metered_provider_failure_is_persisted_and_never_cached(setup, monkeypatch):
    records, inputs, _calls, write, compiler, *_ = setup
    write("a", "A", "unit")
    write("b", "B", "other")

    usage = {"calls": 0, "cost": 0.0, "unknown_cost_calls": 0}

    def fail(**_kwargs):
        usage.update(calls=usage["calls"] + 2, cost=usage["cost"] + 0.03, unknown_cost_calls=1)
        raise RuntimeError("private provider text must not appear")

    client = SimpleNamespace(usage=usage, extract=fail)
    monkeypatch.setattr(
        "psweep.compilation.reconciliation.build_llm_client", lambda *a, **k: client
    )
    c = compiler()
    frame, _ = c.compile_from_directory(records)
    assert c.status == "failed"
    assert c.reconciliation_costs["llm_calls"] == 2
    receipts = list((inputs["_reconciliation_cache"] / "attempts").glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["costs"]["unknown_cost_calls"] == 1
    assert "private provider text" not in frame.iloc[0]["evidence"]
    assert (
        json.loads(
            next((inputs["_reconciliation_cache"] / "decisions").glob("*.json")).read_text()
        )["selections"]
        is None
    )


def test_model_can_abstain_without_regenerating_facts(setup, monkeypatch):
    records, _inputs, _calls, write, compiler, *_ = setup
    write("a", "A", "unit")
    write("b", "B", "other")
    client = SimpleNamespace(
        usage={},
        extract=lambda **k: {
            "data": {
                "selections": {"answer": None},
                "reason": "The observations concern different phases.",
            }
        },
    )
    calls = []
    monkeypatch.setattr(
        "psweep.compilation.reconciliation.build_llm_client",
        lambda *a, **k: calls.append(1) or client,
    )
    c = compiler()
    frame, _ = c.compile_from_directory(records)
    assert c.status == "failed" and frame.iloc[0]["qualifier"] == "NA"
    assert json.loads(frame.iloc[0]["evidence"])["_reconciliation"]["state"] == "abstained"
    compiler().compile_from_directory(records)
    assert calls == [1]


@pytest.mark.parametrize("abstain", [False, True])
def test_invalid_composed_item_triggers_whole_target_reconciliation(setup, monkeypatch, abstain):
    records, inputs, _calls, _write, _compiler, schema, path = setup
    schema = deepcopy(schema)
    item = schema["properties"]["items"]["items"]
    item["properties"] = {"name": {"type": "string"}, "x": evidence(), "y": evidence()}
    item["required"] = ["name", "x", "y"]
    item["allOf"] = [
        {
            "not": {
                "properties": {
                    "x": {"properties": {"value": {"type": "string"}}},
                    "y": {"properties": {"value": {"type": "string"}}},
                }
            }
        }
    ]
    path.write_text(json.dumps(schema))
    inputs["reconciliation"]["field_groups"] = []
    for name, x, y in [("one", "X", None), ("two", None, "Y")]:
        (records / f"{name}.json").write_text(
            json.dumps(
                {
                    "document": {"source_url": f"https://example.org/{name}"},
                    "payload": {
                        "items": [
                            {
                                "name": "subject",
                                "x": {"value": x, "quote": x},
                                "y": {"value": y, "quote": y},
                            }
                        ]
                    },
                }
            )
        )
    seen = []

    def choose(text, **kwargs):
        prompt = json.loads(text)
        seen.append(prompt)
        chosen = next(o for o in prompt["observations"] if o["item"]["x"]["value"])
        return {
            "data": {
                "selections": {"x": None if abstain else chosen["id"], "y": None},
                "reason": "Choose one supported outcome.",
            }
        }

    monkeypatch.setattr(
        "psweep.compilation.reconciliation.build_llm_client",
        lambda *a, **k: SimpleNamespace(usage={}, extract=choose),
    )
    c = DataCompiler(SchemaMetadata(path), inputs=inputs, verbose=False)
    frame, _ = c.compile_from_directory(records)
    assert len(seen) == 1
    assert frame.iloc[0]["x"] == ("NA" if abstain else "X") and frame.iloc[0]["y"] == "NA"
    assert c.status == ("failed" if abstain else "success")
    if abstain:
        second = DataCompiler(SchemaMetadata(path), inputs=inputs, verbose=False)
        second.compile_from_directory(records)
        assert second.status == "failed" and len(seen) == 1


@pytest.mark.parametrize(
    "fields",
    [
        ("milestone", "granularity"),
        ("capacity", "unit"),
        ("permission", "condition"),
    ],
)
def test_groups_work_with_different_domain_field_names(setup, monkeypatch, fields):
    records, inputs, _calls, _write, _compiler, schema, path = setup
    answer, qualifier = fields
    item = schema["properties"]["items"]["items"]
    item["properties"] = {
        "name": {"type": "string"},
        answer: evidence(),
        qualifier: {"type": ["string", "null"]},
    }
    item["required"] = ["name", answer, qualifier]
    path.write_text(json.dumps(schema))
    inputs["reconciliation"]["field_groups"] = [[answer, qualifier]]
    for name, value, companion in [
        ("old", "old", "old qualifier"),
        ("new", "new", "new qualifier"),
    ]:
        (records / f"{name}.json").write_text(
            json.dumps(
                {
                    "document": {"source_url": f"https://example.org/{name}"},
                    "payload": {
                        "items": [
                            {
                                "name": "subject",
                                answer: {"value": value, "quote": value},
                                qualifier: companion,
                            }
                        ]
                    },
                }
            )
        )

    def select(text, **kwargs):
        request = json.loads(text)
        chosen = next(o for o in request["observations"] if o["item"][answer]["value"] == "new")
        return {"data": {"selections": {answer: chosen["id"]}, "reason": "Supported update."}}

    monkeypatch.setattr(
        "psweep.compilation.reconciliation.build_llm_client",
        lambda *a, **k: SimpleNamespace(usage={}, extract=select),
    )
    frame, _ = DataCompiler(
        SchemaMetadata(path), inputs=inputs, verbose=False
    ).compile_from_directory(records)
    assert frame.iloc[0][answer] == "new" and frame.iloc[0][qualifier] == "new qualifier"


def test_original_source_duplicates_do_not_multiply_observations(setup):
    records, _inputs, calls, write, compiler, *_ = setup
    write("one", "A", "unit", "2024-01-01", url="https://example.org/original")
    write("copy", "A", "unit", "2024-01-01", url="https://example.org/original")
    write("new", "B", "other", "2025-01-01")
    compiler().compile_from_directory(records)
    assert len(calls[0]["observations"]) == 2
