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
