"""Shared runtime config loader and resolver for CLI commands."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import yaml


class RuntimeConfigError(ValueError):
    """Raised when runtime config cannot be loaded or validated."""


VARIABLE_CATALOG: dict[str, list[dict[str, str]]] = {
    "global": [
        {
            "name": "domain",
            "level": "required",
            "description": (
                "Domain key used for organizing config and outputs."
            ),
        },
    ],
    "extraction": [
        {
            "name": "input_dir",
            "level": "required",
            "description": "Source document file or directory to process.",
        },
        {
            "name": "schema",
            "level": "required",
            "description": "Schema path used during extraction.",
        },
        {
            "name": "output_dir",
            "level": "optional",
            "description": "Override destination for extracted JSON records.",
        },
        {
            "name": "pages_csv",
            "level": "optional",
            "description": "CSV mapping file names to page ranges (from pages.csv or CLI --pages-csv).",
        },
        {
            "name": "pages",
            "level": "optional",
            "description": "Single-file page range (for example: 10-35, from pages.range or CLI --pages).",
        },
        {
            "name": "page_targeting",
            "level": "optional",
            "description": (
                "LLM-assisted page selection for large documents "
                "(from pages.auto_locate)."
            ),
        },
        {
            "name": "profile",
            "level": "optional",
            "description": "Runtime profile id used for artifact lineage.",
        },
        {
            "name": "provider",
            "level": "optional",
            "description": (
                "LLM provider override "
                "(openai, azure, anthropic, gemini, auto)."
            ),
        },
        {
            "name": "model",
            "level": "optional",
            "description": "Model override for extraction runs.",
        },
        {
            "name": "limit",
            "level": "optional",
            "description": "Process only the first N files.",
        },
        {
            "name": "max_context",
            "level": "advanced",
            "description": "Maximum characters to pass to extraction context.",
        },
        {
            "name": "skip_existing",
            "level": "advanced",
            "description": "Skip files that already have output JSON.",
        },
        {
            "name": "enable_qaqc",
            "level": "advanced",
            "description": "Enable multi-model QA/QC mode for extraction.",
        },
        {
            "name": "qaqc_lane",
            "level": "advanced",
            "description": "Lane hint for follow-up compare workflow.",
        },
        {
            "name": "live_dashboard",
            "level": "advanced",
            "description": "Enable live rich dashboard for multi-file runs.",
        },
    ],
    "compilation": [
        {
            "name": "input_dir",
            "level": "required",
            "description": "Directory of extraction JSON records.",
        },
        {
            "name": "schema",
            "level": "required",
            "description": "Schema path used during extraction.",
        },
        {
            "name": "output_dir",
            "level": "optional",
            "description": "Override destination for compiled output.",
        },
        {
            "name": "deduplication",
            "level": "optional",
            "description": "Deduplication settings: key_fields, ignore_fields, strategy.",
        },
        {
            "name": "output",
            "level": "optional",
            "description": "Output formatting: column_order, exclude_fields, column_renames.",
        },
        {
            "name": "normalization",
            "level": "optional",
            "description": "Field normalization (e.g., state abbreviation).",
        },
        {
            "name": "dry_run",
            "level": "optional",
            "description": "Preview deduplication without writing files.",
        },
        {
            "name": "report_format",
            "level": "optional",
            "description": "Dry-run output format (text or json).",
        },
        {
            "name": "fail_on_suspicious",
            "level": "advanced",
            "description": (
                "Fail threshold for suspicious dedup groups in dry-run."
            ),
        },
    ],
    "discovery": [
        {
            "name": "seeds",
            "level": "required",
            "description": "Seed URLs for web discovery.",
        },
        {
            "name": "allowed_domains",
            "level": "optional",
            "description": "Restrict crawl scope to approved hostnames.",
        },
        {
            "name": "enable_serpapi",
            "level": "optional",
            "description": "Enable optional SerpApi seeker provider.",
        },
        {
            "name": "discovery_rules",
            "level": "optional",
            "description": (
                "URL/text include patterns and crawl discovery settings."
            ),
        },
        {
            "name": "keyword_validation",
            "level": "advanced",
            "description": (
                "Content checks used to validate discovered documents."
            ),
        },
        {
            "name": "runtime.max_concurrent_downloads",
            "level": "advanced",
            "description": "Max parallel file downloads in discovery runs.",
        },
        {
            "name": "runtime.min_request_interval_ms",
            "level": "advanced",
            "description": "Min delay between discovery requests (ms).",
        },
        {
            "name": "policy.robots_mode",
            "level": "advanced",
            "description": "Robots policy: ignore, warn, or enforce.",
        },
        {
            "name": "policy.tos_mode",
            "level": "advanced",
            "description": "ToS mode: ignore, warn, or enforce.",
        },
    ],
}

_SECTION_ALIASES = {
    "discover": "discovery",
    "discovery": "discovery",
    "extract": "extraction",
    "extraction": "extraction",
    "compile": "compilation",
    "compilation": "compilation",
}

_ALLOWED_TOP_LEVEL = {
    "domain",
    "models",
    "model_context_windows",
    "discovery",
    "extraction",
    "compilation",
    "qaqc",
}
_ALLOWED_SECTION_FIELDS = {
    "discovery": {
        "domain",
        "seeds",
        # --- shorthand keys (simple form) ---
        "queries",           # list of templates; auto-creates _default family
        "follow_links",      # bool; true=distributed crawl, false=direct only
        "request_delay_ms",  # int; alias for runtime.min_request_interval_ms
        # --- full-form keys ---
        "query",
        "state",
        "jurisdiction",
        "partition_mode",
        "enable_serpapi",
        "query_templates",
        "query_families",
        "allowed_domains",
        "targets",
        "targets_csv",
        "hub_pages",
        "pipeline",
        "topology",
        "search",
        "seeker",
        "digger",
        "discovery_rules",
        "file_filters",
        "keyword_validation",
        "runtime",
        "output",
        "output_documents",
        "output_manifest",
        "dedupe",
        "routing",
        "scoring",
        "retry_policy",
        "link_prioritization",
        "selection",
        "policy",
        "request_headers",
        "dry_run",
        "document_classifier",
        "document_review",
        "query_context_aliases",
        "partition_by",
        "browser_mode",
        "browser",
    },
    "extraction": {
        "input_dir",
        "schema",
        "output_dir",
        "pages",           # unified block (csv + auto_locate); normalized to pages_csv / page_targeting internally
        "pages_csv",       # internal (set by _normalize_pages_block or CLI --pages-csv)
        "page_targeting",  # internal (set by _normalize_pages_block)
        "profile",
        "provider",
        "model",
        "limit",
        "skip_existing",
        "max_context",
        "enable_qaqc",
        "qaqc_lane",
        "live_dashboard",
    },
    "compilation": {
        "input_dir",
        "schema",
        "output_dir",
        "output",
        "deduplication",
        "normalization",
        "dry_run",
        "report_format",
        "fail_on_suspicious",
        "synthesis",
    },
}

_ALLOWED_TOPOLOGY_MODES = {"distributed", "centralized", "hybrid"}
_ACQUISITION_LIST_FIELDS = {
    "seeds",
    "queries",
    "query_templates",
    "allowed_domains",
    "targets",
    "hub_pages",
    "pipeline",
}
_ACQUISITION_OBJECT_FIELDS = {
    "topology",
    "query_families",
    "search",
    "seeker",
    "digger",
    "discovery_rules",
    "file_filters",
    "keyword_validation",
    "runtime",
    "output",
    "dedupe",
    "routing",
    "scoring",
    "retry_policy",
    "link_prioritization",
    "selection",
    "policy",
    "request_headers",
    "document_classifier",
    "document_review",
}

_ALLOWED_POLICY_MODES = {"ignore", "warn", "enforce"}

_SECTION_NAMES = ("discovery", "extraction", "compilation", "qaqc")
_CONFIG_SUFFIXES = (".yaml", ".yml", ".json")


def _load_targets_from_csv(csv_path: Path) -> list[dict[str, Any]]:
    """Load targets from a CSV file.

    Parameters
    ----------
    csv_path:
        Path to CSV file with headers as target field names.

    Returns
    -------
    list[dict[str, Any]]
        Target rows as dictionaries; empty strings become None.

    Raises
    ------
    RuntimeConfigError
        If the file is missing, empty, or cannot be parsed.
    """
    if not csv_path.exists():
        msg = f"Targets CSV file not found: {csv_path.as_posix()}"
        raise RuntimeConfigError(msg)

    try:
        with csv_path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            targets: list[dict[str, Any]] = [
                {k: v or None for k, v in row.items()}
                for row in reader
            ]
    except RuntimeConfigError:
        raise
    except Exception as e:
        msg = f"Failed to load targets CSV: {csv_path.as_posix()}: {e}"
        raise RuntimeConfigError(msg) from e

    if not fieldnames:
        msg = f"CSV file is empty: {csv_path.as_posix()}"
        raise RuntimeConfigError(msg)
    return targets


def _resolve_targets(
    discovery: dict[str, Any],
    config_dir: Path,
) -> None:
    """Resolve all target sources into ``discovery['targets']`` in-place.

    Handles three forms:
    - ``targets_csv``: authored CSV of target rows (merged with inline).
    - ``targets`` as a list: inline targets (unchanged).
    - ``targets`` as a dict with ``source``: a generated source
      (``dataset`` or ``cross_product``) resolved via a target provider.

    Parameters
    ----------
    discovery:
        Discovery section of the config dict (mutated in-place).
    config_dir:
        Config file directory for resolving relative dataset paths.

    Raises
    ------
    RuntimeConfigError
        If a source is misconfigured or unreadable.
    """
    from psweep.discovery.targets import (
        TargetProviderError,
        resolve_target_provider,
    )

    generated: list[dict[str, Any]] = []

    # 1. targets_csv shorthand → authored rows.
    targets_csv = discovery.get("targets_csv")
    if isinstance(targets_csv, str):
        csv_path = Path(targets_csv)
        if not csv_path.is_absolute():
            csv_path = config_dir / csv_path
        generated.extend(_load_targets_from_csv(csv_path))
    discovery.pop("targets_csv", None)

    # 2. targets: either an inline list (unchanged) or a generated source dict.
    targets = discovery.get("targets")
    if isinstance(targets, dict):
        try:
            provider = resolve_target_provider(targets, config_dir=config_dir)
            discovery["targets"] = generated + provider.provide()
        except TargetProviderError as exc:
            raise RuntimeConfigError(str(exc)) from exc
    elif isinstance(targets, list):
        discovery["targets"] = generated + targets
    elif generated:
        discovery["targets"] = generated


def _validate_discovery_shorthands(discovery: dict[str, Any]) -> None:
    """Validate shorthand keys for simpler domain configs."""
    queries = discovery.get("queries")
    if isinstance(queries, list) and not all(
        isinstance(q, str) for q in queries
    ):
        msg = "'discovery.queries' must be an array of strings"
        raise RuntimeConfigError(msg)

    follow_links = discovery.get("follow_links")
    if follow_links is not None and not isinstance(follow_links, bool):
        msg = "'discovery.follow_links' must be a boolean"
        raise RuntimeConfigError(msg)

    request_delay_ms = discovery.get("request_delay_ms")
    if request_delay_ms is not None and (
        not isinstance(request_delay_ms, int) or request_delay_ms < 0
    ):
        msg = "'discovery.request_delay_ms' must be an integer >= 0"
        raise RuntimeConfigError(msg)


def _validate_discovery_runtime(runtime: dict[str, Any]) -> None:
    max_conc = runtime.get("max_concurrent_downloads")
    if max_conc is not None and (
        not isinstance(max_conc, int) or max_conc < 1
    ):
        msg = (
            "'discovery.runtime.max_concurrent_downloads'"
            " must be an integer >= 1"
        )
        raise RuntimeConfigError(msg)

    min_interval = runtime.get("min_request_interval_ms")
    if min_interval is not None and (
        not isinstance(min_interval, int) or min_interval < 0
    ):
        msg = (
            "'discovery.runtime.min_request_interval_ms'"
            " must be an integer >= 0"
        )
        raise RuntimeConfigError(msg)


def _validate_discovery_request_headers(request_headers: Any) -> None:
    if not isinstance(request_headers, dict):
        msg = "'discovery.request_headers' must be an object"
        raise RuntimeConfigError(msg)
    for key, value in request_headers.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
        ):
            msg = (
                "'discovery.request_headers' must map"
                " non-empty string keys to non-empty string values"
            )
            raise RuntimeConfigError(msg)


def _validate_discovery_policy(policy: dict[str, Any]) -> None:
    for field_name in ("robots_mode", "tos_mode"):
        mode = policy.get(field_name)
        if mode is not None and mode not in _ALLOWED_POLICY_MODES:
            allowed = ", ".join(sorted(_ALLOWED_POLICY_MODES))
            msg = (
                f"'discovery.policy.{field_name}'"
                f" must be one of: {allowed}"
            )
            raise RuntimeConfigError(msg)

    acknowledged = policy.get("acknowledged_tos_domains")
    if acknowledged is not None and (
        not isinstance(acknowledged, list)
        or not all(
            isinstance(item, str) and item.strip() for item in acknowledged
        )
    ):
        msg = (
            "'discovery.policy.acknowledged_tos_domains'"
            " must be an array of non-empty strings"
        )
        raise RuntimeConfigError(msg)


def _validate_discovery_topology(topology: dict[str, Any]) -> None:
    mode = topology.get("mode")
    if mode is not None and mode not in _ALLOWED_TOPOLOGY_MODES:
        allowed = ", ".join(sorted(_ALLOWED_TOPOLOGY_MODES))
        msg = f"'discovery.topology.mode' must be one of: {allowed}"
        raise RuntimeConfigError(msg)


def _validate_discovery_query_families(
    query_families: dict[str, Any],
) -> None:
    for family_name, templates in query_families.items():
        if not isinstance(templates, list) or not all(
            isinstance(item, str) for item in templates
        ):
            msg = (
                f"'discovery.query_families.{family_name}'"
                " must be an array of strings"
            )
            raise RuntimeConfigError(msg)


def _validate_discovery_section_schema(discovery: dict[str, Any]) -> None:
    for field in _ACQUISITION_LIST_FIELDS:
        value = discovery.get(field)
        if value is not None and not isinstance(value, list):
            msg = f"'discovery.{field}' must be an array"
            raise RuntimeConfigError(msg)

    for field in _ACQUISITION_OBJECT_FIELDS:
        value = discovery.get(field)
        if value is not None and not isinstance(value, dict):
            msg = f"'discovery.{field}' must be an object"
            raise RuntimeConfigError(msg)

    _validate_discovery_shorthands(discovery)

    topology = discovery.get("topology")
    if isinstance(topology, dict):
        _validate_discovery_topology(topology)

    query_families = discovery.get("query_families")
    if isinstance(query_families, dict):
        _validate_discovery_query_families(query_families)

    runtime = discovery.get("runtime")
    if isinstance(runtime, dict):
        _validate_discovery_runtime(runtime)

    request_headers = discovery.get("request_headers")
    if request_headers is not None:
        _validate_discovery_request_headers(request_headers)

    policy = discovery.get("policy")
    if isinstance(policy, dict):
        _validate_discovery_policy(policy)


def _validate_models_block(models: Any) -> None:
    """Validate the optional top-level ``models`` alias map.

    Must be a mapping of non-empty alias-name strings to non-empty model-name
    strings (e.g. ``{fast: gpt-4o-mini, accurate: gpt-5}``). Alias names are
    entirely user-chosen — there are no reserved names — so a domain can define
    whatever aliases it needs and reference them from any stage's ``model:``.
    """
    if models is None:
        return
    if not isinstance(models, dict):
        raise RuntimeConfigError(
            "'models' must be a mapping of alias-name -> model-name"
        )
    for key, value in models.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
        ):
            raise RuntimeConfigError(
                "'models' must map non-empty alias names to non-empty model "
                "names"
            )


def _validate_model_context_windows_block(windows: Any) -> None:
    """Validate the optional top-level ``model_context_windows`` map.

    Maps a model/deployment name to its context window in prompt tokens, used to
    fail fast before an over-budget extraction request. No model names are
    hardcoded anywhere; this is the only place a window is declared. Must be a
    mapping of non-empty strings to positive integers.
    """
    if windows is None:
        return
    if not isinstance(windows, dict):
        raise RuntimeConfigError(
            "'model_context_windows' must be a mapping of model-name -> "
            "max prompt tokens (positive integer)"
        )
    for key, value in windows.items():
        if not isinstance(key, str) or not key.strip():
            raise RuntimeConfigError(
                "'model_context_windows' keys must be non-empty model names"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeConfigError(
                "'model_context_windows' values must be positive integers "
                f"(got {value!r} for '{key}')"
            )


def _normalize_pages_block(extraction: dict[str, Any]) -> None:
    """Normalize the unified ``pages`` block into flat internal keys.

    Config format::

        pages:
          csv: path/to/ranges.csv        # manual ranges (wins)
          auto_locate:                    # LLM fallback for uncovered docs
            section_description: "..."
            trigger_chars: 200000

    Expands to internal keys ``pages_csv`` (str) and ``page_targeting``
    (dict with ``enabled: True``) consumed by the extraction pipeline.
    """
    pages = extraction.get("pages")

    if isinstance(pages, dict):
        extraction.pop("pages")
        if "csv" in pages:
            extraction["pages_csv"] = pages["csv"]
        if "auto_locate" in pages:
            al = dict(pages["auto_locate"])
            al.setdefault("enabled", True)  # presence = enabled
            extraction["page_targeting"] = al
        if "range" in pages:
            # Single-file range from config (usually a CLI thing).
            extraction["pages"] = pages["range"]
    elif "pages_csv" in extraction or "page_targeting" in extraction:
        raise RuntimeConfigError(
            "'pages_csv' and 'page_targeting' are no longer supported as "
            "top-level extraction keys. Use the unified 'pages:' block "
            "instead:\n"
            "  pages:\n"
            "    csv: path/to/ranges.csv\n"
            "    auto_locate:\n"
            "      section_description: \"...\"\n"
            "See config/TEMPLATE.yaml for the full reference."
        )


def _validate_extraction_section_schema(extraction: dict[str, Any]) -> None:
    """Deep-validate the ``extraction`` section at load time.

    Mirrors ``_validate_discovery_section_schema`` so config-supplied values
    are checked with the same rigor as CLI flags (Click type/choice guards are
    bypassed when a value comes from YAML). Domain-neutral: only structural
    types and ranges are enforced.
    """

    def _require_positive_int(name: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeConfigError(
                f"extraction.{name} must be a positive integer (got {value!r})"
            )

    for name in ("max_context", "limit"):
        if name in extraction and extraction[name] is not None:
            _require_positive_int(name, extraction[name])

    if extraction.get("skip_existing") is not None and not isinstance(
        extraction["skip_existing"], bool
    ):
        raise RuntimeConfigError("extraction.skip_existing must be a boolean")

    if extraction.get("live_dashboard") is not None and not isinstance(
        extraction["live_dashboard"], bool
    ):
        raise RuntimeConfigError("extraction.live_dashboard must be a boolean")

    provider = extraction.get("provider")
    if provider is not None:
        allowed = {"openai", "azure", "anthropic", "gemini", "auto"}
        if not isinstance(provider, str) or provider.lower() not in allowed:
            raise RuntimeConfigError(
                "extraction.provider must be one of: "
                + ", ".join(sorted(allowed))
            )

    for name in ("input_dir", "schema", "output_dir", "pages", "pages_csv",
                 "model", "profile", "qaqc_lane"):
        if name in extraction and extraction[name] is not None:
            if not isinstance(extraction[name], str):
                raise RuntimeConfigError(
                    f"extraction.{name} must be a string"
                )

    page_targeting = extraction.get("page_targeting")
    if page_targeting is not None:
        _validate_page_targeting_block(page_targeting)


def _validate_page_targeting_block(pt: Any) -> None:
    """Validate the ``pages.auto_locate`` block (internal key: page_targeting)."""
    if not isinstance(pt, dict):
        raise RuntimeConfigError(
            "pages.auto_locate must be an object"
        )
    if pt.get("section_description") is not None and not isinstance(
        pt["section_description"], str
    ):
        raise RuntimeConfigError(
            "pages.auto_locate.section_description must be a string"
        )
    for name in ("trigger_chars", "max_selected_pages"):
        val = pt.get(name)
        if val is not None:
            if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
                raise RuntimeConfigError(
                    f"pages.auto_locate.{name} must be a positive "
                    f"integer (got {val!r})"
                )
    if pt.get("model") is not None and not isinstance(pt["model"], str):
        raise RuntimeConfigError(
            "pages.auto_locate.model must be a string"
        )
    if pt.get("keywords") is not None and not isinstance(
        pt["keywords"], list
    ):
        raise RuntimeConfigError(
            "pages.auto_locate.keywords must be a list"
        )


_SYNTHESIS_STRING_FIELDS = (
    "citation_field",
    "relevance_flag",
    "item_array",
    "instructions",
    "model",
    "ordering_comparison",
)
# How ordered values are compared in the advisory chronology/ordering check.
# Domain-neutral: pick the comparator that fits the field type. "date" enables
# the precision-aware temporal comparator; "numeric" compares as numbers;
# "lexical" (default) compares as strings.
_ALLOWED_ORDERING_COMPARISONS = ("lexical", "numeric", "date")
_SYNTHESIS_STRING_LIST_FIELDS = (
    "group_by",
    "identity_fields",
    "narrative_fields",
    "ordering_constraint",
)
_ALLOWED_SYNTHESIS_KEYS = (
    {"enabled", "min_sources_for_llm", "reconcile_fields"}
    | set(_SYNTHESIS_STRING_FIELDS)
    | set(_SYNTHESIS_STRING_LIST_FIELDS)
)


def _validate_compilation_section_schema(
    compilation: dict[str, Any],
) -> None:
    """Deep-validate the ``compilation`` section at load time.

    Mirrors ``_validate_extraction_section_schema``. Domain-neutral: only
    structural types/shapes are enforced. The rich ``synthesis`` block was
    previously an unchecked passthrough, so a mistyped key (e.g. ``group_bye``)
    silently produced empty output; it is now validated.
    """
    synthesis = compilation.get("synthesis")
    if synthesis is not None:
        _validate_synthesis_block(synthesis)


def _validate_synthesis_block(syn: Any) -> None:
    """Validate the optional ``compilation.synthesis`` block structure."""
    if not isinstance(syn, dict):
        raise RuntimeConfigError("compilation.synthesis must be an object")

    unknown = sorted(k for k in syn if k not in _ALLOWED_SYNTHESIS_KEYS)
    if unknown:
        raise RuntimeConfigError(
            "Unknown keys in 'compilation.synthesis': "
            + ", ".join(unknown)
        )

    if syn.get("enabled") is not None and not isinstance(
        syn["enabled"], bool
    ):
        raise RuntimeConfigError(
            "compilation.synthesis.enabled must be a boolean"
        )

    msl = syn.get("min_sources_for_llm")
    if msl is not None and (
        isinstance(msl, bool) or not isinstance(msl, int) or msl < 0
    ):
        raise RuntimeConfigError(
            "compilation.synthesis.min_sources_for_llm must be a "
            f"non-negative integer (got {msl!r})"
        )

    for name in _SYNTHESIS_STRING_FIELDS:
        if syn.get(name) is not None and not isinstance(syn[name], str):
            raise RuntimeConfigError(
                f"compilation.synthesis.{name} must be a string"
            )

    for name in _SYNTHESIS_STRING_LIST_FIELDS:
        value = syn.get(name)
        if value is None:
            continue
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise RuntimeConfigError(
                f"compilation.synthesis.{name} must be a list of "
                "non-empty strings"
            )

    comparison = syn.get("ordering_comparison")
    if comparison is not None and comparison not in _ALLOWED_ORDERING_COMPARISONS:
        raise RuntimeConfigError(
            "compilation.synthesis.ordering_comparison must be one of "
            + ", ".join(_ALLOWED_ORDERING_COMPARISONS)
            + f" (got {comparison!r})"
        )

    field_names = _validate_reconcile_fields(syn.get("reconcile_fields"))

    # When enabled, group_by is what turns records into entity rows.
    if syn.get("enabled") and not syn.get("group_by"):
        raise RuntimeConfigError(
            "compilation.synthesis.group_by is required when synthesis is "
            "enabled"
        )

    # Ordering constraint must reference fields that are actually reconciled.
    ordering = syn.get("ordering_constraint") or []
    if ordering and field_names:
        unknown_ordered = [f for f in ordering if f not in field_names]
        if unknown_ordered:
            raise RuntimeConfigError(
                "compilation.synthesis.ordering_constraint references "
                "field(s) not in reconcile_fields: "
                + ", ".join(unknown_ordered)
            )


def _validate_reconcile_fields(reconcile_fields: Any) -> set[str]:
    """Validate ``reconcile_fields`` and return the set of declared field names."""
    if reconcile_fields is None:
        return set()
    if not isinstance(reconcile_fields, list):
        raise RuntimeConfigError(
            "compilation.synthesis.reconcile_fields must be a list"
        )
    field_names: set[str] = set()
    for entry in reconcile_fields:
        if not isinstance(entry, dict):
            raise RuntimeConfigError(
                "each compilation.synthesis.reconcile_fields entry must be "
                "an object with a 'field' key"
            )
        field = entry.get("field")
        if not isinstance(field, str) or not field:
            raise RuntimeConfigError(
                "each compilation.synthesis.reconcile_fields entry requires "
                "a non-empty string 'field'"
            )
        evidence = entry.get("evidence")
        if evidence is not None and not isinstance(evidence, str):
            raise RuntimeConfigError(
                f"reconcile_fields['{field}'].evidence must be a string"
            )
        precision = entry.get("precision")
        if precision is not None and not isinstance(precision, str):
            raise RuntimeConfigError(
                f"reconcile_fields['{field}'].precision must be a string"
            )
        field_names.add(field)
    return field_names


def _read_config_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    raw_text = path.read_text(encoding="utf-8")

    if suffix in {".yaml", ".yml"}:
        loaded = yaml.safe_load(raw_text)
    elif suffix == ".json":
        loaded = json.loads(raw_text)
    else:
        msg = (
            f"Unsupported config extension '{suffix}'. "
            "Use .yaml, .yml, or .json"
        )
        raise RuntimeConfigError(msg)

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        msg = f"Config root must be an object: {path.as_posix()}"
        raise RuntimeConfigError(msg)

    return loaded


def _is_run_config_path(config_path: Path) -> bool:
    return config_path.stem.lower() == "run"


def _find_single_section_override_file(
    *,
    config_dir: Path,
    section_name: str,
) -> Path | None:
    candidates = [
        config_dir / f"{section_name}{suffix}" for suffix in _CONFIG_SUFFIXES
    ]
    existing = [path for path in candidates if path.exists()]
    if len(existing) > 1:
        paths = ", ".join(path.name for path in existing)
        msg = (
            f"Multiple section override files found for '{section_name}': "
            f"{paths}"
        )
        raise RuntimeConfigError(msg)
    return existing[0] if existing else None


def _extract_section_override_data(
    *,
    section_name: str,
    raw_override: dict[str, Any],
    override_path: Path,
) -> dict[str, Any]:
    top_level_matches = [
        key for key in raw_override if key in _ALLOWED_TOP_LEVEL
    ]
    if top_level_matches:
        unknown_top = [
            key for key in raw_override if key not in _ALLOWED_TOP_LEVEL
        ]
        if unknown_top:
            msg = (
                "Unknown top-level config keys in section override file "
                f"{override_path.as_posix()}: "
                + ", ".join(sorted(unknown_top))
            )
            raise RuntimeConfigError(msg)
        if section_name not in raw_override:
            msg = (
                f"Section override file must contain"
                f" '{section_name}' section: "
                f"{override_path.as_posix()}"
            )
            raise RuntimeConfigError(msg)
        section_data = raw_override.get(section_name)
    else:
        section_data = raw_override

    if not isinstance(section_data, dict):
        msg = (
            f"'{section_name}' override section must be an object in "
            f"{override_path.as_posix()}"
        )
        raise RuntimeConfigError(msg)

    return section_data


def _apply_split_file_section_overrides(
    *,
    config_path: Path,
    config_data: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(config_data)
    config_dir = config_path.parent

    for section_name in _SECTION_NAMES:
        override_path = _find_single_section_override_file(
            config_dir=config_dir,
            section_name=section_name,
        )
        if override_path is None:
            continue

        raw_override = _read_config_file(override_path)
        section_override = _extract_section_override_data(
            section_name=section_name,
            raw_override=raw_override,
            override_path=override_path,
        )

        existing_section = merged.get(section_name) or {}
        if existing_section and not isinstance(existing_section, dict):
            msg = (
                f"'{section_name}' section must be an object in runtime config"
            )
            raise RuntimeConfigError(msg)

        merged[section_name] = {**existing_section, **section_override}

    return merged


def load_runtime_config_file(config_path: Path) -> dict[str, Any]:
    """Load a run-level or section-level runtime config file."""
    if not config_path.exists():
        msg = f"Config file not found: {config_path.as_posix()}"
        raise RuntimeConfigError(msg)

    config_data = _read_config_file(config_path)
    if _is_run_config_path(config_path):
        config_data = _apply_split_file_section_overrides(
            config_path=config_path,
            config_data=config_data,
        )

    unknown_top = [key for key in config_data if key not in _ALLOWED_TOP_LEVEL]
    if unknown_top:
        raise RuntimeConfigError(
            "Unknown top-level config keys: " + ", ".join(sorted(unknown_top))
        )

    _validate_models_block(config_data.get("models"))
    _validate_model_context_windows_block(
        config_data.get("model_context_windows")
    )

    discovery = config_data.get("discovery")
    if discovery is not None:
        if not isinstance(discovery, dict):
            msg = "'discovery' section must be an object in runtime config"
            raise RuntimeConfigError(msg)
        # targets_csv must be resolved before schema validation
        _resolve_targets(discovery, config_path.parent)
        _validate_discovery_section_schema(discovery)

    extraction = config_data.get("extraction")
    if extraction is not None:
        if not isinstance(extraction, dict):
            msg = "'extraction' section must be an object in runtime config"
            raise RuntimeConfigError(msg)
        _normalize_pages_block(extraction)
        _validate_extraction_section_schema(extraction)

    compilation = config_data.get("compilation")
    if compilation is not None:
        if not isinstance(compilation, dict):
            msg = "'compilation' section must be an object in runtime config"
            raise RuntimeConfigError(msg)
        _validate_compilation_section_schema(compilation)

    return config_data


def catalog_for_command(command: str) -> list[dict[str, str]]:
    """Return catalog entries for a command plus global variables."""
    section = _SECTION_ALIASES.get(command)
    if section is None:
        msg = f"Unsupported command for catalog: {command}"
        raise RuntimeConfigError(msg)
    return VARIABLE_CATALOG["global"] + VARIABLE_CATALOG[section]


def _validate_section_keys(
    section_name: str,
    section_data: dict[str, Any],
    *,
    strict: bool,
) -> list[str]:
    warnings: list[str] = []
    allowed = _ALLOWED_SECTION_FIELDS.get(section_name, set())
    unknown = [key for key in section_data if key not in allowed]
    if unknown:
        unknown_keys = ", ".join(sorted(unknown))
        msg = f"Unknown keys in '{section_name}' section: {unknown_keys}"
        if strict:
            raise RuntimeConfigError(msg)
        warnings.append(msg)
    return warnings


def _required_fields_for_section(section_name: str) -> list[str]:
    if section_name == "discovery":
        return []
    if section_name == "extraction":
        return ["schema", "path"]
    if section_name == "compilation":
        return ["schema", "extracted_dir"]
    return ["schema"]


# ── Discovery merge helpers ──────────────────────────────────────
# One helper per discovery subsection.
# Shorthands run first; verbose keys win when both are present.

_ACQ_SIMPLE_KEYS: tuple[tuple[str, str], ...] = (
    ("hub_pages", "hub_pages"),
    ("targets", "targets"),
    ("query_templates", "query_templates"),
    ("query_families", "query_families"),
    ("allowed_domains", "allowed_domains"),
    ("request_headers", "request_headers"),
)

_ACQ_RUNTIME_KEYS: tuple[str, ...] = (
    "max_depth",
    "max_pages",
    "max_files",
    "timeout_seconds",
    "max_concurrent_downloads",
    "min_request_interval_ms",
)

# YAML selection keys → merged dict keys.
# Shorthands: max_per_target, require_any, exclude.
_ACQ_SELECTION_KEY_MAP: dict[str, str] = {
    "primary_per_target": "selection_primary_per_target",
    "max_per_target": "selection_primary_per_target",
    "exclude_draft": "selection_exclude_draft",
    "draft_patterns": "selection_draft_patterns",
    "relevance_require_any_terms": "selection_relevance_require_any_terms",
    "require_any": "selection_relevance_require_any_terms",
    "relevance_require_legal_marker_terms": (
        "selection_relevance_require_legal_marker_terms"
    ),
    "relevance_exclude_any_terms": "selection_relevance_exclude_any_terms",
    "exclude": "selection_relevance_exclude_any_terms",
    "relevance_allowed_domain_patterns": (
        "selection_relevance_allowed_domain_patterns"
    ),
    "require_supported_document": "selection_require_supported_document",
    "target_identity_require_any_templates": (
        "selection_target_identity_require_any_templates"
    ),
    "target_identity_require_all_templates": (
        "selection_target_identity_require_all_templates"
    ),
    "target_identity_exclude_any_templates": (
        "selection_target_identity_exclude_any_templates"
    ),
}


def _set(
    merged: dict[str, Any],
    sources: dict[str, str],
    key: str,
    value: Any,
    source: str,
) -> None:
    merged[key] = value
    sources[key] = source


def _merge_acq_shorthands(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    queries = section.get("queries")
    if isinstance(queries, list) and queries:
        _set(merged, sources, "query_families", {"_default": queries},
             "config.discovery.queries")
        _set(merged, sources, "use_query_family", "_default",
             "config.discovery.queries")

    follow_links = section.get("follow_links")
    if follow_links is True:
        _set(merged, sources, "topology_mode", "distributed",
             "config.discovery.follow_links")
    elif follow_links is False and "topology_mode" not in merged:
        _set(merged, sources, "topology_mode", None,
             "config.discovery.follow_links")

    delay = section.get("request_delay_ms")
    if delay is not None:
        _set(merged, sources, "min_request_interval_ms", delay,
             "config.discovery.request_delay_ms")


def _merge_acq_topology(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    topo = section.get("topology")
    if isinstance(topo, dict) and topo.get("mode") is not None:
        _set(merged, sources, "topology_mode", topo["mode"],
             "config.discovery.topology.mode")


def _merge_acq_discovery_rules(
    rules: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
    prefix: str,
) -> None:
    for key in ("include_url_patterns", "include_link_text_patterns"):
        if key in rules:
            _set(merged, sources, key, rules[key],
                 f"config.discovery.{prefix}.{key}")


def _merge_acq_runtime(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    runtime = section.get("runtime")
    if not isinstance(runtime, dict):
        return
    for key in _ACQ_RUNTIME_KEYS:
        if key in runtime:
            _set(merged, sources, key, runtime[key],
                 f"config.discovery.runtime.{key}")


def _merge_acq_retry(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    rp = section.get("retry_policy")
    if not isinstance(rp, dict):
        return
    for src, dst in (
        ("max_attempts", "retry_max_attempts"),
        ("initial_backoff_seconds", "retry_initial_backoff_seconds"),
        ("max_backoff_seconds", "retry_max_backoff_seconds"),
    ):
        if src in rp:
            _set(merged, sources, dst, rp[src],
                 f"config.discovery.retry_policy.{src}")


def _merge_acq_policy(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    pol = section.get("policy")
    if not isinstance(pol, dict):
        return
    for src, dst in (
        ("robots_mode", "robots_policy_mode"),
        ("tos_mode", "tos_policy_mode"),
    ):
        if src in pol:
            _set(merged, sources, dst, pol[src],
                 f"config.discovery.policy.{src}")
    if "acknowledged_tos_domains" in pol:
        _set(merged, sources, "acknowledged_tos_domains",
             pol["acknowledged_tos_domains"],
             "config.discovery.policy.acknowledged_tos_domains")


def _merge_acq_output(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    out = section.get("output")
    if not isinstance(out, dict):
        return
    if "documents_dir" in out:
        _set(merged, sources, "output_documents", out["documents_dir"],
             "config.discovery.output.documents_dir")
    if "manifest_path" in out:
        _set(merged, sources, "output_manifest", out["manifest_path"],
             "config.discovery.output.manifest_path")


def _merge_acq_search(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    cfg = section.get("search")
    if not isinstance(cfg, dict):
        return
    provider = str(cfg.get("provider") or "").lower()
    if provider == "serpapi" and cfg.get("enabled") is not False:
        _set(merged, sources, "enable_serpapi", True,
             "config.discovery.search.provider")
    for src, dst in (
        ("query_templates", "query_templates"),
        ("max_results_per_query", "seeker_max_results"),
        ("max_results", "seeker_max_results"),
        ("cache", "seeker_cache"),
        ("cache_ttl_minutes", "seeker_cache_ttl_minutes"),
    ):
        if src in cfg:
            _set(merged, sources, dst, cfg[src],
                 f"config.discovery.search.{src}")

    # Verbatim SerpApi params + the google_news convenience flag → merged into a
    # single seeker_extra_params dict forwarded to the seeker.
    seeker_params: dict[str, Any] = {}
    raw_params = cfg.get("serpapi_params")
    if isinstance(raw_params, dict):
        seeker_params.update(raw_params)
    if cfg.get("google_news") is True:
        seeker_params.setdefault("tbm", "nws")
    if seeker_params:
        _set(merged, sources, "seeker_extra_params", seeker_params,
             "config.discovery.search.serpapi_params")


def _merge_acq_seeker(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    cfg = section.get("seeker")
    if not isinstance(cfg, dict):
        return
    provider = str(cfg.get("provider") or "").lower()
    if provider == "serpapi" and cfg.get("enabled") is not False:
        _set(merged, sources, "enable_serpapi", True,
             "config.discovery.seeker.provider")
    for src, dst in (
        ("query_templates", "query_templates"),
        ("use_query_family", "use_query_family"),
        ("max_results", "seeker_max_results"),
    ):
        if src in cfg:
            _set(merged, sources, dst, cfg[src],
                 f"config.discovery.seeker.{src}")


def _merge_acq_digger(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    cfg = section.get("digger")
    if not isinstance(cfg, dict):
        return
    for src, dst in (
        ("provider", "digger_provider"),
        ("connector", "digger_provider"),
        ("allowed_domains", "allowed_domains"),
    ):
        if src in cfg:
            _set(merged, sources, dst, cfg[src],
                 f"config.discovery.digger.{src}")
    if isinstance(cfg.get("index_page_mode"), dict):
        _set(merged, sources, "index_page_mode", cfg["index_page_mode"],
             "config.discovery.digger.index_page_mode")
    digger_rules = cfg.get("discovery_rules")
    if isinstance(digger_rules, dict):
        _merge_acq_discovery_rules(
            digger_rules, merged, sources, "digger.discovery_rules"
        )


def _merge_acq_link_prioritization(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    lp = section.get("link_prioritization")
    if not isinstance(lp, dict):
        return
    for src, dst in (
        ("mode", "link_prioritization_mode"),
        ("top_k", "link_top_k"),
        ("keywords", "link_prioritization_keywords"),
        ("domain_scores", "link_prioritization_domain_scores"),
    ):
        if src in lp:
            _set(merged, sources, dst, lp[src],
                 f"config.discovery.link_prioritization.{src}")


def _merge_acq_selection(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    sel = section.get("selection")
    if not isinstance(sel, dict):
        return
    for src_key, dst_key in _ACQ_SELECTION_KEY_MAP.items():
        if src_key in sel:
            _set(merged, sources, dst_key, sel[src_key],
                 f"config.discovery.selection.{src_key}")


def _merge_acq_routing(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    routing = section.get("routing")
    if not isinstance(routing, dict):
        return
    if "index_links" in routing:
        _set(merged, sources, "index_links", routing["index_links"],
             "config.discovery.routing.index_links")


def _merge_discovery_fields(
    section: dict[str, Any],
    merged: dict[str, Any],
    sources: dict[str, str],
) -> None:
    # Shorthands first; verbose subsections override when both present.
    _merge_acq_shorthands(section, merged, sources)
    _merge_acq_topology(section, merged, sources)
    for sec_key, merged_key in _ACQ_SIMPLE_KEYS:
        if sec_key in section:
            _set(merged, sources, merged_key, section[sec_key],
                 f"config.discovery.{sec_key}")
    discovery_rules = section.get("discovery_rules")
    if isinstance(discovery_rules, dict):
        _merge_acq_discovery_rules(
            discovery_rules, merged, sources, "discovery_rules"
        )
    _merge_acq_runtime(section, merged, sources)
    _merge_acq_retry(section, merged, sources)
    _merge_acq_policy(section, merged, sources)
    _merge_acq_output(section, merged, sources)
    _merge_acq_search(section, merged, sources)
    _merge_acq_seeker(section, merged, sources)
    _merge_acq_digger(section, merged, sources)
    _merge_acq_routing(section, merged, sources)
    _merge_acq_link_prioritization(section, merged, sources)
    _merge_acq_selection(section, merged, sources)
    classifier = section.get("document_classifier")
    if isinstance(classifier, dict):
        _set(merged, sources, "document_classifier", classifier,
             "config.discovery.document_classifier")
    review = section.get("document_review")
    if isinstance(review, dict):
        _set(merged, sources, "document_review", review,
             "config.discovery.document_review")
    aliases = section.get("query_context_aliases")
    if isinstance(aliases, dict):
        _set(merged, sources, "query_context_aliases", aliases,
             "config.discovery.query_context_aliases")
    partition_by = section.get("partition_by")
    if isinstance(partition_by, list):
        _set(merged, sources, "partition_by", partition_by,
             "config.discovery.partition_by")
    browser_flag = section.get("browser_mode")
    if browser_flag is None:
        browser_flag = section.get("browser")
    if browser_flag is not None:
        _set(merged, sources, "browser_mode", bool(browser_flag),
             "config.discovery.browser_mode")


# ── Top-level field map (all commands) ──────────────────────────

_FIELD_MAP: dict[str, str] = {
    "domain": "domain",
    "seed_urls": "seeds",
    "query": "query",
    "state": "state",
    "jurisdiction": "jurisdiction",
    "partition_mode": "partition_mode",
    "enable_serpapi": "enable_serpapi",
    "output_documents": "output_documents",
    "output_manifest": "output_manifest",
    "dry_run": "dry_run",
    "path": "input_dir",
    "extracted_dir": "input_dir",
    "output": "output_dir",
    "schema": "schema",
    "pages_csv": "pages_csv",
    "pages": "pages",
    "page_targeting": "page_targeting",
    "profile_name": "profile",
    "provider": "provider",
    "model": "model",
    "limit": "limit",
    "skip_existing": "skip_existing",
    "max_context": "max_context",
    "enable_qa_qc": "enable_qaqc",
    "qaqc_lane": "qaqc_lane",
    "live_dashboard": "live_dashboard",
    "report_format": "report_format",
    "fail_on_suspicious": "fail_on_suspicious",
    "synthesis": "synthesis",
    "deduplication": "deduplication",
    "normalization": "normalization",
    "compilation_output": "output",
}


def _merge_config_fields(
    *,
    section_name: str,
    section: dict[str, Any],
    cli_values: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}

    for cli_key, section_key in _FIELD_MAP.items():
        if section_key in section:
            merged[cli_key] = section.get(section_key)
            sources[cli_key] = f"config.{section_name}.{section_key}"

    if section_name == "discovery":
        _merge_discovery_fields(section, merged, sources)

    for cli_key, value in cli_values.items():
        if value is not None:
            merged[cli_key] = value
            sources[cli_key] = "cli"

    return merged, sources


def resolve_command_config(
    *,
    command: str,
    cli_values: dict[str, Any],
    config_data: dict[str, Any] | None,
    strict: bool = False,
) -> dict[str, Any]:
    """Resolve command inputs from runtime config and CLI overrides."""
    section_name = _SECTION_ALIASES.get(command)
    if section_name is None:
        msg = f"Unsupported command config resolution target: {command}"
        raise RuntimeConfigError(msg)

    cfg = config_data or {}
    section = cfg.get(section_name) or {}
    if section and not isinstance(section, dict):
        msg = f"'{section_name}' section must be an object in runtime config"
        raise RuntimeConfigError(msg)

    warnings = _validate_section_keys(section_name, section, strict=strict)
    merged, sources = _merge_config_fields(
        section_name=section_name,
        section=section,
        cli_values=cli_values,
    )

    required_fields = _required_fields_for_section(section_name)

    missing = [name for name in required_fields if not merged.get(name)]
    if missing:
        joined = ", ".join(missing)
        msg = "Missing required runtime configuration field(s): " + joined
        raise RuntimeConfigError(msg)

    if "domain" in cfg:
        merged["domain"] = cfg.get("domain")
        sources["domain"] = "config.domain"

    # Top-level model alias map is available to every command (process,
    # discover, compile) so any LLM stage can resolve an alias reference.
    if "models" in cfg:
        merged["models"] = cfg.get("models")
        sources["models"] = "config.models"

    # Top-level per-model context-window map feeds the extraction fail-fast
    # guard; passed through untouched (no model names hardcoded in code).
    if "model_context_windows" in cfg:
        merged["model_context_windows"] = cfg.get("model_context_windows")
        sources["model_context_windows"] = "config.model_context_windows"

    merged["_config_warnings"] = warnings
    merged["_config_sources"] = sources
    return merged
