"""Tests for the extracted compilation accounting core module.

These exercise the accounting logic directly (no CLI), which is now possible
because it lives in :mod:`psweep.compilation.accounting`.
"""

import json
from pathlib import Path

from psweep.compilation.accounting import (
    build_pipeline_accounting,
    canonical_discovery_manifest_path,
    collect_discovery_manifests,
    discover_domain_roots,
    discovery_domain_root_from_manifest,
    write_compilation_run_manifest,
)
from datetime import datetime, timezone


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_discover_domain_roots_includes_cwd_and_extraction_parents(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    extraction_dir = tmp_path / "extracted" / "widgets"
    roots = discover_domain_roots(
        domain="widgets",
        extraction_dir=extraction_dir,
        discovery_input_dir=None,
    )
    root_strs = {str(r) for r in roots}
    assert str(tmp_path / "discovered" / "widgets") in root_strs


def test_collect_discovery_manifests_dedupes_and_sorts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    domain = "widgets"
    run_manifest = tmp_path / "discovered" / domain / "runs" / "r1" / "manifest.json"
    latest_manifest = tmp_path / "discovered" / domain / "latest" / "manifest.json"
    _write_json(run_manifest, {"run_id": "r1"})
    _write_json(latest_manifest, {"run_id": "r1-latest"})

    manifests = collect_discovery_manifests(
        domain=domain,
        extraction_dir=tmp_path / "extracted" / domain,
        discovery_input_dir=None,
    )
    manifest_strs = {str(m) for m in manifests}
    assert str(run_manifest) in manifest_strs
    assert str(latest_manifest) in manifest_strs
    # No duplicate entries even though several roots resolve to the same tree.
    assert len(manifests) == len(manifest_strs)


def test_discovery_domain_root_from_manifest_layouts(tmp_path):
    domain = "widgets"
    latest = tmp_path / "discovered" / domain / "latest" / "manifest.json"
    runs = tmp_path / "discovered" / domain / "runs" / "r1" / "manifest.json"
    assert discovery_domain_root_from_manifest(latest, domain) == (
        tmp_path / "discovered" / domain
    )
    assert discovery_domain_root_from_manifest(runs, domain) == (
        tmp_path / "discovered" / domain
    )
    # Unknown layout returns None.
    assert discovery_domain_root_from_manifest(tmp_path / "manifest.json", domain) is None


def test_canonical_discovery_manifest_path_prefers_runs_over_latest(tmp_path):
    domain = "widgets"
    run_id = "r1"
    canonical = tmp_path / "discovered" / domain / "runs" / run_id / "manifest.json"
    _write_json(canonical, {"run_id": run_id})
    latest = tmp_path / "discovered" / domain / "latest" / "manifest.json"
    _write_json(latest, {"run_id": run_id})

    resolved = canonical_discovery_manifest_path(
        manifest_path=latest,
        domain=domain,
        run_id=run_id,
    )
    assert resolved == str(canonical)


def test_build_pipeline_accounting_from_extraction_run_manifest(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    domain = "widgets"
    extraction_dir = tmp_path / "extracted" / domain
    output_dir = tmp_path / "compiled" / domain
    output_dir.mkdir(parents=True, exist_ok=True)

    _write_json(
        extraction_dir / "run_manifests" / "abc.manifest.json",
        {
            "run_id": "run://abc",
            "mode": "single_model",
            "costs": {
                "total_cost_usd": 0.5,
                "total_input_tokens": 100,
                "total_output_tokens": 50,
                "documents_billed": 2,
            },
            "lineage": {
                "model": "gpt-x",
                "provider": "azure",
                "schema_id": "schemas/widgets.json",
            },
            "timing": {
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:01:00Z",
                "elapsed_seconds": 60,
            },
        },
    )

    accounting = build_pipeline_accounting(
        domain=domain,
        extraction_dir=extraction_dir,
        output_dir=output_dir,
        compilation_stats={
            "records": 2,
            "columns": 3,
            "duplicates_removed": 0,
            "output_format": "csv, excel",
        },
        discovery_input_dir=None,
    )

    assert accounting["accounting_version"] == "2.0.0"
    assert accounting["domain"] == domain
    summary = accounting["summary"]
    assert summary["total_cost_usd"] == 0.5
    assert summary["documents_extracted"] == 2
    assert summary["extraction_runs"] == 1
    assert summary["cost_per_document_usd"] == 0.25
    assert summary["tokens_total"] == 150

    ext_stage = accounting["stages"]["extraction"]
    assert ext_stage["documents_billed"] == 2
    assert ext_stage["cost_usd"] == 0.5
    assert ext_stage["provider"] == "azure"
    # No discovery manifests present -> discovery marked not applicable.
    assert accounting["stages"]["discovery"]["status"] == "not_applicable"
    # Compilation stats merged into the compilation stage.
    assert accounting["stages"]["compilation"]["records"] == 2


def test_write_compilation_run_manifest_roundtrip(tmp_path):
    started_at = datetime.now(timezone.utc)
    manifest_path = write_compilation_run_manifest(
        output_dir=tmp_path,
        schema_id="schemas/widgets.json",
        synthesis_active=False,
        synthesis_model=None,
        synthesis_provider=None,
        synthesis_llm_calls=0,
        records=5,
        columns=4,
        duplicates_removed=1,
        output_formats=["csv", "excel"],
        started_at=started_at,
    )
    assert manifest_path is not None
    assert manifest_path.parent == tmp_path / "run_manifests"
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["manifest_version"] == "1.0.0"
    assert saved["mode"] == "deterministic"
    assert saved["outputs"]["records"] == 5
    assert saved["outputs"]["duplicates_removed"] == 1
    assert saved["costs"]["total_cost_usd"] == 0.0
