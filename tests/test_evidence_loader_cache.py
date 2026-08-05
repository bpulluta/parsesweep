"""Tests for EvidenceLoader path-keyed caching (W11 consolidation).

The loader is reused across every document in a QA/QC run, so the shared
discovery checkpoint and each document's extraction outputs must be parsed
once per source path instead of re-read on every call.
"""

import json
from pathlib import Path

from psweep.qa_qc.evidence_loader import EvidenceLoader


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_document_metadata_parsed_once_across_documents(tmp_path, monkeypatch):
    checkpoint = tmp_path / "checkpoint.json"
    _write_json(
        checkpoint,
        {"entries": {"doc-a": {"file_path": "a.pdf"}, "doc-b": {"file_path": "b.pdf"}}},
    )

    loader = EvidenceLoader(schema=None)

    calls = {"count": 0}
    real_load = json.load

    def counting_load(fp, *args, **kwargs):
        calls["count"] += 1
        return real_load(fp, *args, **kwargs)

    monkeypatch.setattr(json, "load", counting_load)

    first = loader.load_document_metadata(checkpoint)
    second = loader.load_document_metadata(checkpoint)

    assert first is second
    assert calls["count"] == 1
    assert loader.get_document_path("doc-a") == "a.pdf"


def test_extraction_outputs_reread_for_new_directory(tmp_path):
    schema = {
        "$metadata": {"extraction": {"main_data_array": "items"}},
    }
    loader = EvidenceLoader(schema=schema)

    dir_a = tmp_path / "doc-a"
    dir_b = tmp_path / "doc-b"
    dir_a.mkdir()
    dir_b.mkdir()
    _write_json(dir_a / "m1.json", {"items": [{"id": "1"}]})
    _write_json(dir_a / "m2.json", {"items": [{"id": "1"}]})
    _write_json(dir_b / "m1.json", {"items": [{"id": "2"}]})
    _write_json(dir_b / "m2.json", {"items": [{"id": "2"}]})

    out_a = loader.load_extraction_outputs(dir_a, ["m1", "m2"])
    # Same directory again -> cached identical object (no re-read).
    assert loader.load_extraction_outputs(dir_a, ["m1", "m2"]) is out_a
    # New directory -> refreshed cache.
    out_b = loader.load_extraction_outputs(dir_b, ["m1", "m2"])
    assert out_b is not out_a
    assert out_b["m1"]["items"][0]["id"] == "2"
