"""Normalize discovery config and check its executable input plan.

The runtime resolver merges config and explicit CLI overrides. This
layer checks typed controls and applies defaults to request values
used by discovery. CLI and pipeline preflight share the same plan check.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .constants import DEFAULT_MAX_CONCURRENT_DOWNLOADS
from .engine import DEFAULT_PARTITION_MODE
from .policies import DEFAULT_ROBOTS_POLICY_MODE, DEFAULT_TOS_POLICY_MODE


def normalize_discover_inputs(  # noqa: PLR0913 - mirrors the discover CLI option set (kw-only fallbacks)
    resolved_inputs: dict[str, Any],
    *,
    domain: str | None = None,
    target: str | None = None,
    seed_urls: Sequence[str] = (),
    query: str | None = None,
    state: str | None = None,
    jurisdiction: str | None = None,
    partition_mode: str | None = None,
    target_limit: int | None = None,
    retention_documents: str | None = None,
    digger_provider: str | None = None,
    enable_serpapi: bool = False,
    max_concurrent_downloads: int | None = None,
    min_request_interval_ms: int | None = None,
    robots_policy_mode: str | None = None,
    tos_policy_mode: str | None = None,
    acknowledged_tos_domains: Sequence[str] = (),
    output_documents: str | None = None,
    output_manifest: str | None = None,
    dry_run: bool = False,
    fresh: bool = False,
) -> dict[str, Any]:
    """Apply discovery defaults and infer omitted search enablement.

    Explicit CLI overrides are already merged into ``resolved_inputs``.
    Keyword arguments supply defaults only where no value was resolved.
    Invalid boolean and integer controls fail rather than being coerced.
    """
    ri = resolved_inputs
    for field in (
        "enable_serpapi",
        "selection_exclude_draft",
        "selection_require_supported_document",
        "seeker_cache",
        "browser_mode",
        "dry_run",
        "fresh",
    ):
        if field in ri and not isinstance(ri[field], bool):
            msg = f"Discovery option '{field}' must be a boolean"
            raise ValueError(msg)
    for field, minimum in (
        ("max_concurrent_downloads", 1),
        ("min_request_interval_ms", 0),
        ("target_limit", 1),
        ("seeker_max_results", 1),
        ("link_top_k", 0),
        ("selection_primary_per_target", 1),
        ("selection_max_per_host_per_target", 0),
        ("retry_max_attempts", 1),
    ):
        value = ri.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
        ):
            msg = f"Discovery option '{field}' must be an integer >= {minimum}"
            raise ValueError(msg)

    resolved_target_limit = ri.get("target_limit", target_limit)
    if resolved_target_limit is not None:
        resolved_target_limit = int(resolved_target_limit)

    resolved_targets = ri.get("targets") or None
    total_configured_targets = len(resolved_targets or [])
    if resolved_targets and resolved_target_limit is not None:
        resolved_targets = list(resolved_targets)[:resolved_target_limit]

    # Explicit controls win; infer only from the selected query plan.
    # Config names are already mapped to internal request keys.
    resolved_enable_serpapi = ri.get("enable_serpapi")
    if resolved_enable_serpapi is None:
        resolved_enable_serpapi = enable_serpapi or bool(
            ri.get("query", query)
            or ri.get("query_templates")
            or (ri.get("query_families") or {}).get(ri.get("use_query_family"))
            or any(row.get("query") for row in (resolved_targets or []))
        )

    raw_browser_escalation = ri.get("browser_escalation")
    browser_escalation = (
        dict(raw_browser_escalation)
        if isinstance(raw_browser_escalation, dict)
        else None
    )
    raw_code_host_adapters = ri.get("code_host_adapters")
    code_host_adapters = (
        dict(raw_code_host_adapters)
        if isinstance(raw_code_host_adapters, dict)
        else None
    )
    raw_headers = ri.get("request_headers")
    request_headers = (
        dict(raw_headers) if isinstance(raw_headers, dict) else None
    )
    raw_seeker_extra = ri.get("seeker_extra_params")
    seeker_extra_params = (
        dict(raw_seeker_extra) if isinstance(raw_seeker_extra, dict) else None
    )

    return {
        "domain": ri.get("domain") or domain or target or "default",
        "seed_urls": list(ri.get("seed_urls") or seed_urls),
        "query": ri.get("query", query),
        "state": ri.get("state", state),
        "jurisdiction": ri.get("jurisdiction", jurisdiction),
        "jurisdiction_aliases": ri.get("jurisdiction_aliases") or None,
        "partition_mode": (
            ri.get("partition_mode", partition_mode) or DEFAULT_PARTITION_MODE
        ).lower(),
        "target_limit": resolved_target_limit,
        "retention_documents": str(
            ri.get("retention_documents", retention_documents or "all")
            or "all"
        ).lower(),
        "digger_provider": (
            (ri.get("digger_provider", digger_provider) or "seed_only")
            .strip()
            .lower()
        ),
        "topology_mode": ri.get("topology_mode"),
        "enable_serpapi": resolved_enable_serpapi,
        "hub_pages": ri.get("hub_pages") or None,
        "allowed_domains": ri.get("allowed_domains") or None,
        "targets": resolved_targets,
        "total_configured_targets": total_configured_targets,
        "query_templates": ri.get("query_templates") or None,
        "query_families": ri.get("query_families") or None,
        "use_query_family": ri.get("use_query_family"),
        "seeker_max_results": int(ri.get("seeker_max_results", 10) or 10),
        "link_prioritization_mode": str(
            ri.get("link_prioritization_mode", "heuristic") or "heuristic"
        ).lower(),
        # 0 = no global cap; per-target selection controls recall.
        "link_top_k": int(ri.get("link_top_k", 0) or 0),
        "link_prioritization_keywords": (
            ri.get("link_prioritization_keywords") or None
        ),
        "link_prioritization_domain_scores": (
            ri.get("link_prioritization_domain_scores") or None
        ),
        "link_prioritization_shopping_keywords": (
            ri.get("link_prioritization_shopping_keywords") or None
        ),
        "selection_primary_per_target": int(
            ri.get("selection_primary_per_target", 1) or 1
        ),
        "selection_exclude_draft": bool(
            ri.get("selection_exclude_draft", True)
        ),
        "selection_draft_patterns": (
            ri.get("selection_draft_patterns") or None
        ),
        "selection_relevance_require_any_terms": (
            ri.get("selection_relevance_require_any_terms") or None
        ),
        "selection_relevance_require_legal_marker_terms": (
            ri.get("selection_relevance_require_legal_marker_terms") or None
        ),
        "selection_relevance_exclude_any_terms": (
            ri.get("selection_relevance_exclude_any_terms") or None
        ),
        "selection_exclude_url_patterns": (
            ri.get("selection_exclude_url_patterns") or None
        ),
        "selection_exclude_text_patterns": (
            ri.get("selection_exclude_text_patterns") or None
        ),
        "selection_max_per_host_per_target": int(
            ri.get("selection_max_per_host_per_target", 0) or 0
        ),
        "selection_relevance_allowed_domain_patterns": (
            ri.get("selection_relevance_allowed_domain_patterns") or None
        ),
        "selection_require_supported_document": bool(
            ri.get("selection_require_supported_document", True)
        ),
        "selection_target_identity_require_any_templates": (
            ri.get("selection_target_identity_require_any_templates") or None
        ),
        "selection_target_identity_require_all_templates": (
            ri.get("selection_target_identity_require_all_templates") or None
        ),
        "selection_target_identity_exclude_any_templates": (
            ri.get("selection_target_identity_exclude_any_templates") or None
        ),
        "include_url_patterns": ri.get("include_url_patterns") or None,
        "include_link_text_patterns": (
            ri.get("include_link_text_patterns") or None
        ),
        "index_page_mode": ri.get("index_page_mode") or None,
        "index_links": ri.get("index_links") or None,
        "max_depth": ri.get("max_depth"),
        "max_pages": ri.get("max_pages"),
        "max_files": ri.get("max_files"),
        "timeout_seconds": ri.get("timeout_seconds"),
        "retry_max_attempts": int(ri.get("retry_max_attempts", 3) or 3),
        "retry_initial_backoff_seconds": float(
            ri.get("retry_initial_backoff_seconds", 1.0)
        ),
        "retry_max_backoff_seconds": float(
            ri.get("retry_max_backoff_seconds", 8.0)
        ),
        "max_concurrent_downloads": int(
            ri.get(
                "max_concurrent_downloads",
                max_concurrent_downloads or DEFAULT_MAX_CONCURRENT_DOWNLOADS,
            )
            or DEFAULT_MAX_CONCURRENT_DOWNLOADS
        ),
        "min_request_interval_ms": int(
            ri.get("min_request_interval_ms", min_request_interval_ms or 0)
            or 0
        ),
        "robots_policy_mode": str(
            ri.get(
                "robots_policy_mode",
                robots_policy_mode or DEFAULT_ROBOTS_POLICY_MODE,
            )
            or DEFAULT_ROBOTS_POLICY_MODE
        ).lower(),
        "tos_policy_mode": str(
            ri.get(
                "tos_policy_mode", tos_policy_mode or DEFAULT_TOS_POLICY_MODE
            )
            or DEFAULT_TOS_POLICY_MODE
        ).lower(),
        "acknowledged_tos_domains": list(
            ri.get("acknowledged_tos_domains")
            or acknowledged_tos_domains
            or []
        ),
        "request_headers": request_headers,
        "document_classifier": ri.get("document_classifier") or None,
        "document_review": ri.get("document_review") or None,
        "models": ri.get("models") or None,
        "seeker_cache": bool(ri.get("seeker_cache") or False),
        "seeker_cache_ttl_minutes": float(
            ri.get("seeker_cache_ttl_minutes", 0) or 0
        ),
        "query_context_aliases": ri.get("query_context_aliases") or None,
        "partition_by": ri.get("partition_by") or None,
        "browser_mode": bool(ri.get("browser_mode") or False),
        "browser_escalation": browser_escalation,
        "code_host_adapters": code_host_adapters,
        "seeker_extra_params": seeker_extra_params,
        "output_documents": ri.get("output_documents") or output_documents,
        "output_manifest": ri.get("output_manifest") or output_manifest,
        "dry_run": bool(ri.get("dry_run", dry_run)),
        "fresh": bool(ri.get("fresh", fresh)),
    }


def validate_discover_plan(inputs: dict[str, Any]) -> None:
    """Check executable inputs without contacting external services."""
    has_templates = inputs.get("query_templates") or (
        (inputs.get("query_families") or {}).get(
            inputs.get("use_query_family")
        )
    )
    if not (
        inputs.get("seed_urls")
        or inputs.get("query")
        or inputs.get("targets")
        or inputs.get("hub_pages")
        or (inputs.get("enable_serpapi") and has_templates)
    ):
        msg = (
            "Discovery input missing: provide seed URLs, a query, targets, "
            "hub pages, or enabled search with query templates."
        )
        raise ValueError(msg)
