from __future__ import annotations

import json
from pathlib import Path

from psweep.compilation.input_provenance import (
    analyze_compile_input_provenance,
    list_compile_record_files,
)

from _helpers import write_json as _write_json


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
                "profile_id": "default",
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

    warnings = analyze_compile_input_provenance(extracted)
    assert any(w.startswith("mixed_schema_id:") for w in warnings)
    assert any(w.startswith("mixed_model:") for w in warnings)
    assert any(w.startswith("mixed_provider:") for w in warnings)


def test_warns_on_orphan_records_when_manifests_exist(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted" / "demo"
    _record(
        extracted / "a.json",
        schema_id="schemas/a.json",
        model="gpt-5.6-terra",
        provider="openai",
    )
    _record(
        extracted / "orphan.json",
        schema_id="schemas/a.json",
        model="gpt-5.6-terra",
        provider="openai",
    )
    manifest_dir = extracted / "run_manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": "run://abc",
        "outputs": {
            "records": [(extracted / "a.json").as_posix()],
        },
    }
    (manifest_dir / "abc.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    warnings = analyze_compile_input_provenance(extracted)
    assert any(w.startswith("orphan_extraction_records:") for w in warnings)


def test_no_orphan_warning_without_manifests(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted" / "demo"
    _record(
        extracted / "a.json",
        schema_id="schemas/a.json",
        model="gpt-5.6-terra",
        provider="openai",
    )

    warnings = analyze_compile_input_provenance(extracted)
    assert not any(w.startswith("orphan_extraction_records:") for w in warnings)


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
                "profile_id": "default",
                "schema_id": None,
                "model": "gpt-5.6-terra",
                "provider": "openai",
                "extracted_at": "2026-01-01T00:00:00Z",
            },
            "payload": {"metadata": {"id": "bad"}, "items": [{"name": "x"}]},
        },
    )

    warnings = analyze_compile_input_provenance(extracted)
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
