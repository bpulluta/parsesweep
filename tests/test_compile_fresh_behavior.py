from __future__ import annotations

from pathlib import Path

import pandas as pd
from click.testing import CliRunner

from psweep.cli.main import cli

import json
import pytest


@pytest.fixture(autouse=True)
def synthetic_schema(tmp_path, monkeypatch):
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {"main_data_array": "requirements"},
                    "identity": {"deduplication": {"key_fields": ["value"]}},
                }
            }
        )
    )
    monkeypatch.setitem(globals(), "SCHEMA_PATH", schema)


SCHEMA_PATH = None


def _patch_compiler(monkeypatch):
    def _fake_compile_from_directory(self, json_dir, apply_deduplication=True, allow_model=True):
        return (
            pd.DataFrame([{"State": "MA", "County": "Longmeadow", "Value": 1}]),
            {"type": "Document", "main_array_key": "requirements"},
        )

    def _fake_save_csv(self, df, path, *, prepared=False):
        path.write_text("State,County,Value\nMA,Longmeadow,1\n", encoding="utf-8")

    def _fake_save_excel(self, df, path, *, prepared=False):
        path.write_bytes(b"")

    monkeypatch.setattr(
        "psweep.compilation.data_compiler.DataCompiler.compile_from_directory",
        _fake_compile_from_directory,
    )
    monkeypatch.setattr(
        "psweep.compilation.data_compiler.DataCompiler.save_csv",
        _fake_save_csv,
    )
    monkeypatch.setattr(
        "psweep.compilation.data_compiler.DataCompiler.save_excel",
        _fake_save_excel,
    )


