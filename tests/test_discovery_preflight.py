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
from psweep.discovery import DiscoveryRequest, DiscoveryResult
from psweep.discovery.request_inputs import normalize_discover_inputs
from psweep.pipeline import resolve_run_discovery_enabled, run_pipeline


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
        {"seeds": ["https://example.org/docs"], "request_headers": headers},
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


def test_normalization_copies_request_headers():
    """Request construction does not retain the caller's mutable header map."""
    headers = {"X-Test": "fixture"}
    normalized = normalize_discover_inputs({"request_headers": headers})
    assert normalized["request_headers"] == headers
    assert normalized["request_headers"] is not headers


@pytest.mark.parametrize(
    "discovery",
    [
        {"browser_mode": "false"},
        {"browser_mode": "true"},
        {"dry_run": "false"},
        {"enable_serpapi": "false"},
        {"search": {"enabled": "false"}},
        {"search": {"enabled": "true"}},
        {"search": {"cache": "false"}},
        {"search": {"google_news": "false"}},
        {"selection": {"exclude_draft": "false"}},
        {"selection": {"require_supported_document": "false"}},
        {"browser_escalation": {"enabled": "false"}},
        {"code_host_adapters": {"enabled": "false"}},
        {"document_review": {"deduplicate_redundant": "false"}},
        {"digger": {"index_page_mode": {"enabled": "false"}}},
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
        {"runtime": {"max_pagess": 4}},
        {"policy": {"robots_mod": "warn"}},
        {"topology": {"mod": "distributed"}},
        {"selection": {"max_per_targt": 2}},
        {"output": {"document_dir": "documents"}},
        {"discovery_rules": {"include_url_paterns": ["pdf"]}},
        {"digger": {"discovery_rules": {"include_url_paterns": ["pdf"]}}},
        {"digger": {"index_page_mode": {"enable": True}}},
        {"document_review": {"deduplication": {"key_felds": ["path"]}}},
    ],
)
def test_nested_typos_are_rejected(tmp_path, discovery):
    """Every consumed nested configuration block rejects unknown keys."""
    config = _write_config(tmp_path, discovery)
    with pytest.raises(RuntimeConfigError, match="Unknown keys"):
        load_runtime_config_file(config)


@pytest.mark.parametrize(
    "block",
    ["scoring", "dedupe", "file_filters", "keyword_validation", "pipeline"],
)
def test_unused_blocks_are_rejected_even_without_strict_mode(tmp_path, block):
    """Retired no-op blocks must never look like supported behavior."""
    config = _write_config(
        tmp_path, {block: [] if block == "pipeline" else {}}
    )
    with pytest.raises(RuntimeConfigError, match=block):
        load_runtime_config_file(config)


