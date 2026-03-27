"""Shared runtime config loader and resolver for CLI commands."""

from __future__ import annotations

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
    "processing": [
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
            "description": "CSV mapping file names to page ranges.",
        },
        {
            "name": "pages",
            "level": "optional",
            "description": "Single-file page range (for example: 10-35).",
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
    "consolidation": [
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
            "description": "Override destination for consolidated output.",
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
    "acquisition": [
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
            "description": "Maximum number of parallel file downloads in acquisition runs.",
        },
        {
            "name": "runtime.min_request_interval_ms",
            "level": "advanced",
            "description": "Minimum delay between outbound acquisition requests in milliseconds.",
        },
        {
            "name": "policy.robots_mode",
            "level": "advanced",
            "description": "Robots policy mode for target-site requests: ignore, warn, or enforce.",
        },
        {
            "name": "policy.tos_mode",
            "level": "advanced",
            "description": "Terms acknowledgement mode for target-site requests: ignore, warn, or enforce.",
        },
    ],
}

_SECTION_ALIASES = {
    "acquire": "acquisition",
    "acquisition": "acquisition",
    "process": "processing",
    "processing": "processing",
    "consolidate": "consolidation",
    "consolidation": "consolidation",
}

_ALLOWED_TOP_LEVEL = {"domain", "acquisition", "processing", "consolidation"}
_ALLOWED_SECTION_FIELDS = {
    "acquisition": {
        "domain",
        "seeds",
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
    },
    "processing": {
        "input_dir",
        "schema",
        "output_dir",
        "pages_csv",
        "pages",
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
    "consolidation": {
        "input_dir",
        "schema",
        "output_dir",
        "dry_run",
        "report_format",
        "fail_on_suspicious",
    },
}

_ALLOWED_TOPOLOGY_MODES = {"distributed", "centralized", "hybrid"}
_ACQUISITION_LIST_FIELDS = {
    "seeds",
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
}

_ALLOWED_POLICY_MODES = {"ignore", "warn", "enforce"}

_SECTION_NAMES = ("acquisition", "processing", "consolidation")
_CONFIG_SUFFIXES = (".yaml", ".yml", ".json")


def _load_targets_from_csv(csv_path: Path) -> list[dict[str, Any]]:
    """
    Load targets from a CSV file.
    
    Args:
        csv_path: Path to CSV file with headers as target field names
        
    Returns:
        List of target dictionaries
        
    Raises:
        RuntimeConfigError: If CSV cannot be read or parsed
    """
    if not csv_path.exists():
        msg = f"Targets CSV file not found: {csv_path.as_posix()}"
        raise RuntimeConfigError(msg)
    
    try:
        import csv
        
        targets: list[dict[str, Any]] = []
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise RuntimeConfigError(f"CSV file is empty: {csv_path.as_posix()}")
            
            for row_num, row in enumerate(reader, start=2):  # start=2 because row 1 is headers
                # Convert empty strings to None for optional fields
                target = {
                    key: (None if value == "" else value)
                    for key, value in row.items()
                }
                targets.append(target)
        
        return targets
    except RuntimeConfigError:
        raise
    except Exception as e:
        msg = f"Failed to load targets CSV: {csv_path.as_posix()}: {e}"
        raise RuntimeConfigError(msg) from e


def _process_targets_with_csv_support(
    acquisition: dict[str, Any],
    config_dir: Path,
) -> None:
    """
    Process targets field to support CSV file references.
    
    If acquisition.targets_csv is specified, load targets from that CSV file
    and merge with any inline targets in acquisition.targets.
    
    Modifies acquisition dict in-place.
    
    Args:
        acquisition: Acquisition section of config dict
        config_dir: Directory containing the config file (for resolving relative paths)
        
    Raises:
        RuntimeConfigError: If CSV loading fails
    """
    targets_csv = acquisition.get("targets_csv")
    if targets_csv is None:
        return
    
    # Resolve CSV path (relative to config directory)
    if isinstance(targets_csv, str):
        csv_path = Path(targets_csv)
        if not csv_path.is_absolute():
            csv_path = config_dir / csv_path
        
        csv_targets = _load_targets_from_csv(csv_path)
        
        # Merge CSV targets with inline targets
        existing_targets = acquisition.get("targets") or []
        if not isinstance(existing_targets, list):
            existing_targets = []
        
        # Combine: CSV targets first, then inline targets (inline takes precedence if duplicated)
        acquisition["targets"] = csv_targets + existing_targets
    
    # Remove the targets_csv field after processing
    acquisition.pop("targets_csv", None)


def _validate_acquisition_section_schema(acquisition: dict[str, Any]) -> None:
    for field in _ACQUISITION_LIST_FIELDS:
        value = acquisition.get(field)
        if value is not None and not isinstance(value, list):
            msg = f"'acquisition.{field}' must be an array"
            raise RuntimeConfigError(msg)

    for field in _ACQUISITION_OBJECT_FIELDS:
        value = acquisition.get(field)
        if value is not None and not isinstance(value, dict):
            msg = f"'acquisition.{field}' must be an object"
            raise RuntimeConfigError(msg)

    topology = acquisition.get("topology")
    if isinstance(topology, dict):
        mode = topology.get("mode")
        if mode is not None and mode not in _ALLOWED_TOPOLOGY_MODES:
            allowed = ", ".join(sorted(_ALLOWED_TOPOLOGY_MODES))
            msg = (
                "'acquisition.topology.mode' must be one of: "
                f"{allowed}"
            )
            raise RuntimeConfigError(msg)

    query_families = acquisition.get("query_families")
    if isinstance(query_families, dict):
        for family_name, templates in query_families.items():
            if not isinstance(templates, list) or not all(
                isinstance(item, str) for item in templates
            ):
                msg = (
                    "'acquisition.query_families"
                    f".{family_name}' must be an array of strings"
                )
                raise RuntimeConfigError(msg)

    runtime = acquisition.get("runtime")
    if isinstance(runtime, dict):
        max_concurrent_downloads = runtime.get("max_concurrent_downloads")
        if max_concurrent_downloads is not None:
            if not isinstance(max_concurrent_downloads, int) or max_concurrent_downloads < 1:
                raise RuntimeConfigError(
                    "'acquisition.runtime.max_concurrent_downloads' must be an integer >= 1"
                )

        min_request_interval_ms = runtime.get("min_request_interval_ms")
        if min_request_interval_ms is not None:
            if not isinstance(min_request_interval_ms, int) or min_request_interval_ms < 0:
                raise RuntimeConfigError(
                    "'acquisition.runtime.min_request_interval_ms' must be an integer >= 0"
                )

    request_headers = acquisition.get("request_headers")
    if request_headers is not None:
        if not isinstance(request_headers, dict):
            raise RuntimeConfigError("'acquisition.request_headers' must be an object")
        for key, value in request_headers.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip():
                raise RuntimeConfigError(
                    "'acquisition.request_headers' must map non-empty string keys to non-empty string values"
                )

    policy = acquisition.get("policy")
    if isinstance(policy, dict):
        for field_name in ("robots_mode", "tos_mode"):
            mode = policy.get(field_name)
            if mode is not None and mode not in _ALLOWED_POLICY_MODES:
                allowed = ", ".join(sorted(_ALLOWED_POLICY_MODES))
                raise RuntimeConfigError(
                    f"'acquisition.policy.{field_name}' must be one of: {allowed}"
                )

        acknowledged_domains = policy.get("acknowledged_tos_domains")
        if acknowledged_domains is not None:
            if not isinstance(acknowledged_domains, list) or not all(
                isinstance(item, str) and item.strip() for item in acknowledged_domains
            ):
                raise RuntimeConfigError(
                    "'acquisition.policy.acknowledged_tos_domains' must be an array of non-empty strings"
                )


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
        config_dir / f"{section_name}{suffix}"
        for suffix in _CONFIG_SUFFIXES
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
    top_level_matches = [key for key in raw_override if key in _ALLOWED_TOP_LEVEL]
    if top_level_matches:
        unknown_top = [key for key in raw_override if key not in _ALLOWED_TOP_LEVEL]
        if unknown_top:
            msg = (
                "Unknown top-level config keys in section override file "
                f"{override_path.as_posix()}: "
                + ", ".join(sorted(unknown_top))
            )
            raise RuntimeConfigError(msg)
        if section_name not in raw_override:
            msg = (
                f"Section override file must contain '{section_name}' section: "
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
            msg = f"'{section_name}' section must be an object in runtime config"
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

    acquisition = config_data.get("acquisition")
    if acquisition is not None:
        if not isinstance(acquisition, dict):
            msg = "'acquisition' section must be an object in runtime config"
            raise RuntimeConfigError(msg)
        # Process targets_csv if specified (must happen before validation)
        _process_targets_with_csv_support(acquisition, config_path.parent)
        _validate_acquisition_section_schema(acquisition)

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
    if section_name == "acquisition":
        return []
    if section_name == "processing":
        return ["schema", "path"]
    if section_name == "consolidation":
        return ["schema", "extracted_dir"]
    return ["schema"]


def _merge_config_fields(
    *,
    section_name: str,
    section: dict[str, Any],
    cli_values: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}

    field_map = {
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
        "profile_name": "profile",
        "provider": "provider",
        "model": "model",
        "limit": "limit",
        "skip_existing": "skip_existing",
        "max_context": "max_context",
        "enable_qa_qc": "enable_qaqc",
        "qaqc_lane": "qaqc_lane",
        "live_dashboard": "live_dashboard",
        "dry_run": "dry_run",
        "report_format": "report_format",
        "fail_on_suspicious": "fail_on_suspicious",
    }

    for cli_key, section_key in field_map.items():
        if section_key in section:
            merged[cli_key] = section.get(section_key)
            sources[cli_key] = f"config.{section_name}.{section_key}"

    if section_name == "acquisition":
        topology_config = section.get("topology")
        if isinstance(topology_config, dict) and topology_config.get("mode") is not None:
            merged["topology_mode"] = topology_config.get("mode")
            sources["topology_mode"] = "config.acquisition.topology.mode"

        if "hub_pages" in section:
            merged["hub_pages"] = section.get("hub_pages")
            sources["hub_pages"] = "config.acquisition.hub_pages"

        if "targets" in section:
            merged["targets"] = section.get("targets")
            sources["targets"] = "config.acquisition.targets"

        if "query_templates" in section:
            merged["query_templates"] = section.get("query_templates")
            sources["query_templates"] = "config.acquisition.query_templates"

        if "query_families" in section:
            merged["query_families"] = section.get("query_families")
            sources["query_families"] = "config.acquisition.query_families"

        if "allowed_domains" in section:
            merged["allowed_domains"] = section.get("allowed_domains")
            sources["allowed_domains"] = "config.acquisition.allowed_domains"

        if "request_headers" in section:
            merged["request_headers"] = section.get("request_headers")
            sources["request_headers"] = "config.acquisition.request_headers"

        discovery_rules = section.get("discovery_rules")
        if isinstance(discovery_rules, dict):
            if "include_url_patterns" in discovery_rules:
                merged["include_url_patterns"] = discovery_rules.get("include_url_patterns")
                sources["include_url_patterns"] = "config.acquisition.discovery_rules.include_url_patterns"
            if "include_link_text_patterns" in discovery_rules:
                merged["include_link_text_patterns"] = discovery_rules.get("include_link_text_patterns")
                sources["include_link_text_patterns"] = "config.acquisition.discovery_rules.include_link_text_patterns"

        runtime_config = section.get("runtime")
        if isinstance(runtime_config, dict):
            for runtime_key in (
                "max_depth",
                "max_pages",
                "max_files",
                "timeout_seconds",
                "max_concurrent_downloads",
                "min_request_interval_ms",
            ):
                if runtime_key in runtime_config:
                    merged[runtime_key] = runtime_config.get(runtime_key)
                    sources[runtime_key] = f"config.acquisition.runtime.{runtime_key}"

        retry_policy = section.get("retry_policy")
        if isinstance(retry_policy, dict):
            if "max_attempts" in retry_policy:
                merged["retry_max_attempts"] = retry_policy.get("max_attempts")
                sources["retry_max_attempts"] = "config.acquisition.retry_policy.max_attempts"
            if "initial_backoff_seconds" in retry_policy:
                merged["retry_initial_backoff_seconds"] = retry_policy.get("initial_backoff_seconds")
                sources["retry_initial_backoff_seconds"] = "config.acquisition.retry_policy.initial_backoff_seconds"
            if "max_backoff_seconds" in retry_policy:
                merged["retry_max_backoff_seconds"] = retry_policy.get("max_backoff_seconds")
                sources["retry_max_backoff_seconds"] = "config.acquisition.retry_policy.max_backoff_seconds"

        policy_config = section.get("policy")
        if isinstance(policy_config, dict):
            if "robots_mode" in policy_config:
                merged["robots_policy_mode"] = policy_config.get("robots_mode")
                sources["robots_policy_mode"] = "config.acquisition.policy.robots_mode"
            if "tos_mode" in policy_config:
                merged["tos_policy_mode"] = policy_config.get("tos_mode")
                sources["tos_policy_mode"] = "config.acquisition.policy.tos_mode"
            if "acknowledged_tos_domains" in policy_config:
                merged["acknowledged_tos_domains"] = policy_config.get("acknowledged_tos_domains")
                sources["acknowledged_tos_domains"] = "config.acquisition.policy.acknowledged_tos_domains"

        output_config = section.get("output")
        if isinstance(output_config, dict):
            if "documents_dir" in output_config:
                merged["output_documents"] = output_config.get("documents_dir")
                sources["output_documents"] = "config.acquisition.output.documents_dir"
            if "manifest_path" in output_config:
                merged["output_manifest"] = output_config.get("manifest_path")
                sources["output_manifest"] = "config.acquisition.output.manifest_path"

        search_config = section.get("search")
        if isinstance(search_config, dict):
            search_provider = str(search_config.get("provider") or "").lower()
            search_enabled = search_config.get("enabled")
            if search_provider == "serpapi" and search_enabled is not False:
                merged["enable_serpapi"] = True
                sources["enable_serpapi"] = "config.acquisition.search.provider"
            if "query_templates" in search_config:
                merged["query_templates"] = search_config.get("query_templates")
                sources["query_templates"] = "config.acquisition.search.query_templates"
            if "max_results_per_query" in search_config:
                merged["seeker_max_results"] = search_config.get("max_results_per_query")
                sources["seeker_max_results"] = "config.acquisition.search.max_results_per_query"

        seeker_config = section.get("seeker")
        if isinstance(seeker_config, dict):
            seeker_provider = str(seeker_config.get("provider") or "").lower()
            seeker_enabled = seeker_config.get("enabled")
            if seeker_provider == "serpapi" and seeker_enabled is not False:
                merged["enable_serpapi"] = True
                sources["enable_serpapi"] = "config.acquisition.seeker.provider"
            if "query_templates" in seeker_config:
                merged["query_templates"] = seeker_config.get("query_templates")
                sources["query_templates"] = "config.acquisition.seeker.query_templates"
            if "use_query_family" in seeker_config:
                merged["use_query_family"] = seeker_config.get("use_query_family")
                sources["use_query_family"] = "config.acquisition.seeker.use_query_family"
            if "max_results" in seeker_config:
                merged["seeker_max_results"] = seeker_config.get("max_results")
                sources["seeker_max_results"] = "config.acquisition.seeker.max_results"

        digger_config = section.get("digger")
        if isinstance(digger_config, dict):
            if "provider" in digger_config:
                merged["digger_provider"] = digger_config.get("provider")
                sources["digger_provider"] = "config.acquisition.digger.provider"
            if "connector" in digger_config:
                merged["digger_provider"] = digger_config.get("connector")
                sources["digger_provider"] = "config.acquisition.digger.connector"

            if "allowed_domains" in digger_config:
                merged["allowed_domains"] = digger_config.get("allowed_domains")
                sources["allowed_domains"] = "config.acquisition.digger.allowed_domains"

            index_page_mode = digger_config.get("index_page_mode")
            if isinstance(index_page_mode, dict):
                merged["index_page_mode"] = index_page_mode
                sources["index_page_mode"] = "config.acquisition.digger.index_page_mode"

            digger_rules = digger_config.get("discovery_rules")
            if isinstance(digger_rules, dict):
                if "include_url_patterns" in digger_rules:
                    merged["include_url_patterns"] = digger_rules.get("include_url_patterns")
                    sources["include_url_patterns"] = "config.acquisition.digger.discovery_rules.include_url_patterns"
                if "include_link_text_patterns" in digger_rules:
                    merged["include_link_text_patterns"] = digger_rules.get("include_link_text_patterns")
                    sources["include_link_text_patterns"] = "config.acquisition.digger.discovery_rules.include_link_text_patterns"

        routing_config = section.get("routing")
        if isinstance(routing_config, dict):
            if "index_links" in routing_config:
                merged["index_links"] = routing_config.get("index_links")
                sources["index_links"] = "config.acquisition.routing.index_links"

        link_prioritization = section.get("link_prioritization")
        if isinstance(link_prioritization, dict):
            if "mode" in link_prioritization:
                merged["link_prioritization_mode"] = link_prioritization.get("mode")
                sources["link_prioritization_mode"] = "config.acquisition.link_prioritization.mode"
            if "top_k" in link_prioritization:
                merged["link_top_k"] = link_prioritization.get("top_k")
                sources["link_top_k"] = "config.acquisition.link_prioritization.top_k"
            if "keywords" in link_prioritization:
                merged["link_prioritization_keywords"] = link_prioritization.get("keywords")
                sources["link_prioritization_keywords"] = "config.acquisition.link_prioritization.keywords"
            if "domain_scores" in link_prioritization:
                merged["link_prioritization_domain_scores"] = link_prioritization.get("domain_scores")
                sources["link_prioritization_domain_scores"] = "config.acquisition.link_prioritization.domain_scores"

        selection = section.get("selection")
        if isinstance(selection, dict):
            if "primary_per_target" in selection:
                merged["selection_primary_per_target"] = selection.get("primary_per_target")
                sources["selection_primary_per_target"] = "config.acquisition.selection.primary_per_target"
            if "exclude_draft" in selection:
                merged["selection_exclude_draft"] = selection.get("exclude_draft")
                sources["selection_exclude_draft"] = "config.acquisition.selection.exclude_draft"
            if "draft_patterns" in selection:
                merged["selection_draft_patterns"] = selection.get("draft_patterns")
                sources["selection_draft_patterns"] = "config.acquisition.selection.draft_patterns"
            if "relevance_require_any_terms" in selection:
                merged["selection_relevance_require_any_terms"] = selection.get("relevance_require_any_terms")
                sources["selection_relevance_require_any_terms"] = "config.acquisition.selection.relevance_require_any_terms"
            if "relevance_require_legal_marker_terms" in selection:
                merged["selection_relevance_require_legal_marker_terms"] = selection.get("relevance_require_legal_marker_terms")
                sources["selection_relevance_require_legal_marker_terms"] = "config.acquisition.selection.relevance_require_legal_marker_terms"
            if "relevance_exclude_any_terms" in selection:
                merged["selection_relevance_exclude_any_terms"] = selection.get("relevance_exclude_any_terms")
                sources["selection_relevance_exclude_any_terms"] = "config.acquisition.selection.relevance_exclude_any_terms"
            if "relevance_allowed_domain_patterns" in selection:
                merged["selection_relevance_allowed_domain_patterns"] = selection.get("relevance_allowed_domain_patterns")
                sources["selection_relevance_allowed_domain_patterns"] = "config.acquisition.selection.relevance_allowed_domain_patterns"
            if "require_supported_document" in selection:
                merged["selection_require_supported_document"] = selection.get("require_supported_document")
                sources["selection_require_supported_document"] = "config.acquisition.selection.require_supported_document"
            if "target_identity_require_any_templates" in selection:
                merged["selection_target_identity_require_any_templates"] = selection.get("target_identity_require_any_templates")
                sources["selection_target_identity_require_any_templates"] = "config.acquisition.selection.target_identity_require_any_templates"
            if "target_identity_require_all_templates" in selection:
                merged["selection_target_identity_require_all_templates"] = selection.get("target_identity_require_all_templates")
                sources["selection_target_identity_require_all_templates"] = "config.acquisition.selection.target_identity_require_all_templates"
            if "target_identity_exclude_any_templates" in selection:
                merged["selection_target_identity_exclude_any_templates"] = selection.get("target_identity_exclude_any_templates")
                sources["selection_target_identity_exclude_any_templates"] = "config.acquisition.selection.target_identity_exclude_any_templates"

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
        raise RuntimeConfigError(
            msg
        )

    if "domain" in cfg:
        merged["domain"] = cfg.get("domain")
        sources["domain"] = "config.domain"

    merged["_config_warnings"] = warnings
    merged["_config_sources"] = sources
    return merged
