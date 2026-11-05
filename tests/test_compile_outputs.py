"""Compile CLI/library outputs: domain-named files and the roster dry-run report."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.pipeline import compile_extractions
from psweep.workspace import Workspace

SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "ahj": {"type": "string"},
                    "online": {"type": ["string", "null"], "title": "Online Permitting"},
                },
            },
        }
    },
    "$metadata": {
        "extraction": {"main_data_array": "rows"},
        "identity": {"deduplication": {"key_fields": ["state", "ahj"]}},
    },
}


def _workspace(tmp_path: Path, *, roster: bool = True) -> tuple[Path, Path]:
    (tmp_path / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    (tmp_path / "targets.csv").write_text(
        "state,ahj\nAZ,Pima County\nCO,Boulder County\n", encoding="utf-8"
    )
    records = tmp_path / "extracted"
    records.mkdir()
    (records / "a.json").write_text(
        json.dumps(
            {
                "document": {"source_url": "https://p.gov", "source_scope": "target"},
                "payload": {
                    "rows": [
                        {"state": "AZ", "ahj": "Pima County", "online": "Yes"},
                        {"state": "TX", "ahj": "Travis County", "online": "Yes"},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    discovery = (
        "discovery:\n  targets: targets.csv\n  partition_by: [state, ahj]\n" if roster else ""
    )
    config = tmp_path / "run.yaml"
    config.write_text(
        f"domain: demo_domain\nschema: {tmp_path / 'schema.json'}\n{discovery}"
        "compilation:\n  output:\n    default_format: both\n",
        encoding="utf-8",
    )
    return config, records


def _compile(config: Path, records: Path, output: Path, *extra: str):
    return CliRunner().invoke(
        cli,
        ["compile", str(records), "--config", str(config), "--output", str(output), *extra],
    )


def test_compiled_files_are_named_after_the_domain(tmp_path):
    config, records = _workspace(tmp_path)
    output = tmp_path / "compiled"
    result = _compile(config, records, output)
    assert result.exit_code == 0, result.output
    assert Workspace("demo_domain").compiled_name == "demo_domain"
    assert sorted(p.name for p in output.iterdir() if p.suffix in {".csv", ".xlsx"}) == [
        "demo_domain.csv",
        "demo_domain.xlsx",
    ]


def test_fresh_removes_stale_outputs_with_old_names(tmp_path):
    config, records = _workspace(tmp_path)
    output = tmp_path / "compiled"
    output.mkdir()
    (output / "extracted.csv").write_text("stale\n", encoding="utf-8")
    result = _compile(config, records, output, "--fresh")
    assert result.exit_code == 0, result.output
    assert not (output / "extracted.csv").exists()
    assert (output / "demo_domain.csv").exists()
    assert (output / "demo_domain.xlsx").exists()


def test_library_compile_uses_the_same_name(tmp_path):
    config, records = _workspace(tmp_path, roster=False)
    result = compile_extractions(
        records,
        tmp_path / "schema.json",
        config_path=config,
        output_dir=tmp_path / "out",
        report_format="csv",
    )
    assert [path.name for path in result.output_files] == ["demo_domain.csv"]


def test_roster_dry_run_json_report(tmp_path):
    config, records = _workspace(tmp_path)
    output = tmp_path / "compiled"
    result = _compile(config, records, output, "--dry-run", "--report-format", "json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report == {
        "mode": "roster",
        "schema_type": "Document",
        "main_array_key": "rows",
        "rows": 2,
        "roster_targets": 2,
        "targets_with_evidence": 1,
        "unmatched_items": 1,
        "conflicts": 0,
        "warnings": report["warnings"],
        "provenance_policy": "warn",
        "would_fail_on_provenance": False,
    }
    assert not output.exists()


def test_rows_dry_run_json_report_names_its_mode(tmp_path):
    config, records = _workspace(tmp_path, roster=False)
    result = _compile(
        config, records, tmp_path / "compiled", "--dry-run", "--report-format", "json"
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["mode"] == "rows"
    assert report["rows_before_dedup"] == 2


def test_retired_synthesis_guidance_is_gone():
    from psweep.compilation import accounting
    from psweep.extraction import llm_factory
    from psweep.extraction.llm_client import LLMClient

    try:
        LLMClient(model=None)
    except ValueError as exc:
        message = str(exc)
    assert "synthesis" not in message and "model:" in message
    assert "synthesis" not in (llm_factory.__doc__ or "")
    assert "compilation makes no" in accounting.build_pipeline_accounting.__doc__
