"""Invalid selection repair is bounded, source-only and metered."""

import json
from types import SimpleNamespace

pytest_plugins = ["test_reconciliation"]


def test_required_nonnullable_selection_repaired_once(setup, monkeypatch):
    records, _inputs, calls, write, compiler, schema, path = setup
    schema["properties"]["items"]["items"]["properties"]["qualifier"] = {"type": "string"}
    path.write_text(json.dumps(schema))
    write("old", "A", "old")
    write("new", "B", "new")
    usage = {"calls": 0, "cost": 0.0}

    def extract(text, **_kwargs):
        request = json.loads(text)
        calls.append(request)
        usage["calls"] += 1
        usage["cost"] += 0.01
        if len(calls) == 1:
            return {"data": {"selections": {"answer": None}, "reason": "Uncertain"}}
        assert any("qualifier" in error for error in request["validation_errors"])
        chosen = next(o for o in request["observations"] if o["item"]["answer"]["value"] == "B")
        return {"data": {"selections": {"answer": chosen["id"]}, "reason": "Supported bundle"}}

    monkeypatch.setattr(
        "psweep.compilation.reconciliation.build_llm_client",
        lambda *a, **k: SimpleNamespace(usage=usage, extract=extract),
    )
    c = compiler()
    frame, _ = c.compile_from_directory(records)
    assert frame.iloc[0]["answer"] == "B" and frame.iloc[0]["qualifier"] == "new"
    assert len(calls) == 2 and c.status == "success"
    assert c.reconciliation_costs["llm_calls"] == 2
    assert c.reconciliation_costs["total_cost_usd"] == 0.02


def test_repeated_invalid_selection_stops_with_diagnostic(setup, monkeypatch):
    records, _inputs, calls, write, compiler, *_ = setup
    write("a", "A", "unit")
    write("b", "B", "other")

    def extract(text, **_kwargs):
        calls.append(json.loads(text))
        return {"data": {"selections": {"answer": "invented-id"}, "reason": "No"}}

    monkeypatch.setattr(
        "psweep.compilation.reconciliation.build_llm_client",
        lambda *a, **k: SimpleNamespace(usage={}, extract=extract),
    )
    c = compiler()
    frame, _ = c.compile_from_directory(records)
    assert len(calls) == 2 and c.status == "failed"
    reason = json.loads(frame.iloc[0]["evidence"])["_reconciliation"]["reason"]
    assert "selections.answer" in reason and "enum" in reason


def test_valid_abstention_does_not_trigger_repair(setup, monkeypatch):
    records, _inputs, calls, write, compiler, *_ = setup
    write("a", "A", "unit")
    write("b", "B", "other")

    def extract(text, **_kwargs):
        calls.append(json.loads(text))
        return {"data": {"selections": {"answer": None}, "reason": "Different scopes"}}

    monkeypatch.setattr(
        "psweep.compilation.reconciliation.build_llm_client",
        lambda *a, **k: SimpleNamespace(usage={}, extract=extract),
    )
    compiler().compile_from_directory(records)
    assert len(calls) == 1
