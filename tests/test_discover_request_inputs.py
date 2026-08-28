"""Tests for normalize_discover_inputs (the extracted discover default layer).

Locks the runtime defaults + coercions that used to live inline in the discover
CLI command, and verifies every shipped config resolves + normalizes cleanly.
"""

from pathlib import Path
import glob

from psweep.config.runtime_config_loader import (
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.discovery import (
    DEFAULT_PARTITION_MODE,
    DEFAULT_ROBOTS_POLICY_MODE,
    DEFAULT_TOS_POLICY_MODE,
)
from psweep.discovery.request_inputs import normalize_discover_inputs


class TestDefaults:
    def test_empty_inputs_apply_all_defaults(self):
        n = normalize_discover_inputs({})
        assert n["domain"] == "default"
        assert n["seed_urls"] == []
        assert n["partition_mode"] == DEFAULT_PARTITION_MODE
        assert n["retention_documents"] == "all"
        assert n["digger_provider"] == "seed_only"
        assert n["enable_serpapi"] is False
        assert n["seeker_max_results"] == 10
        assert n["link_prioritization_mode"] == "heuristic"
        assert n["link_top_k"] == 0
        assert n["selection_primary_per_target"] == 1
        assert n["retry_max_attempts"] == 3
        assert n["retry_initial_backoff_seconds"] == 1.0
        assert n["retry_max_backoff_seconds"] == 8.0
        assert n["max_concurrent_downloads"] == 2
        assert n["min_request_interval_ms"] == 0
        assert n["robots_policy_mode"] == DEFAULT_ROBOTS_POLICY_MODE
        assert n["tos_policy_mode"] == DEFAULT_TOS_POLICY_MODE
        assert n["acknowledged_tos_domains"] == []
        assert n["dry_run"] is False
        assert n["fresh"] is False
        assert n["browser_escalation"] is None
        assert n["seeker_extra_params"] is None


class TestCliFallbacks:
    def test_target_is_domain_fallback(self):
        n = normalize_discover_inputs({}, target="my-target")
        assert n["domain"] == "my-target"

    def test_config_domain_wins_over_target(self):
        n = normalize_discover_inputs({"domain": "cfgdom"}, target="t")
        assert n["domain"] == "cfgdom"

    def test_cli_fallback_used_only_when_absent(self):
        # partition_mode absent from config -> falls back to CLI value.
        n = normalize_discover_inputs({}, partition_mode="jurisdiction")
        assert n["partition_mode"] == "jurisdiction"
        # config value overrides the CLI fallback.
        n2 = normalize_discover_inputs(
            {"partition_mode": "host"}, partition_mode="jurisdiction"
        )
        assert n2["partition_mode"] == "host"

    def test_seed_urls_from_cli(self):
        n = normalize_discover_inputs({}, seed_urls=("https://a", "https://b"))
        assert n["seed_urls"] == ["https://a", "https://b"]


class TestCoercions:
    def test_case_and_type_coercions(self):
        n = normalize_discover_inputs(
            {
                "partition_mode": "JURISDICTION",
                "retention_documents": "CURATED",
                "digger_provider": "  SerpApi ",
                "seeker_max_results": "25",
                "enable_serpapi": 1,
                "max_concurrent_downloads": "4",
            }
        )
        assert n["partition_mode"] == "jurisdiction"
        assert n["retention_documents"] == "curated"
        assert n["digger_provider"] == "serpapi"
        assert n["seeker_max_results"] == 25
        assert n["enable_serpapi"] is True
        assert n["max_concurrent_downloads"] == 4

    def test_zero_falls_back_via_or(self):
        # A configured 0 for max_concurrent_downloads is falsy -> default 2
        # (preserves the former inline `... or 2` behavior exactly).
        n = normalize_discover_inputs({"max_concurrent_downloads": 0})
        assert n["max_concurrent_downloads"] == 2

    def test_targets_slicing_and_total_count(self):
        targets = [{"label": f"t{i}"} for i in range(5)]
        n = normalize_discover_inputs(
            {"targets": targets, "target_limit": 2}
        )
        assert n["total_configured_targets"] == 5  # pre-slice count
        assert len(n["targets"]) == 2  # sliced

    def test_browser_escalation_dict_copied(self):
        block = {"min_shell_chars": 8000}
        n = normalize_discover_inputs({"browser_escalation": block})
        assert n["browser_escalation"] == block
        assert n["browser_escalation"] is not block  # copied
        # Non-dict is dropped to None.
        assert normalize_discover_inputs(
            {"browser_escalation": True}
        )["browser_escalation"] is None

    def test_code_host_adapters_dict_copied(self):
        block = {"enabled": False}
        n = normalize_discover_inputs({"code_host_adapters": block})
        assert n["code_host_adapters"] == block
        assert n["code_host_adapters"] is not block  # copied
        # Non-dict is dropped to None (default-enabled handled downstream).
        assert normalize_discover_inputs(
            {"code_host_adapters": True}
        )["code_host_adapters"] is None
        # Absent -> None (engine treats None as enabled).
        assert normalize_discover_inputs({})["code_host_adapters"] is None


def test_all_shipped_configs_resolve_and_normalize():
    """Every shipped discover config resolves + normalizes without error."""
    configs = sorted(glob.glob("config/*/run.yaml"))
    assert configs, "no shipped configs found"
    for path in configs:
        data = load_runtime_config_file(Path(path))
        if not data.get("discovery"):
            continue
        resolved = resolve_command_config(
            command="discover",
            cli_values={},
            config_data=data,
            strict=True,
        )
        n = normalize_discover_inputs(resolved)
        # Sanity: required-shaped values are always present + typed.
        assert isinstance(n["domain"], str) and n["domain"]
        assert isinstance(n["seed_urls"], list)
        assert isinstance(n["seeker_max_results"], int)
        assert isinstance(n["max_concurrent_downloads"], int)
        assert n["partition_mode"] in {"auto", "jurisdiction", "host"}
