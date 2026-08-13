from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from psweep.cli.main import cli


def test_validate_help_includes_core_options() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["validate", "--help"])
    assert result.exit_code == 0
    assert "--config" in result.output
    assert "--compare-only" in result.output
    assert "--fresh" in result.output
    assert "--quiet" in result.output
    assert "--verbose" in result.output


def test_validate_requires_config_option(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["validate"])
    assert result.exit_code != 0
    assert "Missing option '--config'" in result.output


def test_validate_compare_only_errors_when_no_validation_outputs_found(tmp_path: Path) -> None:
    schema_path = Path("schemas/personal/geothermal_ordinance_schema.json")
    assert schema_path.exists()
    (tmp_path / "sample.txt").write_text("test", encoding="utf-8")
    cfg_path = tmp_path / "run.yaml"
    cfg_path.write_text(
        "domain: test\n"
        "extraction:\n"
        f"  schema: {schema_path.as_posix()}\n"
        f"  input_dir: {tmp_path.as_posix()}\n"
        "validation:\n"
        "  models: [primary, secondary]\n"
        "  comparison_approach: mixed\n"
        "  record_matching:\n"
        "    key_fields: [feature, applies_to, specific_subject]\n"
        "  comparison:\n"
        "    primary_fields: [value, units]\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["validate", "--config", str(cfg_path), "--compare-only"],
    )
    assert result.exit_code == 1
    assert "QA/QC output not found" in result.output


def test_validate_accepts_fresh_flag(tmp_path: Path) -> None:
    schema_path = Path("schemas/personal/geothermal_ordinance_schema.json")
    assert schema_path.exists()
    (tmp_path / "sample.txt").write_text("test", encoding="utf-8")
    cfg_path = tmp_path / "run.yaml"
    cfg_path.write_text(
        "domain: test\n"
        "extraction:\n"
        f"  schema: {schema_path.as_posix()}\n"
        f"  input_dir: {tmp_path.as_posix()}\n"
        "validation:\n"
        "  models: [primary, secondary]\n"
        "  comparison_approach: mixed\n"
        "  record_matching:\n"
        "    key_fields: [feature, applies_to, specific_subject]\n"
        "  comparison:\n"
        "    primary_fields: [value, units]\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["validate", "--config", str(cfg_path), "--compare-only", "--fresh"],
    )
    assert result.exit_code == 1
    assert "No such option" not in result.output
    assert "QA/QC output not found" in result.output
