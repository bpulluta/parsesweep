import json
import re
from types import SimpleNamespace
from unittest.mock import patch

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


def test_run_validate_config_only_passes_with_valid_runtime_config(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
extraction:
  input_dir: documents/demo
  schema: schemas/personal/geothermal_ordinance_schema.json
compilation:
  input_dir: extracted/demo
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(config_path), "--validate-config", "-q"],
    )
    assert result.exit_code == 0


def test_run_validate_config_strict_fails_on_unknown_keys_by_default(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
extraction:
  input_dir: documents/demo
  schema: schemas/personal/geothermal_ordinance_schema.json
  mystery_option: true
compilation:
  input_dir: extracted/demo
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(config_path), "--validate-config"],
    )
    assert result.exit_code != 0
    assert "Unknown keys in 'extraction' section" in result.output


def test_run_validate_config_allows_unknown_keys_when_not_strict(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
extraction:
  input_dir: documents/demo
  schema: schemas/personal/geothermal_ordinance_schema.json
  mystery_option: true
compilation:
  input_dir: extracted/demo
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            "--config",
            str(config_path),
            "--validate-config",
            "--no-config-strict",
        ],
    )
    assert result.exit_code == 0


def test_run_fresh_clears_extraction_output_before_stages(tmp_path):
    config_path = tmp_path / "run.yaml"
    extraction_dir = tmp_path / "extracted" / "demo"
    extraction_dir.mkdir(parents=True)
    stale_record = extraction_dir / "stale.json"
    stale_record.write_text("{}", encoding="utf-8")

    config_path.write_text(
        f"""
domain: demo
extraction:
  input_dir: documents/demo
  schema: schemas/personal/geothermal_ordinance_schema.json
  output_dir: {extraction_dir.as_posix()}
compilation:
  input_dir: {extraction_dir.as_posix()}
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    with patch(
        "psweep.cli.app.subprocess.run",
        return_value=SimpleNamespace(returncode=0),
    ):
        result = runner.invoke(
            cli,
            ["run", "--config", str(config_path), "--fresh", "--yes", "-q"],
        )

    assert result.exit_code == 0
    assert not stale_record.exists()


def test_run_summary_prefers_latest_curated_when_discovery_runs(tmp_path, monkeypatch):
    config_path = tmp_path / "run.yaml"
    monkeypatch.chdir(tmp_path)
    extraction_dir = tmp_path / "extracted" / "demo"
    extraction_dir.mkdir(parents=True)

    config_path.write_text(
        f"""
domain: demo
discovery:
  targets:
    - label: t1
      query: q1
extraction:
  input_dir: docs/demo
  schema: schemas/personal/geothermal_ordinance_schema.json
  output_dir: {extraction_dir.as_posix()}
compilation:
  input_dir: {extraction_dir.as_posix()}
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    latest_curated = tmp_path / "discovered" / "demo" / "latest" / "curated"
    consolidated_curated = tmp_path / "discovered" / "demo" / "curated"
    latest_curated.mkdir(parents=True)
    consolidated_curated.mkdir(parents=True)
    (latest_curated / "one.pdf").write_text("x", encoding="utf-8")
    (latest_curated / "two.pdf").write_text("x", encoding="utf-8")
    for idx in range(5):
        (consolidated_curated / f"old-{idx}.pdf").write_text("x", encoding="utf-8")

    runner = CliRunner()
    with patch(
        "psweep.cli.app.subprocess.run",
        return_value=SimpleNamespace(returncode=0),
    ):
        result = runner.invoke(
            cli,
            ["run", "--config", str(config_path), "--yes"],
        )

    assert result.exit_code == 0
    assert re.search(r"Documents found\s+2", result.output) is not None


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


def test_discover_dry_run_fresh_preserves_checkpoint(tmp_path):
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
            "--fresh",
            "-q",
        ],
    )

    assert result.exit_code == 0
    assert checkpoint_path.exists()


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
    assert request.retention_documents == "all"


def test_discover_fresh_flag_sets_request_reprocess(tmp_path):
    result, request = _run_discover_and_capture_request(["--fresh"], tmp_path)
    assert result.exit_code == 0
    assert request is not None
    assert request.reprocess is True