def test_compile_fresh_clears_custom_output_directory(tmp_path: Path, monkeypatch) -> None:
    from psweep.workspace import Workspace

    monkeypatch.chdir(tmp_path)
    cache_entry = Workspace("extracted").reconciliation_cache / "decision.json"
    cache_entry.parent.mkdir(parents=True)
    cache_entry.write_text("verified decision")
    _patch_compiler(monkeypatch)
    extracted_dir = tmp_path / "extracted"
    extracted_dir.mkdir(parents=True)
    (extracted_dir / "dummy.json").write_text('{"payload": {"requirements": []}}', encoding="utf-8")

    output_dir = tmp_path / "custom-output"
    output_dir.mkdir(parents=True)
    stale_file = output_dir / "stale.csv"
    stale_file.write_text("old", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "compile",
            str(extracted_dir),
            "--schema",
            str(SCHEMA_PATH),
            "--output",
            str(output_dir),
            "--fresh",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not stale_file.exists()
    assert any(output_dir.glob("*.csv"))
    assert cache_entry.read_text() == "verified decision"


def test_compile_fresh_dry_run_does_not_clear_output_directory(tmp_path: Path, monkeypatch) -> None:
    _patch_compiler(monkeypatch)
    extracted_dir = tmp_path / "extracted"
    extracted_dir.mkdir(parents=True)
    (extracted_dir / "dummy.json").write_text('{"payload": {"requirements": []}}', encoding="utf-8")

    output_dir = tmp_path / "custom-output"
    output_dir.mkdir(parents=True)
    stale_file = output_dir / "stale.csv"
    stale_file.write_text("old", encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "compile",
            str(extracted_dir),
            "--schema",
            str(SCHEMA_PATH),
            "--output",
            str(output_dir),
            "--dry-run",
            "--report-format",
            "json",
            "--fresh",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stale_file.exists()


@pytest.mark.parametrize("status, code", [("success", 0), ("partial_failure", 3), ("failed", 1)])
def test_compile_reconciliation_status_costs_and_exports(tmp_path, monkeypatch, status, code):
    """CLI publishes unresolved rows while explicitly reporting the stage outcome."""
    from psweep.compilation.data_compiler import DataCompiler

    monkeypatch.chdir(tmp_path)
    _patch_compiler(monkeypatch)
    monkeypatch.setattr(DataCompiler, "status", property(lambda _: status))
    costs = {
        "total_cost_usd": 0.02,
        "total_input_tokens": 100,
        "total_output_tokens": 10,
        "llm_calls": 1,
        "unknown_cost_calls": 0,
        "models": ["resolver-model"],
        "attempt_ids": ["attempt-1"],
        "run_id": "resolver-run-1",
    }
    stats = {"targets": 2, "resolved": 1, "unresolved": int(code > 0), "cached": 0, "pending": 0}
    patched_compile = DataCompiler.compile_from_directory

    def with_usage(self, json_dir, **kwargs):
        assert kwargs["allow_model"] is True
        self.reconciliation_costs = costs
        self.reconciliation_stats = stats
        return patched_compile(self, json_dir, **kwargs)

    monkeypatch.setattr(DataCompiler, "compile_from_directory", with_usage)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "dummy.json").write_text('{"payload": {"requirements": []}}')
    output = tmp_path / "compiled"
    result = CliRunner().invoke(
        cli,
        ["compile", str(extracted), "--schema", str(SCHEMA_PATH), "--output", str(output)],
    )
    assert result.exit_code == code, result.output
    assert list(output.glob("*.csv"))
    manifest = json.loads(next((output / "run_manifests").glob("*.json")).read_text())
    assert manifest["status"]["result"] == status
    assert manifest["costs"] == costs
    assert manifest["reconciliation"] == stats
    assert (
        json.loads((output / "run_accounting.json").read_text())["summary"]["total_cost_usd"]
        == 0.02
    )


@pytest.mark.parametrize("pending", [False, True])
def test_compile_dry_run_disables_model_and_writes_no_manifest(tmp_path, monkeypatch, pending):
    from psweep.compilation.data_compiler import DataCompiler

    monkeypatch.chdir(tmp_path)
    _patch_compiler(monkeypatch)
    patched_compile = DataCompiler.compile_from_directory

    def dry_compile(self, json_dir, **kwargs):
        assert kwargs["allow_model"] is False
        if pending:
            self.roster = object()
            self.reconciliation_stats = {"targets": 1, "pending": 1, "unresolved": 1}
            self.roster_stats = {"roster_targets": 1}
        return patched_compile(self, json_dir, **kwargs)

    monkeypatch.setattr(DataCompiler, "compile_from_directory", dry_compile)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "dummy.json").write_text('{"payload": {"requirements": []}}')
    output = tmp_path / "compiled"
    result = CliRunner().invoke(
        cli,
        [
            "compile",
            str(extracted),
            "--schema",
            str(SCHEMA_PATH),
            "--output",
            str(output),
            "--dry-run",
            "--report-format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    if pending:
        report = json.loads(result.output)
        assert report["status"] == "dry_run"
        assert report["reconciliation"]["pending"] == 1
        assert report["costs"] == {}
    assert not output.exists()
    assert not (tmp_path / "outputs").exists()


def test_export_failure_preserves_usage_and_records_failed_compile(tmp_path, monkeypatch):
    from psweep.compilation.accounting import build_pipeline_accounting, latest_compilation_manifest
    from psweep.compilation.data_compiler import DataCompiler
    from psweep.workspace import Workspace

    monkeypatch.chdir(tmp_path)
    _patch_compiler(monkeypatch)
    patched_compile = DataCompiler.compile_from_directory
    costs = {
        "total_cost_usd": 0.03,
        "llm_calls": 1,
        "total_input_tokens": 12,
        "attempt_ids": ["spent"],
        "run_id": "run-1",
    }

    def compile_with_receipt(self, json_dir, **kwargs):
        self.reconciliation_costs = costs
        receipt = Workspace("extracted").reconciliation_cache / "attempts" / "spent.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"attempt_id": "spent", "run_id": "run-1", "costs": costs}))
        return patched_compile(self, json_dir, **kwargs)

    def fail_export(*args, **kwargs):
        raise OSError("export failed after spending")

    monkeypatch.setattr(DataCompiler, "compile_from_directory", compile_with_receipt)
    monkeypatch.setattr(DataCompiler, "export", fail_export)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "dummy.json").write_text('{"payload": {"requirements": []}}')
    output = tmp_path / "compiled"
    output.mkdir()
    previous = output / "keep.csv"
    previous.write_text("previous export")
    result = CliRunner().invoke(
        cli,
        [
            "compile",
            str(extracted),
            "--schema",
            str(SCHEMA_PATH),
            "--output",
            str(output),
            "--fresh",
        ],
    )
    assert result.exit_code == 1, result.output
    assert previous.read_text() == "previous export"
    manifest = latest_compilation_manifest(output)
    assert manifest["status"]["result"] == "failed"
    assert manifest["lineage"]["table_sha256"] is None
    accounting = build_pipeline_accounting(
        domain="extracted", extraction_dir=extracted, output_dir=output, compilation_stats={}
    )
    assert accounting["summary"]["total_cost_usd"] == 0.03
    assert accounting["summary"]["llm_calls_total"] == 2  # one extraction record + resolver call


@pytest.mark.parametrize("status", ["success", "partial_failure", "failed"])
def test_api_reconciliation_result_manifest_and_freshness(tmp_path, monkeypatch, status):
    from psweep.compilation.accounting import latest_compilation_manifest
    from psweep.compilation.data_compiler import DataCompiler
    from psweep.pipeline import compile_extractions, compiled_table_fresh

    monkeypatch.chdir(tmp_path)
    _patch_compiler(monkeypatch)
    monkeypatch.setattr(DataCompiler, "status", property(lambda _: status))
    patched_compile = DataCompiler.compile_from_directory
    costs = {"total_cost_usd": 0.04, "llm_calls": 1, "attempt_ids": ["api-attempt"]}
    stats = {
        "targets": 2,
        "resolved": 1,
        "unresolved": int(status != "success"),
        "cached": 0,
        "pending": 0,
    }

    def compile_with_usage(self, json_dir, **kwargs):
        assert kwargs["allow_model"] is True
        self.reconciliation_costs = costs
        self.reconciliation_stats = stats
        return patched_compile(self, json_dir, **kwargs)

    monkeypatch.setattr(DataCompiler, "compile_from_directory", compile_with_usage)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "dummy.json").write_text('{"payload": {"requirements": []}}')
    output = tmp_path / "compiled"
    result = compile_extractions(extracted, SCHEMA_PATH, output_dir=output)
    assert result.status == status
    assert result.reconciliation == stats
    assert result.costs == costs
    assert result.output_files
    manifest = latest_compilation_manifest(output)
    assert manifest["status"]["result"] == status
    assert manifest["costs"] == costs
    assert manifest["reconciliation"] == stats
    assert compiled_table_fresh(extracted, SCHEMA_PATH, config_path=None, output_dir=output) is (
        status == "success"
    )


