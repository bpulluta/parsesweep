from __future__ import annotations

from pathlib import Path

import pandas as pd
from click.testing import CliRunner

from psweep.cli.main import cli

SCHEMA_PATH = Path("schemas/personal/geothermal_ordinance_schema.json")


def _patch_compiler(monkeypatch):
    def _fake_compile_from_directory(self, json_dir, apply_deduplication=True):
        return (
            pd.DataFrame([{"State": "MA", "County": "Longmeadow", "Value": 1}]),
            {"type": "Document", "main_array_key": "requirements"},
        )

    def _fake_save_csv(self, df, path):
        path.write_text("State,County,Value\nMA,Longmeadow,1\n", encoding="utf-8")
    def _fake_save_excel(self, df, path):
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
    _patch_compiler(monkeypatch)
    extracted_dir = tmp_path / "extracted"
    extracted_dir.mkdir(parents=True)
    (extracted_dir / "dummy.json").write_text("{}", encoding="utf-8")

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


def test_compile_fresh_dry_run_does_not_clear_output_directory(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_compiler(monkeypatch)
    extracted_dir = tmp_path / "extracted"
    extracted_dir.mkdir(parents=True)
    (extracted_dir / "dummy.json").write_text("{}", encoding="utf-8")

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
