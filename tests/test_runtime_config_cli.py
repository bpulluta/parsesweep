import json

from click.testing import CliRunner

from psweep.cli.main import cli


def test_process_validate_config_with_run_yaml(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
extraction:
  input_dir: documents/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["extract", "--config", str(config_path), "--validate-config"],
    )

    assert result.exit_code == 0
    if result.output.strip():
        assert "Runtime config validation passed" in result.output


def test_process_validate_config_strict_rejects_unknown_key(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
extraction:
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
            "extract",
            "--config",
            str(config_path),
            "--validate-config",
            "--config-strict",
        ],
    )

    assert result.exit_code != 0
    assert "Unknown keys in 'extraction' section" in result.output


def test_compile_validate_config_quiet_returns_json(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
compilation:
  input_dir: processed/geothermal_ordinances
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["compile", "--config", str(config_path), "--validate-config", "-q"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "valid"
    assert payload["command"] == "compile"


def test_config_runtime_catalog_json_output():
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["config", "--show-runtime-catalog", "--format", "json"],
    )

    assert result.exit_code == 0
    catalog = json.loads(result.output)
    assert "extraction" in catalog
    assert "compilation" in catalog
    assert "discovery" in catalog


def test_acquire_validate_config_with_run_yaml(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: geothermal_ordinances
discovery:
  seeds:
    - https://example.org/docs
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["discover", "--config", str(config_path), "--validate-config"],
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
            "discover",
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


def test_discover_reprocess_deletes_checkpoint(tmp_path):
    # Checkpoint lives at manifest.parent.parent.parent/checkpoint.json.
    run_dir = tmp_path / "runs" / "run-x"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(
        '{"version": 1, "entries": {"Aurora CO": {"run_id": "old"}}}',
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "discover",
            "--seed-url",
            "https://example.org/docs",
            "--output-documents",
            str(tmp_path / "docs"),
            "--output-manifest",
            str(manifest_path),
            "--dry-run",
            "--reprocess",
            "-q",
        ],
    )

    assert result.exit_code == 0
    assert not checkpoint_path.exists()


def test_discover_default_preserves_checkpoint(tmp_path):
    run_dir = tmp_path / "runs" / "run-x"
    run_dir.mkdir(parents=True)
    manifest_path = run_dir / "manifest.json"
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(
        '{"version": 1, "entries": {"Aurora CO": {"run_id": "old"}}}',
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "discover",
            "--seed-url",
            "https://example.org/docs",
            "--output-documents",
            str(tmp_path / "docs"),
            "--output-manifest",
            str(manifest_path),
            "--dry-run",
            "-q",
        ],
    )

    assert result.exit_code == 0
    assert checkpoint_path.exists()


def test_acquire_validate_config_accepts_serpapi_flag(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "discover",
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


def _run_discover_and_capture_request(extra_args, tmp_path):
    """Invoke `discover` with DiscoveryEngine.run patched to capture the request."""
    from unittest.mock import patch

    from psweep.discovery.engine import DiscoveryResult

    captured = {}

    def _fake_run(self, request):
        captured["request"] = request
        return DiscoveryResult(
            run_id="test",
            manifest_path=tmp_path / "manifest.json",
            documents_dir=tmp_path / "docs",
            dry_run=True,
        )

    with patch(
        "psweep.discovery.engine.DiscoveryEngine.run", _fake_run
    ):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "discover",
                "--seed-url",
                "https://example.org/docs",
                "-q",
                *extra_args,
            ],
        )
    return result, captured.get("request")


def test_discover_defaults_to_skip_existing(tmp_path):
    result, request = _run_discover_and_capture_request([], tmp_path)
    assert result.exit_code == 0
    assert request is not None
    assert request.reprocess is False


def test_discover_reprocess_flag_sets_request_reprocess(tmp_path):
    result, request = _run_discover_and_capture_request(["--reprocess"], tmp_path)
    assert result.exit_code == 0
    assert request is not None
    assert request.reprocess is True


def test_acquire_validate_config_accepts_partition_fields(tmp_path):
        config_path = tmp_path / "run.yaml"
        config_path.write_text(
                """
domain: geothermal_ordinances
discovery:
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
                ["discover", "--config", str(config_path), "--validate-config", "-q"],
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
discovery:
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
                ["discover", "--config", str(config_path), "--validate-config", "-q"],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        resolved = payload["resolved"]
        assert resolved["robots_policy_mode"] == "warn"
        assert resolved["tos_policy_mode"] == "enforce"
        assert resolved["acknowledged_tos_domains"] == ["example.org"]
