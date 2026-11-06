"""Roster-anchored extraction through the shared pipeline worker (offline)."""

from __future__ import annotations

import csv
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from psweep.config import RuntimeConfigError
from psweep.config.runtime_config_loader import load_runtime_config_file
from psweep.discovery.targets import identify_target
from psweep.extraction import document_utils
from psweep.pipeline import _execute_extraction, _prepare_extraction

FIELDS = ["state", "ahj"]
TARGETS = [
    {"state": "AZ", "ahj": "Pima County"},
    {"state": "CO", "ahj": "Boulder County"},
]
SHARED_TEXT = (
    "Statewide solar permitting survey\n"
    "Pima County, AZ: residential permits are submitted online.\n"
)


def _evidence():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["value", "quote"],
        "properties": {
            "value": {"type": ["string", "null"]},
            "quote": {"type": ["string", "null"]},
        },
    }


def _item(state, ahj, quote):
    return {"state": state, "ahj": ahj, "online": {"value": "Yes", "quote": quote}}


class FakeLLM:
    """Answer by document content and record every call."""

    def __init__(self):
        self.texts = []
        self.lock = threading.Lock()

    def __call__(self, text, schema, **_kwargs):
        with self.lock:
            self.texts.append(text)
        if "Pima fee schedule" in text:
            items = [
                _item("AZ", "Pima County", "fabricated quote"),
                _item("AZ", "City of Tucson", "Pima fee schedule"),
            ]
        else:
            items = [
                _item("AZ", "Pima County", "residential permits are submitted online"),
                _item("AZ", "Nowhere County", "residential permits are submitted online"),
            ]
        return {
            "data": {"jurisdictions": items},
            "cost": 0.01,
            "input_tokens": 5,
            "output_tokens": 2,
        }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    discovered = tmp_path / "discovered"
    curated = discovered / "curated"
    target_doc = curated / "AZ" / "Pima County" / "fees.txt"
    shared_doc = curated / "_shared" / "survey.txt"
    for doc, text in ((target_doc, "Pima fee schedule: $250."), (shared_doc, SHARED_TEXT)):
        doc.parent.mkdir(parents=True)
        doc.write_text(text, encoding="utf-8")
    with (discovered / "download_index.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "path",
                "relative_path",
                "source_scope",
                "target_id",
                "target_metadata",
                "url",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "path": str(target_doc),
                "relative_path": "AZ/Pima County/fees.txt",
                "source_scope": "target",
                "target_id": identify_target(TARGETS[0], FIELDS),
                "target_metadata": json.dumps(TARGETS[0]),
                "url": "https://pima.example/fees",
            }
        )
        writer.writerow(
            {
                "path": str(shared_doc),
                "relative_path": "_shared/survey.txt",
                "source_scope": "shared",
                "url": "https://state.example/survey",
            }
        )
    targets = tmp_path / "targets.csv"
    _write_targets(targets, TARGETS)
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {"main_data_array": "jurisdictions"},
                    "identity": {"deduplication": {"key_fields": FIELDS}},
                },
                "type": "object",
                "additionalProperties": False,
                "required": ["jurisdictions"],
                "properties": {
                    "jurisdictions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["state", "ahj", "online"],
                            "properties": {
                                "state": {"type": "string"},
                                "ahj": {"type": "string"},
                                "online": _evidence(),
                            },
                        },
                    }
                },
            }
        )
    )
    cfg = {"provider": "openai", "model": "gpt-fixture", "api_key": "fixture"}
    monkeypatch.setattr("psweep.utils.config.get_config", lambda: SimpleNamespace(llm_config=cfg))
    llm = FakeLLM()
    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", llm)
    inputs = {
        "path": str(curated),
        "schema": str(schema),
        "output": str(tmp_path / "records"),
        "llm_config": cfg,
        "targets": str(targets),
        "partition_by": FIELDS,
    }
    return SimpleNamespace(
        inputs=inputs,
        llm=llm,
        shared=shared_doc,
        targets=targets,
        records=tmp_path / "records",
        curated=curated,
        index=discovered / "download_index.csv",
    )


