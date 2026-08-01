"""`discover` command extracted from the legacy CLI monolith."""

from __future__ import annotations

import json
import os
import sys
import warnings as std_warnings
from pathlib import Path
from typing import Optional

import click

from psweep.cli.dashboard import create_discovery_live_dashboard
from psweep.cli.ui import (
    Verbosity,
    console,
    get_verbosity,
    print_error,
    print_info,
    print_success,
)
from psweep.config import RuntimeConfigError
from psweep.discovery import DiscoveryEngine, DiscoveryRequest


@click.command()
@click.argument("target", required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Domain config file (RECOMMENDED — includes schema, page targeting, dedup)",
)
@click.option(
    "--show-effective-config",
    is_flag=True,
    help="Print resolved command inputs with source attribution and continue",
)
@click.option(
    "--validate-config",
    "validate_config_only",
    is_flag=True,
    help="Validate resolved command inputs and exit without running discovery",
)
@click.option(
    "--config-strict",
    is_flag=True,
    help="Fail on unknown keys in runtime config sections",
)
@click.option(
    "--domain",
    type=str,
    default=None,
    help="Domain key for discovery output structure",
)
@click.option(
    "--seed-url",
    "seed_urls",
    multiple=True,
    help="Seed URL for discovery (repeatable)",
)
@click.option(
    "--query",
    type=str,
    default=None,
    help="Discovery query hint for seeker stage",
)
@click.option(
    "--state",
    type=str,
    default=None,
    help="Optional state hint used for jurisdiction-based output organization (for example: CA, Colorado)",
)
@click.option(
    "--jurisdiction",
    type=str,
    default=None,
    help="Optional jurisdiction hint used for output organization (for example: Imperial County)",
)
@click.option(
    "--partition-mode",
    type=click.Choice(["auto", "jurisdiction", "host"], case_sensitive=False),
    default=None,
    help="Download organization mode: auto prefers jurisdiction when available, else host",
)
@click.option(
    "--digger-provider",
    type=str,
    default=None,
    help="Digger provider (for example: seed_only, http, crawlee_playwright)",
)
@click.option(
    "--enable-serpapi/--disable-serpapi",
    default=None,
    help="Enable optional SerpApi seeker provider",
)
@click.option(
    "--max-concurrent-downloads",
    type=int,
    default=None,
    help="Maximum parallel downloads during discover runs",
)
@click.option(
    "--min-request-interval-ms",
    type=int,
    default=None,
    help="Minimum delay between outbound discovery requests in milliseconds",
)
@click.option(
    "--robots-policy-mode",
    type=click.Choice(["ignore", "warn", "enforce"], case_sensitive=False),
    default=None,
    help="Robots policy mode for target-site requests",
)
@click.option(
    "--tos-policy-mode",
    type=click.Choice(["ignore", "warn", "enforce"], case_sensitive=False),
    default=None,
    help="Terms acknowledgement mode for target-site requests",
)
@click.option(
    "--acknowledge-tos-domain",
    "acknowledged_tos_domains",
    multiple=True,
    help="Host or parent domain acknowledged for target-site terms checks (repeatable)",
)
@click.option(
    "--output-documents",
    type=click.Path(),
    default=None,
    help="Directory for discovered documents (defaults to run-scoped deterministic path)",
)
@click.option(
    "--output-manifest",
    type=click.Path(),
    default=None,
    help="Path to discovery manifest JSON (defaults to run-scoped deterministic path)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Discover and emit manifest scaffold without downloads",
)
@click.option(
    "--skip-existing/--reprocess",
    "skip_existing",
    default=True,
    show_default=True,
    help="Skip targets completed in a previous run; --reprocess ignores the "
    "checkpoint and refreshes the search cache to start fresh",
)
@click.option("--quiet", "-q", is_flag=True, help="Minimal output")
@click.option("--verbose", "-v", is_flag=True, help="Detailed output")
@click.option(
    "--debug", is_flag=True, help="Debug output with diagnostic context"
)
def discover(
    target: Optional[str],
    config_path: Optional[str],
    show_effective_config: bool,
    validate_config_only: bool,
    config_strict: bool,
    domain: Optional[str],
    seed_urls: tuple[str, ...],
    query: Optional[str],
    state: Optional[str],
    jurisdiction: Optional[str],
    partition_mode: Optional[str],
    digger_provider: Optional[str],
    enable_serpapi: Optional[bool],
    max_concurrent_downloads: Optional[int],
    min_request_interval_ms: Optional[int],
    robots_policy_mode: Optional[str],
    tos_policy_mode: Optional[str],
    acknowledged_tos_domains: tuple[str, ...],
    output_documents: Optional[str],
    output_manifest: Optional[str],
    dry_run: bool,
    skip_existing: bool,
    quiet: bool,
    verbose: bool,
    debug: bool,
):
    """Discover and download source documents from web targets."""
    from psweep.cli.commands import (
        _explicit_cli_overrides,
        _print_effective_config,
        _resolve_runtime_command_inputs,
        begin_run,
    )

    view = begin_run("discover", quiet=quiet, verbose=verbose, debug=debug)

    cli_overrides = _explicit_cli_overrides(
        [
            "domain",
            "seed_urls",
            "query",
            "state",
            "jurisdiction",
            "partition_mode",
            "digger_provider",
            "enable_serpapi",
            "max_concurrent_downloads",
            "min_request_interval_ms",
            "robots_policy_mode",
            "tos_policy_mode",
            "acknowledged_tos_domains",
            "output_documents",
            "output_manifest",
            "dry_run",
        ]
    )

    try:
        resolved_inputs = _resolve_runtime_command_inputs(
            command_name="discover",
            config_path=config_path,
            strict=config_strict,
            cli_values=cli_overrides,
        )
    except RuntimeConfigError as exc:
        view.error("Runtime config resolution failed", str(exc))
        sys.exit(1)

    warnings = resolved_inputs.get("_config_warnings", [])
    view.warnings(warnings)

    if show_effective_config and not get_verbosity().is_quiet:
        _print_effective_config("discover", resolved_inputs)
        console.print()

    if validate_config_only:
        if get_verbosity().is_quiet:
            click.echo(
                json.dumps(
                    {
                        "command": "discover",
                        "status": "valid",
                        "resolved": {
                            k: v
                            for k, v in resolved_inputs.items()
                            if not k.startswith("_")
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print_success(
                "Runtime config validation passed for discover command"
            )
        return

    resolved_domain = (
        resolved_inputs.get("domain") or domain or target or "default"
    )
    resolved_seed_urls = list(resolved_inputs.get("seed_urls") or seed_urls)
    resolved_query = resolved_inputs.get("query", query)
    resolved_state = resolved_inputs.get("state", state)
    resolved_jurisdiction = resolved_inputs.get("jurisdiction", jurisdiction)
    resolved_partition_mode = (
        resolved_inputs.get("partition_mode", partition_mode) or "auto"
    ).lower()
    resolved_digger_provider = (
        (
            resolved_inputs.get("digger_provider", digger_provider)
            or "seed_only"
        )
        .strip()
        .lower()
    )
    resolved_topology_mode = resolved_inputs.get("topology_mode")
    resolved_enable_serpapi = bool(
        resolved_inputs.get("enable_serpapi", enable_serpapi or False)
    )
    resolved_hub_pages = resolved_inputs.get("hub_pages") or None
    resolved_allowed_domains = resolved_inputs.get("allowed_domains") or None
    resolved_targets = resolved_inputs.get("targets") or None
    resolved_query_templates = resolved_inputs.get("query_templates") or None
    resolved_query_families = resolved_inputs.get("query_families") or None
    resolved_use_query_family = resolved_inputs.get("use_query_family")
    resolved_seeker_max_results = int(
        resolved_inputs.get("seeker_max_results", 10) or 10
    )
    resolved_link_prioritization_mode = str(
        resolved_inputs.get("link_prioritization_mode", "heuristic")
        or "heuristic"
    ).lower()
    # 0 = no global cap; per-target selection controls recall.
    resolved_link_top_k = int(resolved_inputs.get("link_top_k", 0) or 0)
    resolved_link_prioritization_keywords = (
        resolved_inputs.get("link_prioritization_keywords") or None
    )
    resolved_link_prioritization_domain_scores = (
        resolved_inputs.get("link_prioritization_domain_scores") or None
    )
    resolved_power_range_kw = resolved_inputs.get("power_range_kw") or None
    resolved_selection_primary_per_target = int(
        resolved_inputs.get("selection_primary_per_target", 1) or 1
    )
    resolved_selection_exclude_draft = bool(
        resolved_inputs.get("selection_exclude_draft", True)
    )
    resolved_selection_draft_patterns = (
        resolved_inputs.get("selection_draft_patterns") or None
    )
    resolved_selection_relevance_require_any_terms = (
        resolved_inputs.get("selection_relevance_require_any_terms") or None
    )
    resolved_selection_relevance_require_legal_marker_terms = (
        resolved_inputs.get("selection_relevance_require_legal_marker_terms")
        or None
    )
    resolved_selection_relevance_exclude_any_terms = (
        resolved_inputs.get("selection_relevance_exclude_any_terms") or None
    )
    resolved_selection_relevance_allowed_domain_patterns = (
        resolved_inputs.get("selection_relevance_allowed_domain_patterns")
        or None
    )
    resolved_selection_require_supported_document = bool(
        resolved_inputs.get("selection_require_supported_document", True)
    )
    resolved_selection_target_identity_require_any_templates = (
        resolved_inputs.get("selection_target_identity_require_any_templates")
        or None
    )
    resolved_selection_target_identity_require_all_templates = (
        resolved_inputs.get("selection_target_identity_require_all_templates")
        or None
    )
    resolved_selection_target_identity_exclude_any_templates = (
        resolved_inputs.get("selection_target_identity_exclude_any_templates")
        or None
    )
    resolved_include_url_patterns = (
        resolved_inputs.get("include_url_patterns") or None
    )
    resolved_include_link_text_patterns = (
        resolved_inputs.get("include_link_text_patterns") or None
    )
    resolved_index_page_mode = resolved_inputs.get("index_page_mode") or None
    resolved_index_links = resolved_inputs.get("index_links") or None
    resolved_max_depth = resolved_inputs.get("max_depth")
    resolved_max_pages = resolved_inputs.get("max_pages")
    resolved_max_files = resolved_inputs.get("max_files")
    resolved_timeout_seconds = resolved_inputs.get("timeout_seconds")
    resolved_retry_max_attempts = int(
        resolved_inputs.get("retry_max_attempts", 3) or 3
    )
    resolved_retry_initial_backoff_seconds = float(
        resolved_inputs.get("retry_initial_backoff_seconds", 1.0) or 1.0
    )
    resolved_retry_max_backoff_seconds = float(
        resolved_inputs.get("retry_max_backoff_seconds", 8.0) or 8.0
    )
    resolved_max_concurrent_downloads = int(
        resolved_inputs.get(
            "max_concurrent_downloads", max_concurrent_downloads or 2
        )
        or 2
    )
    resolved_min_request_interval_ms = int(
        resolved_inputs.get(
            "min_request_interval_ms", min_request_interval_ms or 0
        )
        or 0
    )
    resolved_robots_policy_mode = str(
        resolved_inputs.get(
            "robots_policy_mode", robots_policy_mode or "ignore"
        )
        or "ignore"
    ).lower()
    resolved_tos_policy_mode = str(
        resolved_inputs.get("tos_policy_mode", tos_policy_mode or "ignore")
        or "ignore"
    ).lower()
    resolved_acknowledged_tos_domains = list(
        resolved_inputs.get("acknowledged_tos_domains")
        or acknowledged_tos_domains
        or []
    )
    resolved_document_classifier = (
        resolved_inputs.get("document_classifier") or None
    )
    resolved_document_review = (
        resolved_inputs.get("document_review") or None
    )
    resolved_models = resolved_inputs.get("models") or None
    resolved_seeker_cache = bool(resolved_inputs.get("seeker_cache") or False)
    resolved_seeker_cache_ttl_minutes = float(
        resolved_inputs.get("seeker_cache_ttl_minutes", 0) or 0
    )
    resolved_query_context_aliases = (
        resolved_inputs.get("query_context_aliases") or None
    )
    resolved_partition_by = resolved_inputs.get("partition_by") or None
    resolved_browser_mode = bool(resolved_inputs.get("browser_mode") or False)
    _raw_seeker_extra = resolved_inputs.get("seeker_extra_params")
    resolved_seeker_extra_params = (
        dict(_raw_seeker_extra) if isinstance(_raw_seeker_extra, dict) else None
    )

    if not resolved_seed_urls and not resolved_query and not resolved_targets:
        print_error(
            "Discovery input missing",
            "Provide at least one --seed-url, --query, or discovery.targets entry in config.",
        )
        sys.exit(1)

    resolved_output_documents = (
        resolved_inputs.get("output_documents") or output_documents
    )
    resolved_output_manifest = (
        resolved_inputs.get("output_manifest") or output_manifest
    )

    documents_dir = (
        Path(resolved_output_documents) if resolved_output_documents else None
    )
    manifest_path = (
        Path(resolved_output_manifest) if resolved_output_manifest else None
    )

    if not view.is_quiet:
        if view.verbosity.shows_detail:
            config_info = {
                "Domain": resolved_domain,
                "Seeds": str(len(resolved_seed_urls)),
                "Query": resolved_query or "(none)",
                "State": resolved_state or "(none)",
                "Jurisdiction": resolved_jurisdiction or "(none)",
                "Partition Mode": resolved_partition_mode,
                "Digger Provider": resolved_digger_provider,
                "Topology": resolved_topology_mode or "(default)",
                "Hub Pages": str(len(resolved_hub_pages or [])),
                "Targets": str(len(resolved_targets or [])),
                "Seeker": "serpapi"
                if resolved_enable_serpapi
                else "seed-only",
                "Max Concurrent Downloads": str(
                    max(1, resolved_max_concurrent_downloads)
                ),
                "Min Request Interval (ms)": str(
                    max(0, resolved_min_request_interval_ms)
                ),
                "Robots Policy": resolved_robots_policy_mode,
                "ToS Policy": resolved_tos_policy_mode,
                "Acknowledged ToS Domains": str(
                    len(resolved_acknowledged_tos_domains)
                ),
                "Documents Output": str(documents_dir)
                if documents_dir
                else "(auto: run-scoped)",
                "Manifest": str(manifest_path)
                if manifest_path
                else "(auto: run-scoped)",
                "Mode": "dry-run" if dry_run else "run",
            }
            if not skip_existing:
                config_info["Reprocess"] = "yes (ignore checkpoint + cache)"
        else:
            config_info = {
                "Domain": resolved_domain,
                "Input": f"seeds={len(resolved_seed_urls)}, targets={len(resolved_targets or [])}, query={'yes' if resolved_query else 'no'}",
                "Seeker": "serpapi"
                if resolved_enable_serpapi
                else "seed-only",
                "Topology": resolved_topology_mode or "(default)",
                "Documents Output": str(documents_dir)
                if documents_dir
                else "(auto: run-scoped)",
                "Mode": "dry-run" if dry_run else "run",
            }
        view.header("DISCOVERY")
        view.config(config_info)

        if resolved_targets:
            view.info(
                f"Preparing discovery plan for {len(resolved_targets)} target(s)..."
            )
            if view.verbosity.shows_detail:
                preview_limit = 8
                for idx, target_meta in enumerate(
                    resolved_targets[:preview_limit], start=1
                ):
                    label = DiscoveryEngine._target_label(
                        target_meta if isinstance(target_meta, dict) else None, idx
                    )
                    query_preview = (
                        (target_meta.get("query") if isinstance(target_meta, dict) else None)
                        or resolved_query
                        or "(query templates)"
                    )
                    view.status("info", f"{idx}. {label}", f"query: {query_preview}")
                if len(resolved_targets) > preview_limit:
                    remaining = len(resolved_targets) - preview_limit
                    view.status("info", f"... and {remaining} more target(s)")
            console.print()

    # Keep urllib3 TLS warnings out of the CLI output and surface a single
    # styled warning instead so the run view remains cohesive.
    try:
        from urllib3.exceptions import InsecureRequestWarning

        std_warnings.filterwarnings("ignore", category=InsecureRequestWarning)
    except Exception:
        pass

    def _env_ssl_verify(env_var: str, fallback_var: str) -> bool:
        raw = os.getenv(env_var) or os.getenv(fallback_var) or "false"
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}

    seeker_ssl_verify = _env_ssl_verify(
        "SERPAPI_SSL_VERIFY", "PSWEEP_SSL_VERIFY"
    )
    download_ssl_verify = _env_ssl_verify(
        "DISCOVERY_SSL_VERIFY", "PSWEEP_SSL_VERIFY"
    )

    if resolved_enable_serpapi and not seeker_ssl_verify:
        view.warning(
            "SerpApi TLS verification is disabled (SERPAPI_SSL_VERIFY=false).",
            "Set SERPAPI_SSL_VERIFY=true to suppress insecure-request warnings.",
        )
    if not dry_run and not download_ssl_verify:
        view.warning(
            "Download TLS verification is disabled (DISCOVERY_SSL_VERIFY=false)."
        )

    discover_live = None
    discover_dashboard = None
    if view.verbosity in {Verbosity.NORMAL, Verbosity.VERBOSE} and not dry_run:
        discover_live, discover_dashboard = create_discovery_live_dashboard(
            domain=resolved_domain,
            mode="dry-run" if dry_run else "run",
            total_targets=len(resolved_targets or []),
            seeker_enabled=resolved_enable_serpapi,
        )

    def _discover_progress(message: str) -> None:
        if view.is_quiet:
            return
        if discover_dashboard is not None:
            discover_dashboard.push_event(message)
            return
        print_info(message)

    request = DiscoveryRequest(
        domain=resolved_domain,
        seed_urls=resolved_seed_urls,
        query=resolved_query,
        enable_serpapi=resolved_enable_serpapi,
        output_documents=documents_dir,
        output_manifest=manifest_path,
        dry_run=dry_run,
        state=resolved_state,
        jurisdiction=resolved_jurisdiction,
        partition_mode=resolved_partition_mode,
        digger_provider=resolved_digger_provider,
        topology_mode=resolved_topology_mode,
        hub_pages=resolved_hub_pages,
        allowed_domains=resolved_allowed_domains,
        targets=resolved_targets,
        query_templates=resolved_query_templates,
        query_families=resolved_query_families,
        use_query_family=resolved_use_query_family,
        seeker_max_results=max(1, resolved_seeker_max_results),
        link_prioritization_mode=resolved_link_prioritization_mode,
        link_top_k=max(0, resolved_link_top_k),
        link_prioritization_keywords=resolved_link_prioritization_keywords,
        link_prioritization_domain_scores=resolved_link_prioritization_domain_scores,
        power_range_kw=resolved_power_range_kw,
        selection_primary_per_target=max(
            1, resolved_selection_primary_per_target
        ),
        selection_exclude_draft=resolved_selection_exclude_draft,
        selection_draft_patterns=resolved_selection_draft_patterns,
        selection_relevance_require_any_terms=resolved_selection_relevance_require_any_terms,
        selection_relevance_require_legal_marker_terms=resolved_selection_relevance_require_legal_marker_terms,
        selection_relevance_exclude_any_terms=resolved_selection_relevance_exclude_any_terms,
        selection_relevance_allowed_domain_patterns=resolved_selection_relevance_allowed_domain_patterns,
        selection_require_supported_document=resolved_selection_require_supported_document,
        selection_target_identity_require_any_templates=resolved_selection_target_identity_require_any_templates,
        selection_target_identity_require_all_templates=resolved_selection_target_identity_require_all_templates,
        selection_target_identity_exclude_any_templates=resolved_selection_target_identity_exclude_any_templates,
        document_classifier=resolved_document_classifier,
        document_review=resolved_document_review,
        models=resolved_models,
        seeker_cache=resolved_seeker_cache,
        seeker_cache_ttl_minutes=resolved_seeker_cache_ttl_minutes,
        reprocess=not skip_existing,
        progress_callback=_discover_progress,
        query_context_aliases=resolved_query_context_aliases,
        partition_by=resolved_partition_by,
        browser_mode=resolved_browser_mode,
        seeker_extra_params=resolved_seeker_extra_params,
        include_url_patterns=resolved_include_url_patterns,
        include_link_text_patterns=resolved_include_link_text_patterns,
        index_page_mode=resolved_index_page_mode,
        index_links=resolved_index_links,
        max_depth=resolved_max_depth,
        max_pages=resolved_max_pages,
        max_files=resolved_max_files,
        timeout_seconds=resolved_timeout_seconds,
        retry_max_attempts=resolved_retry_max_attempts,
        retry_initial_backoff_seconds=resolved_retry_initial_backoff_seconds,
        retry_max_backoff_seconds=resolved_retry_max_backoff_seconds,
        max_concurrent_downloads=max(1, resolved_max_concurrent_downloads),
        min_request_interval_ms=max(0, resolved_min_request_interval_ms),
        robots_policy_mode=resolved_robots_policy_mode,
        tos_policy_mode=resolved_tos_policy_mode,
        acknowledged_tos_domains=resolved_acknowledged_tos_domains or None,
    )

    view.phase("Scanning for document sources...")
    try:
        with view.live(discover_live):
            result = DiscoveryEngine().run(request)
    except Exception as exc:
        view.error("Discovery failed", str(exc))
        if view.verbosity is Verbosity.DEBUG:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    if view.is_quiet:
        click.echo(str(result.manifest_path))
        return

    view.phase("Writing discovery index...")
    view.success(f"Discovery complete (run_id={result.run_id})")

    try:
        manifest_data = json.loads(
            result.manifest_path.read_text(encoding="utf-8")
        )
    except Exception:
        manifest_data = {}

    notes = manifest_data.get("notes") or []
    errors = manifest_data.get("errors") or []
    download_summary = (
        (manifest_data.get("stage_summaries") or {}).get("downloads") or {}
    )
    downloaded_count = int(download_summary.get("downloaded") or 0)
    total_count = int(download_summary.get("total") or 0)

    view.summary(
        {
            "Run ID": result.run_id,
            "Mode": "dry-run" if dry_run else "run",
            "Downloaded": f"{downloaded_count}/{total_count}",
            "Notes": str(len(notes)),
            "Errors": str(len(errors)),
        },
        title="Discovery Summary",
    )

    view.notes(notes, title="Discovery Notes")

    if errors:
        error_lines = []
        for error in errors[:5]:
            stage = error.get("stage") or "unknown-stage"
            message = error.get("message") or error.get("error") or "unknown error"
            error_lines.append(f"{stage}: {message}")
        if len(errors) > 5:
            error_lines.append(
                f"... and {len(errors) - 5} more (see manifest for full details)"
            )
        view.warnings(error_lines)

    outputs = {
        "Run folder": str(result.manifest_path.parent),
        "Downloads": str(result.documents_dir),
    }
    if result.curated_dir is not None:
        outputs["Curated"] = (
            f"{result.curated_count} document(s) → {result.curated_dir}"
        )
    elif result.review_index_path is not None:
        outputs["Curated"] = "0 documents passed review"
    view.outputs(outputs)

    next_steps = []
    if result.curated_count == 0 and result.review_index_path is not None:
        # Zero curated — guide the user toward fixing the problem.
        next_steps.append(
            "No documents matched the review criteria. Options:"
        )
        next_steps.append(
            "  • Adjust discovery queries or selection filters in your config"
        )
        next_steps.append(
            f"  • Override LLM picks: edit {result.review_index_path} "
            "(set human_decision=keep), then run: pixi run psweep curate"
        )
    elif result.review_index_path is not None:
        next_steps.append(
            f"Review/adjust picks: edit {result.review_index_path}, "
            "then run: pixi run psweep curate"
        )
    if result.curated_count > 0:
        next_steps.append(
            "Extract the documents: pixi run psweep extract "
            f"{result.curated_dir or result.documents_dir} --schema <schema>"
        )
    view.next_steps(next_steps)
