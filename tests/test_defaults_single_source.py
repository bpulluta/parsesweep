"""Guard the single-source-of-truth contract for CLI/runtime defaults.

Each user-facing default is defined by exactly one named constant and
referenced everywhere else (dataclass field, CLI option, resolution fallback,
evaluator signature). These tests fail loudly if any layer drifts from the
constant, which is how the ``--help`` display, the resolved runtime value, and
the dataclass default are kept provably identical.
"""

from __future__ import annotations

import dataclasses
import inspect

from psweep.cli.main import cli
from psweep.discovery import (
    DEFAULT_PARTITION_MODE,
    DEFAULT_ROBOTS_POLICY_MODE,
    DEFAULT_TOS_POLICY_MODE,
    DiscoveryRequest,
)
from psweep.discovery.policies import DiscoveryPolicyEvaluator
from psweep.extraction.llm_factory import DEFAULT_MODEL


def _option_defaults(command_name: str) -> dict[str, object]:
    """Return a {param_name: default} map for a registered CLI command."""
    return {p.name: p.default for p in cli.commands[command_name].params}


def test_policy_and_partition_constant_values():
    """The intended defaults are the safe, production-facing values."""
    assert DEFAULT_ROBOTS_POLICY_MODE == "warn"
    assert DEFAULT_TOS_POLICY_MODE == "warn"
    assert DEFAULT_PARTITION_MODE == "auto"


def test_discovery_request_defaults_match_constants():
    """DiscoveryRequest field defaults come from the shared constants."""
    field_defaults = {
        f.name: f.default for f in dataclasses.fields(DiscoveryRequest)
    }
    assert field_defaults["partition_mode"] == DEFAULT_PARTITION_MODE
    assert field_defaults["robots_policy_mode"] == DEFAULT_ROBOTS_POLICY_MODE
    assert field_defaults["tos_policy_mode"] == DEFAULT_TOS_POLICY_MODE


def test_policy_evaluator_signature_matches_constants():
    """evaluate() applies the same defaults as the rest of the system."""
    params = inspect.signature(DiscoveryPolicyEvaluator.evaluate).parameters
    assert params["robots_policy_mode"].default == DEFAULT_ROBOTS_POLICY_MODE
    assert params["tos_policy_mode"].default == DEFAULT_TOS_POLICY_MODE


def test_discover_cli_option_defaults_match_constants():
    """discover --help defaults equal the shared constants (no drift/lying)."""
    defaults = _option_defaults("discover")
    assert defaults["partition_mode"] == DEFAULT_PARTITION_MODE
    assert defaults["robots_policy_mode"] == DEFAULT_ROBOTS_POLICY_MODE
    assert defaults["tos_policy_mode"] == DEFAULT_TOS_POLICY_MODE


def test_extract_cli_option_defaults_are_single_sourced():
    """extract --model is sourced from DEFAULT_MODEL; --max-context is 600k."""
    defaults = _option_defaults("extract")
    assert defaults["model"] == DEFAULT_MODEL
    assert defaults["max_context"] == 600000