def _write_targets(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _run(inputs):
    result = _execute_extraction(_prepare_extraction(inputs))
    assert result.exit_code == 0, result.errors
    return result


def _record(ws, relative):
    return json.loads((ws.records / relative).read_text())


def test_shared_doc_keeps_only_roster_candidates(workspace):
    _run(workspace.inputs)
    record = _record(workspace, "_shared/survey.json")
    prompt = next(text for text in workspace.llm.texts if "Statewide solar" in text)
    assert "ROSTER CANDIDATES" in prompt
    assert '"ahj": "Pima County"' in prompt and "Boulder" not in prompt
    assert "SOURCE_URL: https://state.example/survey" in prompt
    assert [item["ahj"] for item in record["payload"]["jurisdictions"]] == ["Pima County"]
    assert record["quality"]["off_candidate_items"] == 1
    assert record["quality"]["unverified_quotes"] == 0
    assert record["document"]["source_document_id"] == "_shared/survey.txt"
    assert record["document"]["source_scope"] == "shared"
    assert record["document"]["source_url"] == "https://state.example/survey"


def test_target_doc_keeps_only_its_own_target(workspace):
    _run(workspace.inputs)
    record = _record(workspace, "AZ/Pima County/fees.json")
    prompt = next(text for text in workspace.llm.texts if "Pima fee schedule" in text)
    block = prompt.split("=== END ROSTER CANDIDATES ===")[0]
    assert '"ahj": "Pima County"' in block and "Boulder" not in block
    assert "SOURCE_URL: https://pima.example/fees" in prompt
    (item,) = record["payload"]["jurisdictions"]
    assert (item["state"], item["ahj"]) == ("AZ", "Pima County")
    assert item["online"] == {"value": None, "quote": None}
    assert record["quality"]["off_candidate_items"] == 1
    assert record["quality"]["unverified_quotes"] == 1
    assert (
        'unverified quote at jurisdictions[0].online: "fabricated quote"'
        in record["quality"]["warnings"]
    )
    assert record["document"]["target_id"] == identify_target(TARGETS[0], FIELDS)


def test_target_items_match_roster_after_value_normalization(workspace, monkeypatch):
    def answer(_client, text, schema, **_kwargs):
        items = [_item("az", "PIMA  COUNTY,", "Pima fee schedule")]
        return {
            "data": {"jurisdictions": items},
            "cost": 0.0,
            "input_tokens": 1,
            "output_tokens": 1,
        }

    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", answer)
    _run(workspace.inputs)
    record = _record(workspace, "AZ/Pima County/fees.json")
    assert len(record["payload"]["jurisdictions"]) == 1
    assert record["quality"]["off_candidate_items"] == 0


def test_target_missing_from_roster_makes_no_call(workspace):
    _write_targets(workspace.targets, TARGETS[1:])
    _run(workspace.inputs)
    record = _record(workspace, "AZ/Pima County/fees.json")
    assert record["quality"]["status"] == "no_roster_match"
    assert record["payload"]["jurisdictions"] == []
    assert workspace.llm.texts == []


def test_roster_identity_edit_reextracts_target_doc(workspace):
    _run(workspace.inputs)
    _write_targets(workspace.targets, [{"state": "AZ", "ahj": "Pima Co"}, TARGETS[1]])
    _run(workspace.inputs)
    record = _record(workspace, "AZ/Pima County/fees.json")
    assert record["quality"]["status"] == "no_roster_match"


def _add_doc(ws, relative, text, scope=None):
    doc = ws.curated / relative
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text, encoding="utf-8")
    if scope:
        with ws.index.open("a", newline="") as handle:
            csv.writer(handle).writerow([str(doc), relative, scope, "", "", ""])


def _failures(result):
    return {r["file"]: r["error"] for r in result.records if not r["success"]}


def test_doc_without_index_row_fails_alone(workspace):
    _add_doc(workspace, "AZ/Pima County/orphan.txt", "Orphan text.")
    result = _execute_extraction(_prepare_extraction(workspace.inputs))
    assert result.successful == 2
    assert "No download_index row for AZ/Pima County/orphan.txt" in _failures(result)["orphan.txt"]


def test_unreadable_shared_doc_fails_alone_and_reuse_survives(workspace):
    _run(workspace.inputs)
    _add_doc(workspace, "_shared/blank.txt", "   ", scope="shared")
    result = _execute_extraction(_prepare_extraction(workspace.inputs))
    assert "Roster candidate scan failed" in _failures(result)["blank.txt"]
    assert result.successful == 2 and len(workspace.llm.texts) == 2


def test_interrupt_cancels_queued_documents(workspace, monkeypatch):
    calls = []

    def interrupt(_client, text, schema, **_kwargs):
        calls.append(text)
        raise KeyboardInterrupt

    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", interrupt)
    for number in range(8):
        _add_doc(workspace, f"bulk/doc{number}.txt", f"Document {number}.")
    inputs = {
        key: value
        for key, value in workspace.inputs.items()
        if key not in {"targets", "partition_by"}
    }
    with pytest.raises(KeyboardInterrupt):
        _execute_extraction(_prepare_extraction({**inputs, "max_concurrent": 1}))
    assert 1 <= len(calls) <= 2


