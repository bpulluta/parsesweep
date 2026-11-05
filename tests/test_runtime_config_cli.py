import json
import re
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.workspace import Workspace


def _panel_text(output: str) -> str:
    """Join Rich panel lines so wrapped error messages can be matched."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", output).split())


def test_process_validate_config_with_run_yaml(tmp_path, schema_path):
    documents = tmp_path / "documents"
    documents.mkdir()
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"""
schema: {schema_path.as_posix()}
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["extract", str(documents), "--config", str(config_path), "--validate-config"],
    )

    assert result.exit_code == 0
    if result.output.strip():
        assert "Runtime config validation passed" in result.output


def test_process_validate_config_rejects_unknown_key(tmp_path, schema_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"""
schema: {schema_path.as_posix()}
extraction:
  skip_existing: true
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
        ],
    )

    assert result.exit_code != 0
    assert "unknown key 'extraction.mystery_option'" in _panel_text(result.output)


def test_compile_validate_config_quiet_returns_json(tmp_path, schema_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"""
schema: {schema_path.as_posix()}
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


def test_run_validate_config_only_passes_with_valid_runtime_config(tmp_path, schema_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"""
domain: demo
schema: {schema_path.as_posix()}
extraction:
  skip_existing: true
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(config_path), "--validate-config", "-q"],
    )
    assert result.exit_code == 0


def test_run_validate_config_fails_on_unknown_keys(tmp_path, schema_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"""
domain: demo
schema: {schema_path.as_posix()}
extraction:
  skip_existing: true
  mystery_option: true
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(config_path), "--validate-config", "-q"],
    )
    assert result.exit_code != 0
    assert "unknown key 'extraction.mystery_option'" in _panel_text(result.output)


def test_run_fresh_delegates_safe_cleanup_to_extraction(tmp_path, monkeypatch, schema_path):
    """The parent run must not delete before its child preflight."""
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "run.yaml"
    extraction_dir = Workspace("demo").extracted
    extraction_dir.mkdir(parents=True)
    stale_record = extraction_dir / "stale.json"
    stale_record.write_text("{}", encoding="utf-8")

    config_path.write_text(
        f"""
domain: demo
schema: {schema_path.as_posix()}
extraction:
  skip_existing: true
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    with patch(
        "psweep.pipeline.subprocess.run",
        return_value=SimpleNamespace(returncode=0),
    ):
        result = runner.invoke(
            cli,
            ["run", "--config", str(config_path), "--fresh", "--yes", "-q"],
        )

    assert result.exit_code == 0
    assert stale_record.read_text() == "{}"


def test_run_summary_prefers_latest_curated_when_discovery_runs(tmp_path, monkeypatch, schema_path):
    config_path = tmp_path / "run.yaml"
    monkeypatch.chdir(tmp_path)
    workspace = Workspace("demo")
    workspace.extracted.mkdir(parents=True)

    config_path.write_text(
        f"""
domain: demo
discovery:
  targets:
    - label: t1
      query: q1
schema: {schema_path.as_posix()}
extraction:
  skip_existing: true
""",
        encoding="utf-8",
    )

    latest_curated = workspace.latest / "curated"
    consolidated_curated = workspace.curated
    latest_curated.mkdir(parents=True)
    consolidated_curated.mkdir(parents=True)
    (latest_curated / "one.pdf").write_text("x", encoding="utf-8")
    (latest_curated / "two.pdf").write_text("x", encoding="utf-8")
    for idx in range(5):
        (consolidated_curated / f"old-{idx}.pdf").write_text("x", encoding="utf-8")

    runner = CliRunner()
    with patch(
        "psweep.pipeline.subprocess.run",
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


def test_discover_validate_config_with_run_yaml(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: geothermal_ordinances
discovery:
  sources:
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


def test_discover_dry_run_writes_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "discover", "--domain", "demo",
            "--source", "https://example.org/docs", "--dry-run", "-q",
        ],
    )

    assert result.exit_code == 0
    assert list(Workspace("demo").discovered.glob("runs/*/manifest.json"))


def test_discover_dry_run_preserves_checkpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checkpoint_path = Workspace("demo").discovered / "checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text('{"version": 1, "entries": {}}', encoding="utf-8")

    for extra in ([], ["--fresh"]):
        result = CliRunner().invoke(
            cli,
            [
                "discover", "--domain", "demo",
                "--source", "https://example.org/docs", "--dry-run", "-q", *extra,
            ],
        )
        assert result.exit_code == 0
        assert checkpoint_path.exists()


def _run_discover_and_capture_request(extra_args, tmp_path):
    """Invoke `discover` with DiscoveryEngine.run patched to capture the request."""
    from unittest.mock import patch

    from psweep.discovery.engine import DiscoveryResult

    captured = {}

    def _fake_run(self, request):
        captured["request"] = request
        (tmp_path / "manifest.json").write_text(json.dumps({
            "status": "dry_run", "errors": [], "notes": [],
        }))
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
                "--domain",
                "demo",
                "--source",
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
    assert request.fresh is False
    assert request.retention_documents == "all"


def test_discover_fresh_flag_sets_request_fresh(tmp_path):
    result, request = _run_discover_and_capture_request(["--fresh"], tmp_path)
    assert result.exit_code == 0
    assert request is not None
    assert request.fresh is True


def test_discover_config_retention_documents_flows_to_request(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
discovery:
  sources:
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
        (tmp_path / "manifest.json").write_text(json.dumps({
            "status": "dry_run", "errors": [], "notes": [],
        }))
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


def test_run_preflight_rejects_none_retention_with_extract_compile(tmp_path, schema_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"""
domain: demo
discovery:
  sources:
    - https://example.org/docs
  retention:
    documents: none
schema: {schema_path.as_posix()}
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--config", str(config_path), "--yes"],
    )

    assert result.exit_code != 0
    assert "discovery.retention.documents 'none'" in result.output


