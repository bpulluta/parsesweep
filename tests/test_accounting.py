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
    latest_compilation_manifest,
    write_compilation_run_manifest,
)
from psweep.workspace import Workspace
from datetime import datetime, UTC


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


def test_build_pipeline_accounting_from_extraction_run_manifest(monkeypatch, tmp_path):
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
            "mode": "rows",
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
    started_at = datetime.now(UTC)
    stats = {"records": 3, "columns": 6, "roster_targets": 3, "conflicts": 1}
    manifest_path = write_compilation_run_manifest(
        output_dir=tmp_path,
        schema_id="config/widgets/schema.json",
        mode="roster",
        stats=stats,
        started_at=started_at,
        build_fingerprint="f" * 64,
        table_sha256="t" * 64,
    )
    assert manifest_path is not None
    assert manifest_path.parent == tmp_path / "run_manifests"
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["manifest_version"] == "2.0.0"
    assert saved["mode"] == "roster"
    assert saved["outputs"] == stats
    assert "costs" not in saved
    assert saved["lineage"]["build_fingerprint"] == "f" * 64
    assert saved["lineage"]["table_sha256"] == "t" * 64
    assert latest_compilation_manifest(tmp_path) == saved
    assert latest_compilation_manifest(tmp_path / "missing") is None

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


def test_extraction_stage_names_every_model_that_spent(monkeypatch, tmp_path):
    """Transcription spend is named beside the extraction model."""
    monkeypatch.chdir(tmp_path)
    workspace = Workspace("widgets")
    runs = workspace.extracted / "run_manifests"
    for run_id, models in (("a", ["gemini-3.5-flash", "gpt-x"]), ("b", [])):
        _write_json(
            runs / f"{run_id}.manifest.json",
            {
                "run_id": run_id,
                "costs": {"total_cost_usd": 0.5, "models": models},
                "lineage": {"model": "gpt-x", "provider": "openai"},
                "timing": {"finished_at": f"2026-01-01T00:0{len(models)}:00Z"},
            },
        )
    accounting = build_pipeline_accounting(
        domain="widgets",
        extraction_dir=workspace.extracted,
        output_dir=workspace.compiled,
        compilation_stats={"records": 1},
    )
    stage = accounting["stages"]["extraction"]
    assert stage["model"] == ["gemini-3.5-flash", "gpt-x"]
    assert [run["model"] for run in stage["runs"]] == [
        "gpt-x",
        ["gemini-3.5-flash", "gpt-x"],
    ]


def _usage(cost=0.2, **overrides):
    return {
        "total_cost_usd": cost,
        "total_input_tokens": 100,
        "total_output_tokens": 10,
        "llm_calls": 1,
        "unknown_cost_calls": 0,
        "models": ["resolver-model"],
        **overrides,
    }


def _compile_manifest(output, costs, *, status="success", reconciliation=None):
    return write_compilation_run_manifest(
        output_dir=output,
        schema_id="schema.json",
        mode="roster",
        stats={"records": 2},
        started_at=datetime.now(UTC),
        build_fingerprint="fingerprint",
        table_sha256="table",
        status=status,
        costs=costs,
        reconciliation=reconciliation,
    )


def _receipt(workspace, attempt_id, costs, *, run_id="resolver-run"):
    _write_json(
        workspace.reconciliation_cache / "attempts" / f"{attempt_id}.json",
        {
            "attempt_id": attempt_id,
            "run_id": run_id,
            "costs": costs,
            "completed_at": "2026-01-01T00:00:00Z",
        },
    )