def test_api_dryrun_reports_pending_without_calls_or_writes(tmp_path, monkeypatch):
    from psweep.compilation.data_compiler import DataCompiler
    from psweep.pipeline import compile_extractions

    monkeypatch.chdir(tmp_path)
    _patch_compiler(monkeypatch)
    patched_compile = DataCompiler.compile_from_directory
    stats = {"targets": 1, "resolved": 0, "unresolved": 0, "cached": 0, "pending": 1}

    def dry_compile(self, json_dir, **kwargs):
        assert kwargs["allow_model"] is False
        self.reconciliation_stats = stats
        return patched_compile(self, json_dir, **kwargs)

    monkeypatch.setattr(DataCompiler, "compile_from_directory", dry_compile)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "dummy.json").write_text('{"payload": {"requirements": []}}')
    output = tmp_path / "compiled"
    result = compile_extractions(extracted, SCHEMA_PATH, output_dir=output, dry_run=True)
    assert result.status == "dry_run"
    assert result.reconciliation == stats
    assert not result.costs
    assert result.output_files == []
    assert not output.exists()
    assert not (tmp_path / "outputs").exists()


def test_api_export_failure_records_failed_manifest_with_spend(tmp_path, monkeypatch):
    from psweep.compilation.accounting import latest_compilation_manifest
    from psweep.compilation.data_compiler import DataCompiler
    from psweep.pipeline import compile_extractions, compiled_table_fresh

    monkeypatch.chdir(tmp_path)
    _patch_compiler(monkeypatch)
    patched_compile = DataCompiler.compile_from_directory
    costs = {"total_cost_usd": 0.07, "llm_calls": 1, "attempt_ids": ["failed-api"]}

    def compile_with_usage(self, json_dir, **kwargs):
        self.reconciliation_costs = costs
        return patched_compile(self, json_dir, **kwargs)

    def fail_export(*args, **kwargs):
        raise OSError("API export failed")

    monkeypatch.setattr(DataCompiler, "compile_from_directory", compile_with_usage)
    monkeypatch.setattr(DataCompiler, "export", fail_export)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "dummy.json").write_text('{"payload": {"requirements": []}}')
    output = tmp_path / "compiled"
    output.mkdir()
    previous = output / "extracted.csv"
    previous.write_text("previous export")
    with pytest.raises(OSError, match="API export failed"):
        compile_extractions(extracted, SCHEMA_PATH, output_dir=output)
    assert previous.read_text() == "previous export"
    manifest = latest_compilation_manifest(output)
    assert manifest["status"]["result"] == "failed"
    assert manifest["costs"] == costs
    assert manifest["lineage"]["table_sha256"] is None
    assert not compiled_table_fresh(extracted, SCHEMA_PATH, config_path=None, output_dir=output)
