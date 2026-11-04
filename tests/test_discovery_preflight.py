"""Regression coverage for discovery configuration and execution preflight."""

import json
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.config import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.discovery import DiscoveryResult
from psweep.discovery.request_inputs import discovery_request_fields
from psweep.pipeline import resolve_run_discovery_enabled, run_pipeline
from psweep.workspace import Workspace


def _write_config(tmp_path, discovery, **sections):
    path = tmp_path / "run.yaml"
    path.write_text(
        yaml.safe_dump({"domain": "demo", "discovery": discovery, **sections}),
        encoding="utf-8",
    )
    return path


def test_request_headers_reach_discovery_engine(tmp_path):
    """Configured headers survive the complete CLI-to-request handoff."""
    headers = {"User-Agent": "FixtureAgent/1.0", "X-Test": "fixture"}
    config = _write_config(
        tmp_path,
        {"sources": ["https://example.org/docs"], "crawl": {"request_headers": headers}},
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "status": "dry_run",
                "errors": [],
                "notes": [],
            }
        )
    )
    result = DiscoveryResult(
        run_id="fixture",
        manifest_path=tmp_path / "manifest.json",
        documents_dir=tmp_path / "documents",
        dry_run=True,
    )
    with patch(
        "psweep.discovery.engine.DiscoveryEngine.run", return_value=result
    ) as run:
        invocation = CliRunner().invoke(
            cli, ["discover", "--config", str(config), "--dry-run", "-q"]
        )
    assert invocation.exit_code == 0, invocation.output
    assert run.call_args.args[0].request_headers == headers


@pytest.mark.parametrize(
    "discovery",
    [
        {"crawl": {"browser_downloads": "false"}},
        {"crawl": {"browser_downloads": "true"}},
        {"search": {"cache": "false"}},
        {"search": {"google_news": "false"}},
        {"selection": {"exclude_draft": "false"}},
        {"selection": {"require_supported_document": "false"}},
        {"crawl": {"browser_escalation": {"enabled": "false"}}},
        {"crawl": {"code_host_adapters": {"enabled": "false"}}},
        {"document_review": {"deduplicate_redundant": "false"}},
    ],
)
def test_quoted_booleans_are_rejected(tmp_path, discovery):
    """Quoted false cannot accidentally enable network behavior."""
    config = _write_config(tmp_path, discovery)
    with pytest.raises(RuntimeConfigError, match="boolean"):
        load_runtime_config_file(config)


@pytest.mark.parametrize(
    "discovery",
    [
        {"runtime": {"max_filess": 4}},
        {"policy": {"robots_mod": "warn"}},
        {"selection": {"max_per_targt": 2}},
        {"output": {"document_dir": "documents"}},
        {"crawl": {"include_url_paterns": ["pdf"]}},
        {"crawl": {"browser_escalation": {"enable": True}}},
        {"document_review": {"deduplication": {"key_felds": ["path"]}}},
    ],
)
def test_nested_typos_are_rejected(tmp_path, discovery):
    """Every consumed nested configuration block rejects unknown keys."""
    config = _write_config(tmp_path, discovery)
    with pytest.raises(RuntimeConfigError, match="unknown key"):
        load_runtime_config_file(config)


@pytest.mark.parametrize(
    "block",
    ["scoring", "dedupe", "file_filters", "keyword_validation", "pipeline"],
)
def test_unused_blocks_are_rejected(tmp_path, block):
    """Retired no-op blocks must never look like supported behavior."""
    config = _write_config(
        tmp_path, {block: [] if block == "pipeline" else {}}
    )
    with pytest.raises(RuntimeConfigError, match=block):
        load_runtime_config_file(config)


@pytest.mark.parametrize(
    "discovery",
    [
        {"crawl": {"max_depth": -1}},
        {"crawl": {"max_pages": True}},
        {"runtime": {"max_files": "10"}},
        {"runtime": {"timeout_seconds": 0}},
        {"runtime": {"min_request_interval_ms": True}},
        {"retry_policy": {"max_attempts": 0}},
        {"retry_policy": {"initial_backoff_seconds": -1}},
        {"search": {"max_results": 0}},
        {"search": {"cache_ttl_minutes": float("nan")}},
        {"crawl": {"browser_escalation": {"settle_seconds": float("inf")}}},
        {"crawl": {"browser_escalation": {"min_shell_chars": 1.5}}},
        {"selection": {"max_per_target": 0}},
        {"selection": {"max_per_host_per_target": -1}},
        {"selection": {"exclude_url_patterns": ["("]}},
        {"document_classifier": {"required_keywords": "term"}},
        {"document_review": {"keywords": [1]}},
        {"document_review": {"max_chars": 0}},
    ],
)
def test_invalid_values_fail_before_execution(tmp_path, discovery):
    """Invalid limits, regexes, and lists fail at the config boundary."""
    config = _write_config(tmp_path, discovery)
    with pytest.raises(RuntimeConfigError):
        load_runtime_config_file(config)


@pytest.mark.parametrize("validate_only", [False, True])
def test_empty_discovery_preflight_matches_execution(tmp_path, validate_only):
    """An empty executable plan fails without calling the engine or writing."""
    config = _write_config(tmp_path, {"runtime": {"max_files": 3}})
    args = ["discover", "--config", str(config), "--fresh", "-q"]
    if validate_only:
        args.append("--validate-config")
    with patch("psweep.discovery.engine.DiscoveryEngine.run") as run:
        invocation = CliRunner().invoke(cli, args)
    assert invocation.exit_code != 0
    assert "Discovery has nothing to do" in invocation.output
    run.assert_not_called()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["run.yaml"]


