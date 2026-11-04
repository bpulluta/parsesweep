"""Discovery config schema: strict validation and merge into request fields.

Each schema key maps to a nested schema or to a ``_Key`` whose check
validates (and may normalize) the value and whose ``dest`` names the
``DiscoveryRequest`` field it feeds.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psweep.discovery.constants import RESERVED_SEARCH_PARAMS
from psweep.discovery.targets import TargetSpecError, resolve_targets_spec
from psweep.discovery.urls import is_http_url

from .errors import RuntimeConfigError, reject_unknown_keys

Check = Callable[[Any, str], Any]


@dataclass(frozen=True)
class _Key:
    check: Check
    dest: str | None = None


def _fail(location: str, expected: str) -> None:
    raise RuntimeConfigError(f"'{location}' must be {expected}")


def _bool(value: Any, loc: str) -> bool:
    if not isinstance(value, bool):
        _fail(loc, "a boolean")
    return value


def _int(minimum: int) -> Check:
    def check(value: Any, loc: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            _fail(loc, f"an integer >= {minimum}")
        return value
    return check


def _number(value: Any, loc: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        _fail(loc, "a finite number >= 0")
    return float(value)


def _string(value: Any, loc: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(loc, "a non-empty string")
    return value


def _choice(*options: str) -> Check:
    def check(value: Any, loc: str) -> str:
        if value not in options:
            _fail(loc, "one of: " + ", ".join(options))
        return value
    return check


def _strings(value: Any, loc: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        _fail(loc, "an array of non-empty strings")
    return value


def _patterns(value: Any, loc: str) -> list[str]:
    for pattern in _strings(value, loc):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RuntimeConfigError(
                f"'{loc}' has an invalid regular expression: {exc}"
            ) from exc
    return value


def _urls(value: Any, loc: str) -> list[str]:
    for url in _strings(value, loc):
        if not is_http_url(url):
            _fail(loc, f"http(s) URLs with a host (got {url!r})")
    return [url.strip() for url in value]


def _string_map(value: Any, loc: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
        for k, v in value.items()
    ):
        _fail(loc, "a map of non-empty strings to non-empty strings")
    return value


def _domain_scores(value: Any, loc: str) -> dict[str, float]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and k.strip()
        and isinstance(v, (int, float)) and not isinstance(v, bool)
        for k, v in value.items()
    ):
        _fail(loc, "a map of domain substrings to numbers")
    return value


def _search_params(value: Any, loc: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(loc, "an object")
    reserved = RESERVED_SEARCH_PARAMS.intersection(value)
    if reserved:
        raise RuntimeConfigError(
            f"'{loc}' has reserved keys: {', '.join(sorted(reserved))}"
        )
    return value


def _walk(
    values: Any,
    schema: dict[str, Any],
    loc: str,
    out: dict[str, tuple[Any, str]] | None,
) -> None:
    if not isinstance(values, dict):
        _fail(loc, "an object")
    reject_unknown_keys(values, schema, loc)
    for key, value in values.items():
        rule, where = schema[key], f"{loc}.{key}"
        if isinstance(rule, dict):
            _walk(value, rule, where, out)
            continue
        normalized = rule.check(value, where)
        if rule.dest and out is not None:
            out[rule.dest] = (normalized, f"config.{where}")


def _block(schema: dict[str, Any], *, toggle: bool = False) -> Check:
    """Validate a nested block that is passed through as one value."""
    def check(value: Any, loc: str) -> dict[str, Any]:
        if toggle and isinstance(value, bool):
            return {"enabled": value}
        _walk(value, schema, loc, None)
        return dict(value)
    return check


_POLICY_MODE = _choice("ignore", "warn", "enforce")

DISCOVERY_SCHEMA: dict[str, Any] = {
    # ``targets`` is validated separately: it needs the config directory.
    "targets": _Key(lambda value, loc: value, "targets"),
    "sources": _Key(_urls, "sources"),
    "queries": _Key(_strings, "queries"),
    "partition_by": _Key(_strings, "partition_by"),
    "crawl": {
        "provider": _Key(_choice("http", "selenium"), "crawl_provider"),
        "max_depth": _Key(_int(0), "max_depth"),
        "max_pages": _Key(_int(1), "max_pages"),
        "allowed_domains": _Key(_strings, "allowed_domains"),
        "include_url_patterns": _Key(_patterns, "include_url_patterns"),
        "include_link_text_patterns": _Key(_patterns, "include_link_text_patterns"),
        "browser_downloads": _Key(_bool, "browser_downloads"),
        "browser_escalation": _Key(
            _block(
                {
                    "enabled": _Key(_bool),
                    "settle_seconds": _Key(_number),
                    "min_shell_chars": _Key(_int(0)),
                    "min_rendered_chars": _Key(_int(0)),
                },
                toggle=True,
            ),
            "browser_escalation",
        ),
        "code_host_adapters": _Key(
            _block({"enabled": _Key(_bool)}, toggle=True), "code_host_adapters"
        ),
        "request_headers": _Key(_string_map, "request_headers"),
    },
    "runtime": {
        "max_files": _Key(_int(1), "max_files"),
        "timeout_seconds": _Key(_int(1), "timeout_seconds"),
        "max_concurrent_downloads": _Key(_int(1), "max_concurrent_downloads"),
        "min_request_interval_ms": _Key(_int(0), "min_request_interval_ms"),
    },
    "search": {
        "max_results": _Key(_int(1), "seeker_max_results"),
        "cache": _Key(_bool, "seeker_cache"),
        "cache_ttl_minutes": _Key(_number, "seeker_cache_ttl_minutes"),
        "params": _Key(_search_params, "seeker_params"),
    },
    "selection": {
        "max_per_target": _Key(_int(1), "selection_primary_per_target"),
        "exclude_draft": _Key(_bool, "selection_exclude_draft"),
        "draft_patterns": _Key(_patterns, "selection_draft_patterns"),
        "require_any": _Key(_strings, "selection_relevance_require_any_terms"),
        "relevance_require_legal_marker_terms": _Key(
            _strings, "selection_relevance_require_legal_marker_terms"
        ),
        "exclude": _Key(_strings, "selection_relevance_exclude_any_terms"),
        "exclude_url_patterns": _Key(_patterns, "selection_exclude_url_patterns"),
        "exclude_text_patterns": _Key(_patterns, "selection_exclude_text_patterns"),
        "max_per_host_per_target": _Key(_int(0), "selection_max_per_host_per_target"),
        "relevance_allowed_domain_patterns": _Key(
            _strings, "selection_relevance_allowed_domain_patterns"
        ),
        "require_supported_document": _Key(
            _bool, "selection_require_supported_document"
        ),
        "target_identity_require_any_templates": _Key(
            _strings, "selection_target_identity_require_any_templates"
        ),
        "target_identity_require_all_templates": _Key(
            _strings, "selection_target_identity_require_all_templates"
        ),
        "target_identity_exclude_any_templates": _Key(
            _strings, "selection_target_identity_exclude_any_templates"
        ),
    },
    "link_prioritization": {
        "mode": _Key(_choice("heuristic", "off"), "link_prioritization_mode"),
        "top_k": _Key(_int(0), "link_top_k"),
        "keywords": _Key(_strings, "link_prioritization_keywords"),
        "domain_scores": _Key(_domain_scores, "link_prioritization_domain_scores"),
        "shopping_path_keywords": _Key(
            _strings, "link_prioritization_shopping_keywords"
        ),
    },
    "document_classifier": _Key(
        _block(
            {
                "required_keywords": _Key(_strings),
                "nice_to_have_keywords": _Key(_strings),
                "min_required_matches": _Key(_int(0)),
                "action": _Key(_choice("warn", "filter")),
            }
        ),
        "document_classifier",
    ),
    "document_review": _Key(
        _block(
            {
                "document_description": _Key(_string),
                "model": _Key(_string),
                "keep_top": _Key(_int(1)),
                "max_chars": _Key(_int(1)),
                "keywords": _Key(_strings),
                "deduplicate_redundant": _Key(_bool),
                "deduplication": {"key_fields": _Key(_strings)},
            }
        ),
        "document_review",
    ),
    "retry_policy": {
        "max_attempts": _Key(_int(1), "retry_max_attempts"),
        "initial_backoff_seconds": _Key(_number, "retry_initial_backoff_seconds"),
        "max_backoff_seconds": _Key(_number, "retry_max_backoff_seconds"),
    },
    "policy": {
        "robots_mode": _Key(_POLICY_MODE, "robots_policy_mode"),
        "tos_mode": _Key(_POLICY_MODE, "tos_policy_mode"),
        "acknowledged_tos_domains": _Key(_strings, "acknowledged_tos_domains"),
    },
    "retention": {
        "documents": _Key(_choice("all", "curated", "none"), "retention_documents"),
    },
}


def _check_review_capacity(fields: dict[str, tuple[Any, str]]) -> None:
    max_per_target = fields.get("selection_primary_per_target", (None,))[0]
    keep_top = (fields.get("document_review", ({},))[0]).get("keep_top")
    if max_per_target is not None and keep_top is not None and max_per_target < keep_top:
        raise RuntimeConfigError(
            "'discovery.selection.max_per_target' must be >= "
            "'discovery.document_review.keep_top'"
        )


def discovery_key_paths(
    schema: dict[str, Any] = DISCOVERY_SCHEMA, loc: str = "discovery"
) -> dict[str, str]:
    """Map each request field to the config key path users write."""
    paths: dict[str, str] = {}
    for key, rule in schema.items():
        if isinstance(rule, dict):
            paths.update(discovery_key_paths(rule, f"{loc}.{key}"))
        elif rule.dest:
            paths[rule.dest] = f"{loc}.{key}"
    return paths


def resolve_discovery_section(
    section: Any, config_path: str | None
) -> dict[str, tuple[Any, str]]:
    """Validate ``discovery`` and return ``{request_field: (value, source)}``.

    Errors name the config file and the full key path.
    """
    fields: dict[str, tuple[Any, str]] = {}
    try:
        _walk(section, DISCOVERY_SCHEMA, "discovery", fields)
        _check_review_capacity(fields)
        if "targets" in fields:
            config_dir = Path(config_path).parent if config_path else Path.cwd()
            partition_by = fields.get("partition_by", (None,))[0]
            spec = resolve_targets_spec(fields["targets"][0], config_dir, partition_by)
            fields["targets"] = (spec, "config.discovery.targets")
    except (RuntimeConfigError, TargetSpecError) as exc:
        prefix = f"{config_path}: " if config_path else ""
        raise RuntimeConfigError(f"{prefix}{exc}") from exc
    return fields
