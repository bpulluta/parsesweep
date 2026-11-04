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
    write_compilation_run_manifest,
)
from psweep.workspace import Workspace
from datetime import datetime, timezone


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_collect_discovery_manifests_reads_workspace_runs_and_latest(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    discovered = Workspace("widgets").discovered
    run_manifest = discovered / "runs" / "r1" / "manifest.json"
    latest_manifest = discovered / "latest" / "manifest.json"
    _write_json(run_manifest, {"run_id": "r1"})
    _write_json(latest_manifest, {"run_id": "r1-latest"})
    _write_json(Workspace("other").discovered / "runs" / "x" / "manifest.json", {})

    assert collect_discovery_manifests("widgets") == [latest_manifest, run_manifest]


def test_canonical_discovery_manifest_path_prefers_runs_over_latest(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    discovered = Workspace("widgets").discovered
    canonical = discovered / "runs" / "r1" / "manifest.json"
    latest = discovered / "latest" / "manifest.json"
    _write_json(canonical, {"run_id": "r1"})
    _write_json(latest, {"run_id": "r1"})

    assert canonical_discovery_manifest_path("widgets", latest, "r1") == str(canonical)
    assert canonical_discovery_manifest_path("widgets", latest, "missing") == str(latest)


def test_build_pipeline_accounting_from_extraction_run_manifest(
    monkeypatch, tmp_path
):
    """Stage metadata and measured usage survive pipeline accounting."""
    monkeypatch.chdir(tmp_path)
    domain = "widgets"
    extraction_dir = Workspace(domain).extracted
    output_dir = Workspace(domain).compiled
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
                "schema_id": "config/widgets/schema.json",
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


def test_accounting_uses_discovery_contract_dry_run_flag(
    monkeypatch,
    tmp_path,
):
    """Status migrations cannot turn a dry-run into acquisition."""
    monkeypatch.chdir(tmp_path)
    _write_json(
        Workspace("widgets").discovered / "runs/r1/manifest.json",
        {
            "run_id": "r1",
            "status": "dry_run",
            "input": {"dry_run": True},
            "stage_summaries": {"seeker": {"queries_executed": 1}},
        },
    )
    accounting = build_pipeline_accounting(
        domain="widgets",
        extraction_dir=Workspace("widgets").extracted,
        output_dir=Workspace("widgets").compiled,
        compilation_stats={},
    )
    assert accounting["stages"]["discovery"]["runs"][0]["status"] == "dry_run"


def test_write_compilation_run_manifest_roundtrip(tmp_path):
    started_at = datetime.now(timezone.utc)
    stats = {"records": 3, "columns": 6, "roster_targets": 3, "conflicts": 1}
    manifest_path = write_compilation_run_manifest(
        output_dir=tmp_path,
        schema_id="config/widgets/schema.json",
        mode="roster",
        stats=stats,
        started_at=started_at,
    )
    assert manifest_path is not None
    assert manifest_path.parent == tmp_path / "run_manifests"
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["manifest_version"] == "2.0.0"
    assert saved["mode"] == "roster"
    assert saved["outputs"] == stats
    assert "costs" not in saved

    accounting = build_pipeline_accounting(
        domain="widgets",
        extraction_dir=tmp_path / "missing",
        output_dir=tmp_path,
        compilation_stats=stats,
    )
    stage = accounting["stages"]["compilation"]
    assert stage["conflicts"] == 1
    assert stage["runs"][0]["mode"] == "roster"
    assert stage["runs"][0]["roster_targets"] == 3
    assert "aggregate" not in stage