def test_slow_document_does_not_hide_finished_ones(workspace, monkeypatch):
    """Progress follows completion order; records keep the planned order."""
    for number in range(3):
        _add_doc(workspace, f"bulk/doc{number}.txt", f"Document {number}.")
    inputs = {
        key: value
        for key, value in workspace.inputs.items()
        if key not in {"targets", "partition_by"}
    }
    plan = _prepare_extraction({**inputs, "max_concurrent": 2})
    pending = list(plan["pending"])
    slow_text = pending[0].read_text(encoding="utf-8")
    release = threading.Event()

    def extract(_client, text, schema, **kwargs):
        if slow_text in text:
            assert release.wait(10)
        return workspace.llm(text, schema, **kwargs)

    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", extract)
    running, finished = set(), []

    def on_progress(doc, outcome):
        if outcome is None:
            running.add(doc)
            assert len(running) <= 2
            return
        running.remove(doc)
        finished.append(doc)
        if len(finished) == len(pending) - 1:
            release.set()

    try:
        result = _execute_extraction(plan, on_progress=on_progress)
    finally:
        release.set()
    assert finished == [*pending[1:], pending[0]]
    assert [record["file"] for record in result.records] == [doc.name for doc in pending]


def test_shared_doc_without_candidates_makes_no_call(workspace):
    workspace.shared.write_text("Nothing about any roster jurisdiction.\n")
    _run(workspace.inputs)
    record = _record(workspace, "_shared/survey.json")
    assert record["payload"]["jurisdictions"] == []
    assert record["quality"]["status"] == "no_roster_match"
    assert len(workspace.llm.texts) == 1


def test_rerun_reuses_and_roster_change_only_touches_affected_shared_doc(workspace):
    _run(workspace.inputs)
    assert len(workspace.llm.texts) == 2
    _run(workspace.inputs)
    assert len(workspace.llm.texts) == 2
    _write_targets(workspace.targets, [*TARGETS, {"state": "AZ", "ahj": "Yuma County"}])
    _run(workspace.inputs)
    assert len(workspace.llm.texts) == 2
    workspace.shared.write_text(SHARED_TEXT + "Yuma County, AZ: permits online.\n")
    _run(workspace.inputs)
    assert len(workspace.llm.texts) == 3
    assert '"ahj": "Yuma County"' in workspace.llm.texts[-1]


def test_concurrency_is_deterministic(workspace, tmp_path):
    outcomes = []
    for workers in (1, 4):
        output = tmp_path / f"records{workers}"
        result = _run({**workspace.inputs, "output": str(output), "max_concurrent": workers})
        outcomes.append(
            (
                result.successful,
                result.total_items,
                round(result.total_cost, 6),
                [r["output_path"].replace(str(output), "") for r in result.records],
                {
                    path.relative_to(output).as_posix(): (
                        json.loads(path.read_text())["payload"],
                        json.loads(path.read_text())["quality"],
                    )
                    for path in output.rglob("*.json")
                    if "run_manifests" not in path.parts
                },
            )
        )
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("value", [0, -1, "two"])
def test_max_concurrent_must_be_positive_int(tmp_path, value):
    config = tmp_path / "run.yaml"
    config.write_text(f"domain: demo\nextraction:\n  schema: s.json\n  max_concurrent: {value}\n")
    with pytest.raises(RuntimeConfigError, match="max_concurrent"):
        load_runtime_config_file(config)


def test_partial_key_overlap_fails_before_calls(workspace):
    schema = Path(workspace.inputs["schema"])
    data = json.loads(schema.read_text(encoding="utf-8"))
    data["$metadata"]["identity"]["deduplication"]["key_fields"] = ["state", "online"]
    schema.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(RuntimeConfigError, match="partially overlaps"):
        _prepare_extraction(workspace.inputs)
    assert workspace.llm.texts == []


def test_shared_table_window_is_labelled_and_layout_change_reextracts(workspace, monkeypatch):
    towns = "\n".join(f"Town {number} County,AZ,FALSE" for number in range(150))
    table = f"Jurisdiction Name,State,SolarAPP\n{towns}\nPima County,AZ,TRUE\n{towns}\n"
    _add_doc(workspace, "_shared/locations.csv", table, scope="shared")
    _run(workspace.inputs)
    prompt = next(t for t in workspace.llm.texts if "Jurisdiction Name: Pima County" in t)
    assert "Jurisdiction Name: Pima County | State: AZ | SolarAPP: TRUE" in prompt
    assert len(prompt) < len(table) / 4
    workspace.llm.texts.clear()
    _run(workspace.inputs)
    assert workspace.llm.texts == []
    monkeypatch.setitem(document_utils.TABLE_TEXT_LAYOUT, ".csv", "another-layout")
    _run(workspace.inputs)
    assert len(workspace.llm.texts) == 1 and "SolarAPP: TRUE" in workspace.llm.texts[0]