def test_compilation_manifest_status_usage_and_summary(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    workspace = Workspace("widgets")
    stats = {"targets": 2, "resolved": 1, "unresolved": 1, "cached": 0, "pending": 0}
    costs = _usage()
    _compile_manifest(workspace.compiled, costs, status="partial_failure", reconciliation=stats)
    manifest = latest_compilation_manifest(workspace.compiled)
    assert manifest["status"] == {"result": "partial_failure"}
    assert manifest["costs"] == costs
    assert manifest["reconciliation"] == stats
    accounting = build_pipeline_accounting(
        domain=workspace.domain,
        extraction_dir=workspace.extracted,
        output_dir=workspace.compiled,
        compilation_stats={"records": 2},
    )
    assert accounting["summary"]["total_cost_usd"] == 0.2
    assert accounting["summary"]["llm_calls_total"] == 1
    assert accounting["summary"]["tokens_total"] == 110
    stage = accounting["stages"]["compilation"]
    assert stage["status"] == "partial_failure"
    assert stage["aggregate"] == costs
    assert stage["reconciliation"] == stats


def test_receipts_deduplicate_manifest_and_preserve_export_abort_spend(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    workspace = Workspace("widgets")
    _receipt(workspace, "a", _usage())
    # A duplicate file for the same actual attempt is not a second call.
    _write_json(
        workspace.reconciliation_cache / "attempts" / "duplicate.json",
        {"attempt_id": "a", "run_id": "resolver-run", "costs": _usage()},
    )
    _receipt(workspace, "aborted", _usage(0.3), run_id="aborted-run")
    # Prefer durable receipts over a manifest describing those same calls.
    _compile_manifest(workspace.compiled, _usage(9, attempt_ids=["a"], run_id="resolver-run"))
    # A subsequent cache hit incurs no new calls or spend.
    _compile_manifest(
        workspace.compiled,
        _usage(
            0,
            total_input_tokens=0,
            total_output_tokens=0,
            llm_calls=0,
            models=[],
            attempt_ids=[],
            run_id="cache-run",
        ),
    )
    accounting = build_pipeline_accounting(
        domain=workspace.domain,
        extraction_dir=workspace.extracted,
        output_dir=workspace.compiled,
        compilation_stats={},
    )
    assert accounting["summary"]["total_cost_usd"] == 0.5
    assert accounting["summary"]["llm_calls_total"] == 2
    assert accounting["summary"]["tokens_total"] == 220
    assert accounting["stages"]["compilation"]["unpublished_attempts"] == 1


def test_missing_receipt_uses_manifest_without_double_counting_present_receipt(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    workspace = Workspace("widgets")
    _receipt(workspace, "a", _usage())
    _compile_manifest(workspace.compiled, _usage(0.4, llm_calls=2, attempt_ids=["a", "b"]))
    accounting = build_pipeline_accounting(
        domain=workspace.domain,
        extraction_dir=workspace.extracted,
        output_dir=workspace.compiled,
        compilation_stats={},
    )
    assert accounting["summary"]["total_cost_usd"] == 0.4
    assert accounting["summary"]["llm_calls_total"] == 2
    assert "unpublished_attempts" not in accounting["stages"]["compilation"]


def test_orphan_unknown_price_usage_is_not_reported_as_free(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    workspace = Workspace("widgets")
    _receipt(workspace, "unknown", _usage(0, unknown_cost_calls=1))
    _write_json(workspace.reconciliation_cache / "attempts" / "bad.json", {"costs": {}})
    accounting = build_pipeline_accounting(
        domain=workspace.domain,
        extraction_dir=workspace.extracted,
        output_dir=workspace.compiled,
        compilation_stats={},
    )
    assert accounting["summary"]["unknown_cost_calls"] == 1
    assert accounting["summary"]["cost_scope"] == "known_metered_llm_only"
    assert accounting["summary"]["llm_calls_total"] == 1
    assert "pricing unavailable" in accounting["summary"]["zero_cost_reason"]
    assert accounting["warnings"][0].startswith("reconciliation_receipt_parse_error:")


def test_receipt_matching_by_run_id_and_duplicate_manifests(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    workspace = Workspace("widgets")
    _receipt(workspace, "a", _usage())
    path = _compile_manifest(workspace.compiled, _usage(9, run_id="resolver-run"))
    saved = json.loads(path.read_text())
    _write_json(workspace.compiled / "run_manifests" / "duplicate.manifest.json", saved)
    # Distinct export run manifests cannot recount an existing provider attempt.
    _compile_manifest(workspace.compiled, _usage(9, attempt_ids=["a"], run_id="resolver-run"))
    accounting = build_pipeline_accounting(
        domain=workspace.domain,
        extraction_dir=workspace.extracted,
        output_dir=workspace.compiled,
        compilation_stats={},
    )
    assert accounting["summary"]["total_cost_usd"] == 0.2
    assert accounting["summary"]["llm_calls_total"] == 1
    assert accounting["stages"]["compilation"]["run_count"] == 2
