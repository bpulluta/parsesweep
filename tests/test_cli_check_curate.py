from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from psweep.cli.main import cli


def test_check_command_valid_payload(monkeypatch, tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"foo": {"type": "string"}},
                "required": ["foo"],
            }
        ),
        encoding="utf-8",
    )

    extraction_path = tmp_path / "doc.json"
    extraction_path.write_text(
        json.dumps({"payload": {"foo": "bar"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "psweep.cli.commands_check.get_config",
        lambda: SimpleNamespace(default_schema=str(schema_path)),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["check", str(extraction_path)])
    assert result.exit_code == 0
    assert "Schema validation passed" in result.output


def test_check_command_invalid_json_exits_nonzero(monkeypatch, tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps({"type": "object", "properties": {}}),
        encoding="utf-8",
    )
    extraction_path = tmp_path / "bad.json"
    extraction_path.write_text("{invalid", encoding="utf-8")

    monkeypatch.setattr(
        "psweep.cli.commands_check.get_config",
        lambda: SimpleNamespace(default_schema=str(schema_path)),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["check", str(extraction_path)])
    assert result.exit_code == 1
    assert "Invalid JSON file" in result.output


def test_check_uses_recorded_schema_instead_of_default(monkeypatch, tmp_path):
    schema_path = tmp_path / "resolved.json"
    schema_path.write_text(json.dumps({
        "type": "object", "required": ["quote"],
        "properties": {"quote": {"type": "string", "pattern": r"\S"}},
    }))
    extraction = tmp_path / "record.json"
    extraction.write_text(json.dumps({
        "lineage": {"schema_id": str(schema_path)},
        "payload": {"quote": "Evidence"},
    }))
    monkeypatch.setattr("psweep.cli.commands_check.get_config", lambda: SimpleNamespace(default_schema="not-present.json"))
    result = CliRunner().invoke(cli, ["check", str(extraction)])
    assert result.exit_code == 0, result.output


def test_check_explicit_schema_and_calendar_format(tmp_path):
    schema_path = tmp_path / "resolved.json"
    schema_path.write_text(json.dumps({
        "type": "object",
        "properties": {"date": {"type": "string", "format": "date"}},
    }))
    extraction = tmp_path / "record.json"
    extraction.write_text(json.dumps({"payload": {"date": "2024-02-30"}}))
    result = CliRunner().invoke(cli, ["check", str(extraction), "--schema", str(schema_path)])
    assert result.exit_code == 1
    assert "Schema validation failed" in result.output


def test_check_missing_recorded_schema_does_not_silently_fallback(monkeypatch, tmp_path):
    default = tmp_path / "default.json"
    default.write_text(json.dumps({"type": "object"}))
    extraction = tmp_path / "record.json"
    extraction.write_text(json.dumps({
        "lineage": {"schema_id": "absent-domain-schema.json"}, "payload": {},
    }))
    monkeypatch.setattr("psweep.cli.commands_check.get_config", lambda: SimpleNamespace(default_schema=str(default)))
    result = CliRunner().invoke(cli, ["check", str(extraction)])
    assert result.exit_code == 1
    assert "Schema not found" in result.output


def test_check_invalid_schema_reports_error(tmp_path):
    schema = tmp_path / "invalid.json"
    schema.write_text(json.dumps({"type": "invalid"}))
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"payload": {}}))
    result = CliRunner().invoke(cli, ["check", str(record), "--schema", str(schema)])
    assert result.exit_code == 1
    assert "Cannot load schema" in result.output


def test_curate_command_with_run_dir_quiet(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-001"
    docs_dir = run_dir / "documents"
    docs_dir.mkdir(parents=True)

    doc_path = docs_dir / "example.txt"
    doc_path.write_text("hello", encoding="utf-8")

    review_csv = run_dir / "review.csv"
    with review_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "path",
                "relative_path",
                "llm_selected",
                "human_decision",
                "human_notes",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "path": str(doc_path),
                "relative_path": "example.txt",
                "llm_selected": "true",
                "human_decision": "",
                "human_notes": "",
            }
        )

    runner = CliRunner()
    result = runner.invoke(cli, ["curate", "--run", str(run_dir), "--quiet"])
    assert result.exit_code == 0
    curated_dir = Path(result.output.strip())
    assert curated_dir.exists()
    assert (curated_dir / "example.txt").exists()
