"""Optional evidence reconciliation is atomic, selection-only and domain-neutral."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from psweep.compilation.data_compiler import DataCompiler
from psweep.config.runtime_config_loader import load_runtime_config_file
from psweep.utils.schema_metadata import SchemaMetadata


def evidence():
    return {
        "type": "object",
        "required": ["value", "quote"],
        "properties": {
            "value": {"type": ["string", "null"]},
            "quote": {"type": ["string", "null"]},
        },
    }


@pytest.fixture
def setup(tmp_path, monkeypatch):
    schema = {
        "$metadata": {
            "extraction": {"main_data_array": "items"},
            "identity": {"deduplication": {"key_fields": ["name"]}},
        },
        "type": "object",
        "properties": {
            "published": {"type": ["string", "null"]},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "answer", "qualifier"],
                    "properties": {
                        "name": {"type": "string"},
                        "answer": evidence(),
                        "qualifier": {"type": ["string", "null"]},
                    },
                },
            },
        },
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    records = tmp_path / "records"
    records.mkdir()
    inputs = {
        "targets": [{"name": "subject"}],
        "partition_by": ["name"],
        "domain": "fixture",
        "_reconciliation_cache": tmp_path / "cache",
        "model": "fixture-model",
        "reconciliation": {
            "enabled": True,
            "field_groups": [["answer", "qualifier"]],
            "source_date_field": "published",
            "instructions": "Choose supported evidence.",
        },
    }
    calls = []

    class Client:
        raw_model = "fixture-model"

        def __init__(self):
            self.usage = {
                "calls": 0,
                "cost": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "unknown_cost_calls": 0,
            }

        def extract(self, text, **kwargs):
            for name, value in {
                "calls": 1,
                "cost": 0.02,
                "input_tokens": 20,
                "output_tokens": 10,
            }.items():
                self.usage[name] += value
            calls.append(json.loads(text))
            observations = calls[-1]["observations"]
            chosen = max(observations, key=lambda o: o.get("source_date") or "")
            return {
                "data": {
                    "selections": {"answer": chosen["id"]},
                    "reason": "Choose the explicitly dated update.",
                }
            }

    monkeypatch.setattr(
        "psweep.compilation.reconciliation.build_llm_client", lambda *a, **k: Client()
    )
    monkeypatch.setattr(
        "psweep.compilation.reconciliation.resolve_llm_kwargs",
        lambda *a, **k: {"model": "fixture-model", "provider": "openai", "api_key": "fixture"},
    )

    def write(name, value, qualifier, published="2025-01-01", quote="source words", url=None):
        item = {
            "name": "subject",
            "answer": {"value": value, "quote": quote},
            "qualifier": qualifier,
        }
        (records / f"{name}.json").write_text(
            json.dumps(
                {
                    "document": {
                        "source_url": url or f"https://example.org/{name}",
                        "source_scope": "target",
                    },
                    "payload": {"published": published, "items": [item]},
                }
            )
        )

    def compiler():
        return DataCompiler(SchemaMetadata(path), inputs=deepcopy(inputs), verbose=False)

    return records, inputs, calls, write, compiler, schema, path


@pytest.mark.parametrize("allow_model", [True, False])
def test_agreement_does_not_hash_a_decision_request(setup, monkeypatch, allow_model):
    records, inputs, calls, write, compiler, *_ = setup
    write("a", "same", "unit")
    write("b", "same", "unit", quote="other supporting words")
    from psweep.compilation import reconciliation

    digest = reconciliation._digest
    hashed = []

    def capture(value):
        hashed.append(value)
        return digest(value)

    monkeypatch.setattr(reconciliation, "_digest", capture)
    compiler().compile_from_directory(records, allow_model=allow_model)
    assert not any("request" in value for value in hashed)
    assert not calls and not inputs["_reconciliation_cache"].exists()


@pytest.mark.parametrize(
    "payload,path,expected",
    [
        ({"metadata": {"published": "2024-02-29"}}, "metadata.published", "2024-02-29"),
        ({"published": {"value": "2024-03-01", "quote": "issued"}}, "published", "2024-03-01"),
        ({"published": "invalid"}, "published", None),
        ({"metadata": None}, "metadata.published", None),
        ({"published": "2024-03-01"}, None, None),
    ],
)
def test_source_date_uses_shared_nested_lookup(payload, path, expected):
    from psweep.compilation.reconciliation import _source_date

    assert _source_date(payload, path) == expected


def test_worker_client_is_reused_and_receipts_use_per_target_deltas(setup, monkeypatch):
    records, inputs, _calls, write, compiler, *_ = setup
    write("a", "A", "unit")
    write("b", "B", "other")
    from psweep.compilation import reconciliation

    inputs["targets"] = [{"name": f"subject-{index}"} for index in range(8)]
    for index in range(8):
        for variant in ("a", "b"):
            data = json.loads((records / f"{variant}.json").read_text())
            data["payload"]["items"][0]["name"] = f"subject-{index}"
            (records / f"{variant}-{index}.json").write_text(json.dumps(data))
    built = []

    class Client:
        def __init__(self):
            self.usage = {"calls": 0, "cost": 0.0}
            built.append(self)

        def extract(self, text, **kwargs):
            request = json.loads(text)
            self.usage["calls"] += 1
            self.usage["cost"] += 0.01
            chosen = request["observations"][0]
            return {"data": {"selections": {"answer": chosen["id"]}, "reason": "supported"}}

    monkeypatch.setattr(reconciliation, "build_llm_client", lambda *args, **kwargs: Client())
    monkeypatch.setattr(reconciliation, "DEFAULT_MAX_CONCURRENT", 1)
    compiled = compiler()
    frame, _ = compiled.compile_from_directory(records)
    assert len(built) == 1
    assert len(frame) == compiled.reconciliation_costs["llm_calls"] == 8
    assert compiled.reconciliation_costs["total_cost_usd"] == pytest.approx(0.08)
    receipts = list((inputs["_reconciliation_cache"] / "attempts").glob("*.json"))
    assert len(receipts) == 8
    for receipt in receipts:
        costs = json.loads(receipt.read_text())["costs"]
        assert costs["llm_calls"] == 1
        assert costs["total_cost_usd"] == pytest.approx(0.01)


def test_disputed_dry_run_never_hashes_a_decision_request(setup, monkeypatch):
    records, inputs, calls, write, compiler, *_ = setup
    write("a", "A", "unit")
    write("b", "B", "other")
    from psweep.compilation import reconciliation

    digest = reconciliation._digest

    def without_requests(value):
        assert "request" not in value
        return digest(value)

    monkeypatch.setattr(reconciliation, "_digest", without_requests)
    compiled = compiler()
    compiled.compile_from_directory(records, allow_model=False)
    assert compiled.reconciliation_stats["pending"] == 1
    assert not calls and not inputs["_reconciliation_cache"].exists()


def test_roster_projection_matches_with_reconciliation_enabled_or_disabled(setup):
    records, inputs, _calls, write, compiler, *_ = setup
    inputs["targets"] = [{"name": "subject", "context_note": "hint"}, {"name": "empty"}]
    write("a", "Supported", "unit")
    reconciled, _ = compiler().compile_from_directory(records)
    inputs["reconciliation"] = {"enabled": False}
    plain = compiler()
    ordinary, _ = plain.compile_from_directory(records)
    assert list(ordinary) == list(reconciled)
    for column in ("name", "answer", "qualifier", "context_note", "sources", "confidence"):
        assert ordinary[column].tolist() == reconciled[column].tolist()
    assert plain.roster_stats["roster_targets"] == len(reconciled)


@pytest.mark.parametrize("meaning", ["lifecycle", "equipment", "regulation"])
def test_atomic_choice_and_cached_decision(setup, meaning):
    records, inputs, calls, write, compiler, *_ = setup
    inputs["reconciliation"]["instructions"] = f"Resolve this {meaning} observation."
    write("old", "old", "coarse", "2024-01-01")
    write("new", "new", "exact", "2025-01-01")
    c = compiler()
    frame, _ = c.compile_from_directory(records)
    assert frame.iloc[0]["answer"] == "new"
    assert frame.iloc[0]["qualifier"] == "exact"
    assert len(calls) == 1
    assert c.status == "success"
    assert c.reconciliation_costs["total_cost_usd"] == 0.02
    ev = json.loads(frame.iloc[0]["evidence"])["answer"]
    assert ev["resolution"] == "resolved" and ev["conflict"]
    assert ev["source_date"] == "2025-01-01"
    c = compiler()
    frame, _ = c.compile_from_directory(records)
    assert len(calls) == 1
    assert c.reconciliation_stats["cached"] == 1
    assert c.reconciliation_costs["llm_calls"] == 0


def test_agreement_needs_no_provider_or_cache(setup):
    records, inputs, calls, write, compiler, *_ = setup
    write("a", "same", "unit")
    write("b", "same", "unit", quote="different supporting words")
    c = compiler()
    frame, _ = c.compile_from_directory(records)
    assert not calls
    assert frame.iloc[0]["answer"] == "same"
    assert not inputs["_reconciliation_cache"].exists()


def test_dry_run_never_calls_or_writes(setup):
    records, inputs, calls, write, compiler, *_ = setup
    write("a", "A", "unit")
    write("b", "B", "other")
    c = compiler()
    c.compile_from_directory(records, allow_model=False)
    assert not calls and not inputs["_reconciliation_cache"].exists()
    assert c.reconciliation_stats["pending"] == 1


def test_null_evidence_cannot_emit_orphan_qualifier(setup):
    records, _inputs, calls, write, compiler, *_ = setup
    write("a", None, "orphan", quote=None)
    c = compiler()
    frame, _ = c.compile_from_directory(records)
    assert frame.iloc[0]["answer"] == "NA" and frame.iloc[0]["qualifier"] == "NA"
    assert not calls


def test_oversized_competitors_abstain_without_call(setup):
    records, inputs, calls, write, compiler, *_ = setup
    inputs["reconciliation"]["max_context"] = 1
    write("a", "A", "unit")
    write("b", "B", "other")
    c = compiler()
    frame, _ = c.compile_from_directory(records)
    assert not calls
    assert c.status == "failed"
    assert frame.iloc[0]["answer"] == "NA"


def test_unknown_observation_id_is_unresolved(setup, monkeypatch):
    records, _inputs, _calls, write, compiler, *_ = setup
    write("a", "A", "unit")
    write("b", "B", "other")
    client = SimpleNamespace(
        usage={},
        extract=lambda **k: {
            "data": {"selections": {"answer": "invented-id"}, "reason": "invented"}
        },
    )
    monkeypatch.setattr(
        "psweep.compilation.reconciliation.build_llm_client", lambda *a, **k: client
    )
    c = compiler()
    frame, _ = c.compile_from_directory(records)
    assert c.status == "failed"
    assert frame.iloc[0]["answer"] == "NA"


def test_rows_mode_rejected_before_execution(setup):
    _records, inputs, calls, _write, compiler, *_ = setup
    inputs.pop("partition_by")
    with pytest.raises(ValueError, match="roster"):
        compiler()
    assert not calls


@pytest.mark.parametrize(
    "policy",
    [
        {"enabled": "true"},
        {"enabled": True, "unknown": 1},
        {"enabled": True, "field_groups": [["answer", "answer"]]},
        {"enabled": True, "max_context": True},
    ],
)
def test_yaml_policy_is_strict(tmp_path, policy):
    config = tmp_path / "run.json"
    config.write_text(json.dumps({"domain": "fixture", "compilation": {"reconciliation": policy}}))
    with pytest.raises(ValueError):
        load_runtime_config_file(config)
