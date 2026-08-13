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
from psweep.extraction.llm_factory import DEFAULT_MAX_CONTEXT, DEFAULT_MODEL
from psweep.extraction.page_locator import (
    DEFAULT_MAX_SELECTED_PAGES,
    DEFAULT_PAGE_TRIGGER_CHARS,
    PageLocator,
)


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
    """extract --model and --max-context come from the shared constants."""
    defaults = _option_defaults("extract")
    assert defaults["model"] == DEFAULT_MODEL
    assert defaults["max_context"] == DEFAULT_MAX_CONTEXT


def test_max_context_default_is_single_sourced_across_layers():
    """CLI, public pipeline API, and DocumentExtractor share DEFAULT_MAX_CONTEXT.

    Guards against the pre-audit drift where ``extract``/``run`` used 600k but
    ``validate`` and the library API used 400k — validating on less context than
    extraction produced.
    """
    from psweep.extraction.document_extractor import DocumentExtractor
    from psweep.pipeline import extract_documents
    from psweep.validation.multi_model_extractor import (
        run_multi_model_extraction,
    )

    assert DEFAULT_MAX_CONTEXT == 600000
    for func, param in (
        (extract_documents, "max_context"),
        (DocumentExtractor.__init__, "max_context_chars"),
        (run_multi_model_extraction, "max_context_chars"),
    ):
        sig = inspect.signature(func)
        assert sig.parameters[param].default == DEFAULT_MAX_CONTEXT


def test_page_target_defaults_are_single_sourced():
    """PageLocator defaults come from the shared page-target constants."""
    params = inspect.signature(PageLocator.__init__).parameters
    assert params["trigger_chars"].default == DEFAULT_PAGE_TRIGGER_CHARS
    assert params["max_selected_pages"].default == DEFAULT_MAX_SELECTED_PAGES
    assert DEFAULT_PAGE_TRIGGER_CHARS == 200_000
    assert DEFAULT_MAX_SELECTED_PAGES == 30
