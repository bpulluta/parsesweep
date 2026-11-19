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
    DEFAULT_ROBOTS_POLICY_MODE,
    DEFAULT_TOS_POLICY_MODE,
    DiscoveryRequest,
)
from psweep.compilation.input_provenance import DEFAULT_PROVENANCE_POLICY
from psweep.config.runtime_config_loader import VARIABLE_CATALOG
from psweep.discovery import constants as discovery_constants
from psweep.discovery.document_reviewer import DocumentReviewer
from psweep.discovery.policies import DiscoveryPolicyEvaluator
from psweep.extraction.llm_factory import resolve_model_name
from psweep.extraction.llm_client import LLMClient
from psweep.extraction.llm_factory import (
    DEFAULT_LLM_TIMEOUT_SECONDS,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_CONTEXT,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER_MODELS,
)
from psweep.extraction.page_locator import (
    DEFAULT_MAX_SELECTED_PAGES,
    DEFAULT_PAGE_TRIGGER_CHARS,
    PageLocator,
)
from psweep.utils.schema_metadata import DEFAULT_OUTPUT_FORMAT


def _option_defaults(command_name: str) -> dict[str, object]:
    """Return a {param_name: default} map for a registered CLI command."""
    return {p.name: p.default for p in cli.commands[command_name].params}


def test_policy_constant_values():
    """The intended defaults are the safe, production-facing values."""
    assert DEFAULT_ROBOTS_POLICY_MODE == "warn"
    assert DEFAULT_TOS_POLICY_MODE == "warn"


def test_discovery_request_defaults_match_constants():
    """DiscoveryRequest field defaults come from the shared constants."""
    field_defaults = {f.name: f.default for f in dataclasses.fields(DiscoveryRequest)}
    assert field_defaults["robots_policy_mode"] == DEFAULT_ROBOTS_POLICY_MODE
    assert field_defaults["tos_policy_mode"] == DEFAULT_TOS_POLICY_MODE


def test_policy_evaluator_signature_matches_constants():
    """evaluate() applies the same defaults as the rest of the system."""
    params = inspect.signature(DiscoveryPolicyEvaluator.evaluate).parameters
    assert params["robots_policy_mode"].default == DEFAULT_ROBOTS_POLICY_MODE
    assert params["tos_policy_mode"].default == DEFAULT_TOS_POLICY_MODE


def test_extract_cli_option_defaults_are_single_sourced():
    """extract --model and --max-context come from the shared constants."""
    defaults = _option_defaults("extract")
    assert defaults["model"] is None
    assert resolve_model_name(None, llm_config={}) == DEFAULT_MODEL
    assert defaults["max_context"] == DEFAULT_MAX_CONTEXT


def test_max_context_default_is_single_sourced_across_layers():
    """CLI, public pipeline API, and DocumentExtractor share DEFAULT_MAX_CONTEXT."""
    from psweep.extraction.document_extractor import DocumentExtractor
    from psweep.pipeline import extract_documents

    assert DEFAULT_MAX_CONTEXT == 600000
    for func, param in (
        (extract_documents, "max_context"),
        (DocumentExtractor.__init__, "max_context_chars"),
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


def test_review_and_classifier_defaults_are_single_sourced():
    c = discovery_constants
    params = inspect.signature(DocumentReviewer.__init__).parameters
    assert params["keep_top"].default == c.DEFAULT_REVIEW_KEEP_TOP == 1
    assert params["max_chars"].default == c.DEFAULT_REVIEW_MAX_CHARS == 12000
    assert params["deduplicate_redundant"].default is c.DEFAULT_REVIEW_DEDUPLICATE_REDUNDANT
    assert c.DEFAULT_REVIEW_DEDUPLICATE_REDUNDANT is True
    assert params["prefer_newest"].default is c.DEFAULT_REVIEW_PREFER_NEWEST
    assert c.DEFAULT_REVIEW_PREFER_NEWEST is False
    assert c.DEFAULT_CLASSIFIER_MIN_REQUIRED_MATCHES == 1
    assert c.DEFAULT_CLASSIFIER_ACTION == "warn"
    assert c.DEFAULT_PDF_SCREENING == "off"


def test_discovery_timeout_defaults():
    c = discovery_constants
    assert (
        c.DEFAULT_SEARCH_TIMEOUT_SECONDS,
        c.DEFAULT_POLICY_TIMEOUT_SECONDS,
        c.DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        c.DEFAULT_CRAWL_TIMEOUT_SECONDS,
        c.DEFAULT_CODE_HOST_TIMEOUT_SECONDS,
    ) == (45, 15, 60, 30, 30)


def test_llm_timeout_and_provider_fallbacks_are_single_sourced():
    assert LLMClient.DEFAULT_TIMEOUT == DEFAULT_LLM_TIMEOUT_SECONDS == 120
    assert DEFAULT_PROVIDER_MODELS == {
        "anthropic": "claude-3.5-sonnet",
        "gemini": "gemini-1.5-pro",
    }


def test_compile_defaults_are_single_sourced():
    assert DEFAULT_OUTPUT_FORMAT == "both"
    assert DEFAULT_PROVENANCE_POLICY == "warn"
    assert _option_defaults("compile")["provenance_policy"] == DEFAULT_PROVENANCE_POLICY


def test_catalog_help_quotes_constants():
    extraction = {row["name"]: row["description"] for row in VARIABLE_CATALOG["extraction"]}
    assert str(DEFAULT_MAX_CONCURRENT) in extraction["max_concurrent"]
    assert f"{DEFAULT_LLM_TIMEOUT_SECONDS}s" in extraction["timeout_seconds"]