def test_extract_fresh_preserves_output_when_input_path_missing(tmp_path, monkeypatch):
    """Missing source inputs cannot delete prior extraction records."""
    monkeypatch.chdir(tmp_path)
    workspace = Workspace("demo")
    workspace.extracted.mkdir(parents=True)
    stale_record = workspace.extracted / "stale.json"
    stale_record.write_text("{}", encoding="utf-8")

    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        '{"$metadata":{"extraction":{"main_data_array":"items"},"identity":{"deduplication":{"key_fields":["name"],"ignore_fields":[]}}}}',
        encoding="utf-8",
    )
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        f"""
domain: demo
schema: {schema_path.as_posix()}
""",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["extract", "--config", str(config_path), "--fresh"],
    )

    assert result.exit_code != 0
    assert stale_record.read_text() == "{}"
    assert "Documents path not found" in result.output


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
        (tmp_path / "manifest.json").write_text(json.dumps({
            "status": "dry_run", "errors": [], "notes": [],
        }))
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


def test_discover_validate_config_accepts_retention_documents(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
discovery:
  sources:
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
  sources:
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
            "-q",
        ],
    )

    assert result.exit_code != 0
    assert "unknown key 'discovery.retention.unknown'" in _panel_text(result.output)


def test_discover_dry_run_flag_flows_to_request(tmp_path):
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: demo
discovery:
  sources:
    - https://example.org/docs
""",
        encoding="utf-8",
    )

    from unittest.mock import patch

    from psweep.discovery.engine import DiscoveryResult

    captured = {}

    def _fake_run(self, request):
        captured["request"] = request
        (tmp_path / "manifest.json").write_text(json.dumps({
            "status": "dry_run", "errors": [], "notes": [],
        }))
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
            ["discover", "--config", str(config_path), "--dry-run", "-q"],
        )

    assert result.exit_code == 0
    assert captured["request"].dry_run is True




def test_discover_effective_config_shows_config_key_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        """
domain: fixture
discovery:
  queries: [records]
  search:
    max_results: 7
  selection:
    max_per_target: 2
""",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        cli,
        [
            "discover", "--config", str(config_path),
            "--show-effective-config", "--validate-config", "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    output = _panel_text(result.output)
    assert "discovery.search.max_results" in output
    assert "discovery.selection.max_per_target" in output
    assert "seeker_max_results" not in output
    assert "selection_primary_per_target" not in output


def test_extract_validate_config_does_not_require_documents(tmp_path, schema_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        "domain: demo\n", encoding="utf-8"
    )
    assert schema_path.parent == tmp_path
    result = CliRunner().invoke(
        cli, ["extract", "--config", str(config_path), "--validate-config", "-q"]
    )
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "outputs").exists()
    real = CliRunner().invoke(cli, ["extract", "--config", str(config_path), "-q"])
    assert real.exit_code != 0
    assert "Documents path not found" in _panel_text(real.output)
