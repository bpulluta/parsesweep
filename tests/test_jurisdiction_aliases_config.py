"""Tests for config-supplied jurisdiction aliases.

The built-in US-state alias table (active only under
``partition_mode=jurisdiction``) can be supplemented/overridden via
``discovery.jurisdiction_aliases`` so non-US or custom jurisdiction schemes
work. When unconfigured, the US default is used unchanged.
"""

from pathlib import Path

import pytest

from psweep.config.runtime_config_loader import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.discovery.engine import DiscoveryEngine


class TestEffectiveStateAliases:
    def test_default_identity_when_no_extra(self):
        assert (
            DiscoveryEngine._effective_state_aliases(None)
            is DiscoveryEngine._STATE_ALIASES
        )
        assert (
            DiscoveryEngine._effective_state_aliases({})
            is DiscoveryEngine._STATE_ALIASES
        )

    def test_merges_and_config_wins_on_conflict(self):
        merged = DiscoveryEngine._effective_state_aliases(
            {"Ontario": "ON", "Texas": "TX-CUSTOM"}
        )
        assert merged["ontario"] == "on"  # new, lowercased
        assert merged["texas"] == "tx-custom"  # override wins
        assert merged["california"] == "ca"  # built-in default retained


class TestInferJurisdictionWithAliases:
    def test_infers_non_us_jurisdiction_from_custom_alias(self):
        aliases = DiscoveryEngine._effective_state_aliases({"ontario": "on"})
        jurisdiction, state = DiscoveryEngine._infer_jurisdiction_from_query(
            "Toronto City Ontario zoning permits", aliases
        )
        assert state == "on"
        assert jurisdiction == "Toronto City"

    def test_default_us_inference_unchanged(self):
        jurisdiction, state = DiscoveryEngine._infer_jurisdiction_from_query(
            "Fairfax County Virginia ordinance"
        )
        assert state == "va"
        assert jurisdiction == "Fairfax County"


class TestNormalizeStateKey:
    def test_custom_alias_resolved(self):
        aliases = DiscoveryEngine._effective_state_aliases({"ontario": "on"})
        assert (
            DiscoveryEngine._normalize_state_key("Ontario", aliases) == "on"
        )

    def test_default_alias_unchanged(self):
        assert DiscoveryEngine._normalize_state_key("Virginia") == "va"


class TestJurisdictionAliasesResolve:
    def test_resolve_maps_jurisdiction_aliases(self, tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  seeds:
    - https://example.ca/hub
  partition_mode: jurisdiction
  jurisdiction_aliases:
    ontario: "on"
    quebec: "qc"
""",
            encoding="utf-8",
        )
        config_data = load_runtime_config_file(run_path)
        resolved = resolve_command_config(
            command="discover",
            cli_values={},
            config_data=config_data,
            strict=True,
        )
        assert resolved["jurisdiction_aliases"] == {
            "ontario": "on",
            "quebec": "qc",
        }

    def test_invalid_alias_map_raises(self, tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  seeds:
    - https://example.ca/hub
  jurisdiction_aliases:
    ontario: 5
""",
            encoding="utf-8",
        )
        with pytest.raises(
            RuntimeConfigError, match="jurisdiction_aliases"
        ):
            load_runtime_config_file(run_path)

    def test_non_dict_alias_map_raises(self, tmp_path: Path):
        run_path = tmp_path / "run.yaml"
        run_path.write_text(
            """
discovery:
  seeds:
    - https://example.ca/hub
  jurisdiction_aliases:
    - ontario
""",
            encoding="utf-8",
        )
        with pytest.raises(
            RuntimeConfigError, match="jurisdiction_aliases"
        ):
            load_runtime_config_file(run_path)
