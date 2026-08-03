from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from psweep.cli.main import cli


def test_compare_help_includes_core_options() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["compare", "--help"])
    assert result.exit_code == 0
    assert "--schema" in result.output
    assert "--qaqc-lane" in result.output
    assert "--quiet" in result.output
    assert "--verbose" in result.output


def test_compare_requires_schema_option(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["compare", str(tmp_path)])
    assert result.exit_code != 0
    assert "Missing option '--schema'" in result.output


def test_compare_errors_when_no_qaqc_outputs_found(tmp_path: Path) -> None:
    schema_path = Path("schemas/personal/geothermal_ordinance_schema.json")
    assert schema_path.exists()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["compare", str(tmp_path), "--schema", str(schema_path)],
    )
    assert result.exit_code == 1
    # No run config provided → ValueError from resolve_qaqc_runtime_config
    assert "No QA/QC configuration found" in result.output