def test_discover_config_retention_documents_flows_to_request(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
discovery:
  seeds:
    - https://example.org/docs
  retention:
    documents: none
""",
        encoding="utf-8",
    )

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

    with patch("psweep.discovery.engine.DiscoveryEngine.run", _fake_run):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["discover", "--config", str(config_path), "-q"],
        )

    assert result.exit_code == 0
    assert captured["request"].retention_documents == "none"


def test_run_preflight_rejects_none_retention_with_extract_compile(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
discovery:
  seeds:
    - https://example.org/docs
  retention:
    documents: none
extraction:
  input_dir: discovered/demo/curated
  schema: schemas/personal/geothermal_ordinance_schema.json
compilation:
  input_dir: extracted/demo
  schema: schemas/personal/geothermal_ordinance_schema.json
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(config_path), "--yes"],
    )

    assert result.exit_code != 0
    assert "retention_documents='none'" in result.output


def test_extract_fresh_clears_output_even_when_input_path_missing(tmp_path):
    extraction_dir = tmp_path / "extracted" / "demo"
    extraction_dir.mkdir(parents=True)
    stale_record = extraction_dir / "stale.json"
    stale_record.write_text("{}", encoding="utf-8")

    missing_input = tmp_path / "discovered" / "demo" / "latest" / "curated"
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        '{"$metadata":{"extraction":{"main_data_array":"items","identifier_fields":["name"]},"identity":{"deduplication":{"key_fields":["name"],"ignore_fields":[]}}}}',
        encoding="utf-8",
    )
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"""
domain: demo
extraction:
  input_dir: {missing_input.as_posix()}
  schema: {schema_path.as_posix()}
  output_dir: {extraction_dir.as_posix()}
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["extract", "--config", str(config_path), "--fresh"],
    )

    assert result.exit_code != 0
    assert not stale_record.exists()
    assert "Latest discovery curation is missing or empty" in result.output


def test_discover_target_limit_slices_configured_targets(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
discovery:
  targets:
    - label: one
      query: first
    - label: two
      query: second
    - label: three
      query: third
""",
        encoding="utf-8",
    )

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

    with patch("psweep.discovery.engine.DiscoveryEngine.run", _fake_run):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "discover",
                "--config",
                str(config_path),
                "--target-limit",
                "2",
                "-q",
            ],
        )

    assert result.exit_code == 0
    request = captured["request"]
    assert request.targets is not None
    assert len(request.targets) == 2
    assert [row["label"] for row in request.targets] == ["one", "two"]


def test_discover_validate_config_accepts_runtime_target_limit(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
discovery:
  seeds:
    - https://example.org/docs
  runtime:
    target_limit: 3
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
    assert payload["resolved"]["target_limit"] == 3


def test_discover_validate_config_accepts_retention_documents(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
discovery:
  seeds:
    - https://example.org/docs
  retention:
    documents: curated
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
    assert payload["resolved"]["retention_documents"] == "curated"


def test_discover_validate_config_rejects_unknown_retention_keys(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
discovery:
  seeds:
    - https://example.org/docs
  retention:
    documents: curated
    unknown: true
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "discover",
            "--config",
            str(config_path),
            "--validate-config",
            "--config-strict",
            "-q",
        ],
    )

    assert result.exit_code != 0
    assert "Unknown keys in 'discovery.retention'" in result.output


def test_discover_uses_config_dry_run_when_not_passed_on_cli(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
discovery:
  seeds:
    - https://example.org/docs
  dry_run: true
""",
        encoding="utf-8",
    )

    from unittest.mock import patch

    from psweep.discovery.engine import DiscoveryResult

    captured = {}

    def _fake_run(self, request):
        captured["request"] = request
        return DiscoveryResult(
            run_id="test",
            manifest_path=tmp_path / "manifest.json",
            documents_dir=tmp_path / "docs",
            dry_run=request.dry_run,
        )

    with patch("psweep.discovery.engine.DiscoveryEngine.run", _fake_run):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["discover", "--config", str(config_path), "-q"],
        )

    assert result.exit_code == 0
    assert captured["request"].dry_run is True


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
