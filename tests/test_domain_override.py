"""One ``--domain`` workspace override on every config-driven stage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.config import RuntimeConfigError, load_runtime_config_file
from psweep.pipeline import build_run_stage_commands

STAGES = ("discover", "extract", "compile", "validate")


@pytest.fixture
def config(tmp_path, schema_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ref.csv").write_text("key\n", encoding="utf-8")
    path = tmp_path / "run.yaml"
    path.write_text(
        f"""
domain: demo
schema: {schema_path.as_posix()}
discovery:
  sources:
    - https://example.org/docs
validation:
  references: [ref.csv]
  key_fields: [state, county]
""",
        encoding="utf-8",
    )
    return path


def _flat(output: str) -> str:
    return "".join(output.split())


def test_loader_applies_the_override_once_and_validates_it(config):
    assert load_runtime_config_file(config)["domain"] == "demo"
    assert load_runtime_config_file(config, domain="other")["domain"] == "other"
    for bad in ("..", "a/b"):
        with pytest.raises(RuntimeConfigError, match="domain"):
            load_runtime_config_file(config, domain=bad)


@pytest.mark.parametrize("stage", ["discover", "extract", "compile"])
def test_stage_resolves_every_path_under_the_override(config, stage):
    """Inputs, outputs and caches follow `--domain`; the config domain is not used."""
    result = CliRunner().invoke(
        cli,
        [
            *(stage, "--config", str(config), "--domain", "other"),
            *("--show-effective-config", "--validate-config"),
        ],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "domainother(cli)" in flat
    assert "outputs/demo/" not in flat
    assert stage == "discover" or "outputs/other/" in flat


@pytest.mark.parametrize("stage", ["discover", "extract", "compile"])
def test_stage_without_override_keeps_the_config_domain(config, stage):
    result = CliRunner().invoke(
        cli,
        [stage, "--config", str(config), "--show-effective-config", "--validate-config"],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "domaindemo(config)" in flat
    assert "outputs/other/" not in flat
    assert stage == "discover" or "outputs/demo/" in flat


@pytest.mark.parametrize("stage", ["discover", "extract", "compile", "run"])
def test_invalid_override_is_rejected(config, stage):
    result = CliRunner().invoke(
        cli, [stage, "--config", str(config), "--domain", "a/b", "--validate-config"]
    )
    assert result.exit_code != 0
    assert "domain" in _flat(result.output)


def test_validate_passes_the_override_to_the_validation_run(config):
    seen = {}

    def fake(path, **kwargs):
        seen.update(kwargs)
        raise ValueError("stop")

    with patch("psweep.validation.run_validation", fake):
        CliRunner().invoke(cli, ["validate", "--config", str(config), "--domain", "other"])
    assert seen["domain"] == "other"


def test_run_forwards_the_override_to_every_stage(config):
    captured = []

    def fake(stage_cmds, on_stage=None):
        captured.extend(stage_cmds)
        return []

    with patch("psweep.cli.app.execute_run_stages", fake):
        result = CliRunner().invoke(
            cli, ["run", "--config", str(config), "--domain", "other", "--yes", "-q"]
        )
    assert result.exit_code == 0, result.output
    assert [stage for stage, _ in captured] == list(STAGES)
    for stage, argv in captured:
        assert argv[argv.index("--domain") + 1] == "other", stage


def test_run_stage_argv_has_no_domain_without_an_override(config):
    commands = build_run_stage_commands(Path(config), base_cmd=["psweep"])
    assert all("--domain" not in argv for _, argv in commands)
    forwarded = build_run_stage_commands(Path(config), base_cmd=["psweep"], domain="other")
    assert all(argv.count("--domain") == 1 for _, argv in forwarded)