@pytest.mark.parametrize(
    "discovery",
    [
        {"sources": ["https://example.org/index"], "crawl": {"max_depth": 1}},
        {"queries": ["generic document"]},
        {"targets": [{"name": "a", "urls": ["https://example.org/a.pdf"]}]},
        {"targets": [{"name": "a", "query": "a manual"}]},
    ],
)
def test_executable_inputs_pass_preflight(tmp_path, discovery):
    """Sources, queries, and target urls/query each make a plan executable."""
    config = _write_config(tmp_path, discovery)
    with patch("psweep.discovery.engine.DiscoveryEngine.run") as run:
        invocation = CliRunner().invoke(
            cli,
            ["discover", "--config", str(config), "--validate-config", "-q"],
        )
    assert invocation.exit_code == 0, invocation.output
    run.assert_not_called()


def test_run_preflight_rejects_empty_discovery(tmp_path, monkeypatch, schema_path):
    """Run preflights discovery before clearing outputs or starting stages."""
    monkeypatch.chdir(tmp_path)
    output = Workspace("demo").extracted
    output.mkdir(parents=True)
    sentinel = output / "previous.json"
    sentinel.write_text("{}", encoding="utf-8")
    config = _write_config(
        tmp_path,
        {"runtime": {"max_files": 3}},
        schema=str(schema_path),
    )
    with patch("psweep.cli.app.subprocess.run") as run:
        invocation = CliRunner().invoke(
            cli, ["run", "--config", str(config), "--fresh", "--yes", "-q"]
        )
    assert invocation.exit_code != 0
    assert "Discovery has nothing to do" in invocation.output
    run.assert_not_called()
    assert sentinel.exists()


def test_run_helpers_do_not_bypass_invalid_config(tmp_path):
    """Strict loader failures cannot fall back to unvalidated raw YAML."""
    config = _write_config(
        tmp_path, {"runtime": {"max_concurrent_downloads": 0}}
    )
    with pytest.raises(RuntimeConfigError):
        resolve_run_discovery_enabled(config)


def test_target_rows_without_inputs_are_not_executable():
    """Targets need a query or urls when no shared input exists."""
    with pytest.raises(ValueError, match="nothing to do"):
        discovery_request_fields({"domain": "demo", "targets": [{"name": "a"}]})


def test_invalid_cli_source_is_rejected():
    with pytest.raises(ValueError, match="--source must be an http"):
        discovery_request_fields({"domain": "demo", "sources": ["file:///etc"]})


def test_validate_and_execute_reject_invalid_cli_target_limit(tmp_path):
    """CLI overrides obey the same bounds for validation and execution."""
    config = _write_config(tmp_path, {"sources": ["https://example.org/doc.pdf"]})
    for flags in ([], ["--validate-config"]):
        with patch("psweep.discovery.engine.DiscoveryEngine.run") as run:
            invocation = CliRunner().invoke(
                cli,
                ["discover", "--config", str(config), "--target-limit", "0", *flags, "-q"],
            )
        assert invocation.exit_code != 0
        run.assert_not_called()


@pytest.mark.parametrize("block", ["crawl", "retention", "runtime"])
def test_null_blocks_raise_config_errors(tmp_path, block):
    """Explicit null blocks fail cleanly instead of crashing validators."""
    config = _write_config(
        tmp_path,
        {"sources": ["https://example.org/doc.txt"], block: None},
    )
    with pytest.raises(RuntimeConfigError, match=block):
        load_runtime_config_file(config)


@pytest.mark.parametrize("domain", [None, "override"])
def test_domain_precedence_ignores_absent_cli_values(domain):
    """Null optional CLI values do not suppress the configured domain."""
    resolved = resolve_command_config(
        command="discover",
        cli_values={"domain": domain},
        config_data={
            "domain": "demo",
            "discovery": {
                "sources": ["https://example.org/doc.txt"],
            },
        },
    )
    assert resolved["domain"] == (domain or "demo")


@pytest.mark.parametrize(
    "discovery",
    [{"crawl": {"browser_downloads": "false"}}, {"crawl": {"max_pages": True}}],
)
def test_dictionary_config_uses_discovery_validation(discovery):
    """Direct resolver callers cannot bypass file-loader validation."""
    with pytest.raises(RuntimeConfigError):
        resolve_command_config(
            command="discover",
            cli_values={},
            config_data={"discovery": discovery},
        )


def test_zero_retry_backoffs_are_preserved(tmp_path):
    """Supported zero delays remain zero in the effective request."""
    config = _write_config(
        tmp_path,
        {
            "sources": ["https://example.org/doc.pdf"],
            "retry_policy": {
                "initial_backoff_seconds": 0,
                "max_backoff_seconds": 0,
            }
        },
    )
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data=load_runtime_config_file(config),
    )
    fields = discovery_request_fields(resolved)
    assert fields["retry_initial_backoff_seconds"] == 0
    assert fields["retry_max_backoff_seconds"] == 0


def test_python_run_rejects_empty_discovery_before_subprocess(
    tmp_path, schema_path
):
    """Python orchestration checks discovery before starting any stage."""
    config = _write_config(
        tmp_path,
        {"runtime": {"max_files": 3}},
        schema=str(schema_path),
    )
    with (
        patch("psweep.pipeline.subprocess.run") as run,
        pytest.raises(ValueError, match="Discovery has nothing to do"),
    ):
        run_pipeline(config)
    run.assert_not_called()
