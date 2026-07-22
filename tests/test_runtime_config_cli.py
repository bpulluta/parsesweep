import json

from click.testing import CliRunner

from psweep.cli.main import cli


def test_process_validate_config_with_run_yaml(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
processing:
  input_dir: documents/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["process", "--config", str(config_path), "--validate-config"],
    )

    assert result.exit_code == 0
    if result.output.strip():
        assert "Runtime config validation passed" in result.output


def test_process_validate_config_strict_rejects_unknown_key(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
processing:
  input_dir: documents/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
  mystery_option: true
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "process",
            "--config",
            str(config_path),
            "--validate-config",
            "--config-strict",
        ],
    )

    assert result.exit_code != 0
    assert "Unknown keys in 'processing' section" in result.output


def test_consolidate_validate_config_quiet_returns_json(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
consolidation:
  input_dir: processed/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["consolidate", "--config", str(config_path), "--validate-config", "-q"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "valid"
    assert payload["command"] == "consolidate"


def test_config_runtime_catalog_json_output():
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["config", "--show-runtime-catalog", "--format", "json"],
    )

    assert result.exit_code == 0
    catalog = json.loads(result.output)
    assert "processing" in catalog
    assert "consolidation" in catalog
    assert "acquisition" in catalog


def test_acquire_validate_config_with_run_yaml(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: geothermal_ordinances
acquisition:
  seeds:
    - https://example.org/docs
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["acquire", "--config", str(config_path), "--validate-config"],
    )

    assert result.exit_code == 0
    assert "Runtime config validation passed" in result.output


def test_acquire_dry_run_writes_manifest(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    docs_dir = tmp_path / "docs"

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "acquire",
            "--seed-url",
            "https://example.org/docs",
            "--output-documents",
            str(docs_dir),
            "--output-manifest",
            str(manifest_path),
            "--dry-run",
            "-q",
        ],
    )

    assert result.exit_code == 0
    assert manifest_path.exists()
    assert docs_dir.exists()


def test_acquire_validate_config_accepts_serpapi_flag(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "acquire",
            "--seed-url",
            "https://example.org/docs",
            "--enable-serpapi",
            "--validate-config",
            "-q",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["resolved"]["enable_serpapi"] is True


def test_acquire_validate_config_accepts_partition_fields(tmp_path):
        config_path = tmp_path / "run.yaml"
        config_path.write_text(
                """
domain: geothermal_ordinances
acquisition:
    seeds:
        - https://example.org/docs
    state: California
    jurisdiction: Imperial County
    partition_mode: jurisdiction
""",
                encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
                cli,
                ["acquire", "--config", str(config_path), "--validate-config", "-q"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        resolved = payload["resolved"]
        assert resolved["state"] == "California"
        assert resolved["jurisdiction"] == "Imperial County"
        assert resolved["partition_mode"] == "jurisdiction"


def test_acquire_validate_config_accepts_policy_fields(tmp_path):
        config_path = tmp_path / "run.yaml"
        config_path.write_text(
                """
domain: geothermal_ordinances
acquisition:
    seeds:
        - https://example.org/docs
    policy:
        robots_mode: warn
        tos_mode: enforce
        acknowledged_tos_domains:
            - example.org
""",
                encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
                cli,
                ["acquire", "--config", str(config_path), "--validate-config", "-q"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        resolved = payload["resolved"]
        assert resolved["robots_policy_mode"] == "warn"
        assert resolved["tos_policy_mode"] == "enforce"
        assert resolved["acknowledged_tos_domains"] == ["example.org"]