@pytest.mark.parametrize(
    "discovery",
    [
        {"runtime": {"max_depth": -1}},
        {"runtime": {"max_pages": True}},
        {"runtime": {"max_files": "10"}},
        {"runtime": {"timeout_seconds": 0}},
        {"request_delay_ms": True},
        {"retry_policy": {"max_attempts": 0}},
        {"retry_policy": {"initial_backoff_seconds": -1}},
        {"search": {"max_results": 0}},
        {"search": {"cache_ttl_minutes": float("nan")}},
        {"browser_escalation": {"settle_seconds": float("inf")}},
        {"browser_escalation": {"min_shell_chars": 1.5}},
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
    config = _write_config(tmp_path, {"runtime": {"max_pages": 3}})
    args = ["discover", "--config", str(config), "--fresh", "-q"]
    if validate_only:
        args.append("--validate-config")
    with patch("psweep.discovery.engine.DiscoveryEngine.run") as run:
        invocation = CliRunner().invoke(cli, args)
    assert invocation.exit_code != 0
    assert "Discovery input missing" in invocation.output
    run.assert_not_called()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["run.yaml"]


@pytest.mark.parametrize(
    "discovery",
    [
        {
            "hub_pages": ["https://example.org/index"],
            "topology": {"mode": "centralized"},
        },
        {
            "query_templates": ["generic document"],
            "search": {"provider": "serpapi"},
        },
        {"queries": ["generic document"], "search": {"provider": "serpapi"}},
    ],
)
def test_executable_hubs_and_templates_pass_preflight(tmp_path, discovery):
    """Validate seedless hubs/templates without executing discovery."""
    config = _write_config(tmp_path, discovery)
    with patch("psweep.discovery.engine.DiscoveryEngine.run") as run:
        invocation = CliRunner().invoke(
            cli,
            ["discover", "--config", str(config), "--validate-config", "-q"],
        )
    assert invocation.exit_code == 0, invocation.output
    run.assert_not_called()


def test_run_preflight_rejects_empty_discovery(tmp_path, schema_path):
    """Run preflights discovery before clearing outputs or starting stages."""
    output = tmp_path / "extracted"
    output.mkdir()
    sentinel = output / "previous.json"
    sentinel.write_text("{}", encoding="utf-8")
    config = _write_config(
        tmp_path,
        {"runtime": {"max_pages": 3}},
        extraction={
            "input_dir": "missing",
            "schema": str(schema_path),
            "output_dir": str(output),
        },
        compilation={"input_dir": str(output), "schema": str(schema_path)},
    )
    with patch("psweep.cli.app.subprocess.run") as run:
        invocation = CliRunner().invoke(
            cli, ["run", "--config", str(config), "--fresh", "--yes", "-q"]
        )
    assert invocation.exit_code != 0
    assert "Discovery input missing" in invocation.output
    run.assert_not_called()
    assert sentinel.exists()


def test_run_helpers_do_not_bypass_invalid_config(tmp_path):
    """Strict loader failures cannot fall back to unvalidated raw YAML."""
    config = _write_config(
        tmp_path, {"runtime": {"max_concurrent_downloads": 0}}
    )
    with pytest.raises(RuntimeConfigError):
        resolve_run_discovery_enabled(config)


def test_discovery_concurrency_default_matches_direct_requests():
    """CLI normalization and direct engine requests share one default."""
    request = DiscoveryRequest(
        domain="demo",
        seed_urls=[],
        query=None,
        enable_serpapi=False,
        output_documents=None,
        output_manifest=None,
        dry_run=True,
    )
    assert (
        request.max_concurrent_downloads
        == normalize_discover_inputs({})["max_concurrent_downloads"]
    )


def test_explicit_disabled_search_wins_over_top_level_enable(tmp_path):
    """An explicit search disable wins over top-level enablement."""
    config = _write_config(
        tmp_path,
        {
            "enable_serpapi": True,
            "search": {"provider": "serpapi", "enabled": False},
        },
    )
    resolved = resolve_command_config(
        command="discover",
        cli_values={},
        config_data=load_runtime_config_file(config),
        strict=True,
    )
    assert resolved["enable_serpapi"] is False


def test_validate_and_execute_reject_invalid_cli_budget(tmp_path):
    """Explicit CLI overrides must obey the same resource bounds as YAML."""
    config = _write_config(
        tmp_path, {"seeds": ["https://example.org/doc.pdf"]}
    )
    for flags in ([], ["--validate-config"]):
        with patch("psweep.discovery.engine.DiscoveryEngine.run") as run:
            invocation = CliRunner().invoke(
                cli,
                [
                    "discover",
                    "--config",
                    str(config),
                    "--max-concurrent-downloads",
                    "0",
                    *flags,
                    "-q",
                ],
            )
        assert invocation.exit_code != 0
        run.assert_not_called()


@pytest.mark.parametrize("block", ["query_families", "retention", "runtime"])
def test_null_blocks_raise_config_errors(tmp_path, block):
    """Explicit null blocks fail cleanly instead of crashing validators."""
    config = _write_config(
        tmp_path,
        {"seeds": ["https://example.org/doc.txt"], block: None},
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
                "seeds": ["https://example.org/doc.txt"],
            },
        },
        strict=True,
    )
    assert resolved["domain"] == (domain or "demo")


@pytest.mark.parametrize(
    "discovery",
    [{"browser_mode": "false"}, {"runtime": {"max_pages": True}}],
)
def test_dictionary_config_uses_discovery_validation(discovery):
    """Direct resolver callers cannot bypass file-loader validation."""
    with pytest.raises(RuntimeConfigError):
        resolve_command_config(
            command="discover",
            cli_values={},
            config_data={"discovery": discovery},
            strict=True,
        )


@pytest.mark.parametrize(
    "links",
    [123, [None], [{"url": "https://example.org/a", "texxt": "a"}]],
)
def test_malformed_index_links_fail_at_config_boundary(tmp_path, links):
    """Seeded index links require a list of URLs or closed URL/text objects."""
    config = _write_config(tmp_path, {"routing": {"index_links": links}})
    with pytest.raises(RuntimeConfigError, match="index_links"):
        load_runtime_config_file(config)


def test_zero_retry_backoffs_are_preserved(tmp_path):
    """Supported zero delays remain zero in the effective request."""
    config = _write_config(
        tmp_path,
        {
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
        strict=True,
    )
    normalized = normalize_discover_inputs(resolved)
    assert normalized["retry_initial_backoff_seconds"] == 0
    assert normalized["retry_max_backoff_seconds"] == 0


def test_python_run_rejects_empty_discovery_before_subprocess(
    tmp_path, schema_path
):
    """Python orchestration checks discovery before starting any stage."""
    config = _write_config(
        tmp_path,
        {"runtime": {"max_pages": 3}},
        extraction={"input_dir": "missing", "schema": str(schema_path)},
        compilation={"input_dir": "missing", "schema": str(schema_path)},
    )
    with (
        patch("psweep.pipeline.subprocess.run") as run,
        pytest.raises(ValueError, match="Discovery input missing"),
    ):
        run_pipeline(config)
    run.assert_not_called()
