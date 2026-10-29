"""Jurisdiction aliases are explicit domain inputs, not US defaults."""

from pathlib import Path

import pytest

from psweep.config.runtime_config_loader import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.discovery.engine import DiscoveryEngine


class TestEffectiveStateAliases:
    def test_no_unconfigured_geographic_aliases(self):
        assert DiscoveryEngine._effective_state_aliases(None) == {}
        assert DiscoveryEngine._effective_state_aliases({}) == {}

    def test_only_configured_aliases_are_normalized(self):
        aliases = DiscoveryEngine._effective_state_aliases(
            {"Ontario": "ON", "Texas": "TX-CUSTOM"}
        )
        assert aliases == {"ontario": "on", "texas": "tx-custom"}
        assert "california" not in aliases


class TestNormalizeStateKey:
    def test_custom_alias_resolved(self):
        aliases = DiscoveryEngine._effective_state_aliases({"ontario": "on"})
        assert (
            DiscoveryEngine._normalize_state_key("Ontario", aliases) == "on"
        )

    def test_absent_alias_uses_generic_slug(self):
        assert DiscoveryEngine._normalize_state_key("Virginia") == "virginia"


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
