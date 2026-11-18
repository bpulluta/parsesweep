from __future__ import annotations

from pathlib import Path

import hashlib

import pytest

from psweep.compilation.input_provenance import (
    analyze_compile_input_provenance as _analyze,
    list_compile_record_files,
    select_compile_inputs,
)
from psweep.extraction.admission import admission_path

from _helpers import write_json as _write_json


def analyze(directory: Path) -> list[str]:
    return _analyze(select_compile_inputs(directory))


def _publish(directory: Path, **dispositions: str) -> None:
    """Write run evidence: admitted names get their real digest."""
    records = {}
    for name, disposition in dispositions.items():
        entry = {"disposition": disposition}
        if disposition in {"new", "reused"}:
            entry["record_sha256"] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        records[name] = entry
    _write_json(
        admission_path(directory),
        {"admission_version": "1.0.0", "run_id": "run://x", "records": records},
    )


def _record(path: Path, *, schema_id: str, model: str, provider: str) -> None:
    _write_json(
        path,
        {
            "record_id": f"record://{path.stem}",
            "contract_version": "1.0.0",
            "document": {"source_document_id": path.stem},
            "lineage": {
                "run_id": "run://abc",
                "artifact_id": "artifact://runtime/abc",
                "schema_id": schema_id,
                "model": model,
                "provider": provider,
                "extracted_at": "2026-01-01T00:00:00Z",
            },
            "payload": {"metadata": {"id": path.stem}, "items": [{"name": "x"}]},
        },
    )


def test_warns_on_mixed_lineage_dimensions(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted" / "demo"
    _record(
        extracted / "a.json",
        schema_id="schemas/a.json",
        model="gpt-5.6-terra",
        provider="openai",
    )
    _record(
        extracted / "b.json",
        schema_id="schemas/b.json",
        model="gpt-4o",
        provider="azure",
    )

    warnings = analyze(extracted)
    assert any(w.startswith("mixed_schema_id:") for w in warnings)
    assert any(w.startswith("mixed_model:") for w in warnings)
    assert any(w.startswith("mixed_provider:") for w in warnings)


def test_warns_on_unresolved_and_missing_lineage_fields(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted" / "demo"
    _write_json(
        extracted / "bad.json",
        {
            "record_id": "record://bad",
            "contract_version": "1.0.0",
            "document": {"source_document_id": "bad"},
            "lineage": {
                "run_id": "run://abc",
                "artifact_id": "artifact://runtime/unresolved",
                "schema_id": None,
                "model": "gpt-5.6-terra",
                "provider": "openai",
                "extracted_at": "2026-01-01T00:00:00Z",
            },
            "payload": {"metadata": {"id": "bad"}, "items": [{"name": "x"}]},
        },
    )

    warnings = analyze(extracted)
    assert any(w.startswith("missing_schema_id:") for w in warnings)
    assert any(w.startswith("unresolved_artifact_id:") for w in warnings)


def test_compile_record_listing_is_recursive_and_excludes_aux_dirs(
    tmp_path: Path,
) -> None:
    extracted = tmp_path / "extracted" / "demo"
    _record(
        extracted / "nested" / "a.json",
        schema_id="schemas/a.json",
        model="gpt-5.6-terra",
        provider="openai",
    )
    _record(
        extracted / "nested" / "deeper" / "b.json",
        schema_id="schemas/a.json",
        model="gpt-5.6-terra",
        provider="openai",
    )
    _record(
        extracted / "run_manifests" / "ignored.json",
        schema_id="schemas/a.json",
        model="gpt-5.6-terra",
        provider="openai",
    )
    _record(
        extracted / "validation" / "ignored.json",
        schema_id="schemas/a.json",
        model="gpt-5.6-terra",
        provider="openai",
    )

    files = list_compile_record_files(extracted)
    rel_files = {path.relative_to(extracted).as_posix() for path in files}
    assert rel_files == {"nested/a.json", "nested/deeper/b.json"}


def _pair(tmp_path: Path) -> Path:
    extracted = tmp_path / "extracted"
    for name in ("old-a", "new-b"):
        _record(extracted / f"{name}.json", schema_id="s", model="m", provider="p")
    return extracted


def test_selects_only_admitted_records_and_reports_preserved_history(tmp_path: Path) -> None:
    extracted = _pair(tmp_path)
    _publish(extracted, **{"old-a.json": "failed", "new-b.json": "new"})
    selected = select_compile_inputs(extracted)
    assert selected.qualification == "current"
    assert [path.name for path in selected.files] == ["new-b.json"]
    assert selected.excluded == ("old-a.json",)
    assert selected.failed_documents == 1
    assert (extracted / "old-a.json").exists()
    # Exclusion is not a warning, so warning policy cannot readmit or hide it.
    assert not any("old-a" in warning for warning in _analyze(selected))


def test_unlisted_and_deferred_records_are_not_admitted(tmp_path: Path) -> None:
    extracted = _pair(tmp_path)
    _publish(extracted, **{"new-b.json": "reused", "old-a.json": "deferred"})
    assert select_compile_inputs(extracted).excluded == ("old-a.json",)
    _publish(extracted, **{"new-b.json": "reused"})
    assert select_compile_inputs(extracted).excluded == ("old-a.json",)


def test_admitted_record_changed_after_admission_fails_closed(tmp_path: Path) -> None:
    extracted = _pair(tmp_path)
    _publish(extracted, **{"old-a.json": "new", "new-b.json": "new"})
    (extracted / "new-b.json").write_text((extracted / "new-b.json").read_text() + " ")
    with pytest.raises(ValueError, match="changed since admission"):
        select_compile_inputs(extracted)


def test_admitted_record_missing_fails_closed(tmp_path: Path) -> None:
    extracted = _pair(tmp_path)
    _publish(extracted, **{"old-a.json": "new", "new-b.json": "new"})
    (extracted / "old-a.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        select_compile_inputs(extracted)


@pytest.mark.parametrize("content", ["{not json", '{"admission_version": "0", "records": {}}'])
def test_broken_pointer_fails_closed(tmp_path: Path, content: str) -> None:
    extracted = _pair(tmp_path)
    admission_path(extracted).parent.mkdir(parents=True)
    admission_path(extracted).write_text(content)
    with pytest.raises(ValueError, match="admission evidence"):
        select_compile_inputs(extracted)


def test_managed_directory_without_pointer_is_not_historical(tmp_path: Path) -> None:
    extracted = _pair(tmp_path)
    _write_json(extracted / "run_manifests" / "r.manifest.json", {"run_id": "run://r"})
    with pytest.raises(ValueError, match="no current admission"):
        select_compile_inputs(extracted)


def test_standalone_directory_is_explicitly_historical(tmp_path: Path) -> None:
    extracted = _pair(tmp_path)
    selected = select_compile_inputs(extracted)
    assert selected.qualification == "historical_unverified"
    assert len(selected.records) == 2 and selected.excluded == ()
