"""`discover` command."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from psweep.cli.dashboard import create_discovery_live_dashboard
from psweep.cli.ui import (
    Verbosity,
    console,
    get_verbosity,
    print_info,
    print_success,
)
from psweep.config import RuntimeConfigError
from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.models import discovery_exit_code
from psweep.discovery.eligibility import eligibility_held
from psweep.discovery.request_inputs import discovery_request_fields


@click.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Domain run config file (targets, sources, queries, crawl, review)",
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
    "--domain",
    type=str,
    default=None,
    help="Domain key for the output workspace (overrides config)",
)
@click.option(
    "--source",
    "sources",
    multiple=True,
    help="URL to acquire with configured screening (repeatable; replaces discovery.sources)",
)
@click.option(
    "--query",
    "queries",
    multiple=True,
    help="Search query template (repeatable; replaces discovery.queries)",
)
@click.option(
    "--target-limit",
    "-n",
    type=click.IntRange(min=1),
    default=None,
    help="Use only the first N targets (pilot runs).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Search and emit a dry-run manifest without downloads",
)
@click.option(
    "--fresh",
    is_flag=True,
    default=False,
    help=(
        "Re-run all targets: clear the checkpoint, bypass the search cache, "
        "and replace curated partitions for targets processed this run."
    ),
)
@click.option("--quiet", "-q", is_flag=True, help="Minimal output")
@click.option("--verbose", "-v", is_flag=True, help="Detailed output")
@click.option("--debug", is_flag=True, help="Debug output with diagnostic context")
def discover(
    config_path: str | None,
    show_effective_config: bool,
    validate_config_only: bool,
    domain: str | None,  # noqa: ARG001 - read via _explicit_cli_overrides
    sources: tuple[str, ...],  # noqa: ARG001 - read via _explicit_cli_overrides
    queries: tuple[str, ...],  # noqa: ARG001 - read via _explicit_cli_overrides
    target_limit: int | None,  # noqa: ARG001 - read via _explicit_cli_overrides
    dry_run: bool,  # noqa: ARG001 - read via _explicit_cli_overrides
    fresh: bool,  # noqa: ARG001 - read via _explicit_cli_overrides
    quiet: bool,
    verbose: bool,
    debug: bool,
):
    """Discover and download source documents.

    Downloads configured sources and target URLs, searches with the configured
    queries, optionally crawls links, then reviews and curates the documents.
    Resumes from a checkpoint on later runs; pass --fresh to start over.

    Examples
    --------
    ::

        psweep discover --config config/my_domain/run.yaml
        psweep discover --source https://example.com/doc.pdf --domain my_domain
        psweep discover --config config/my_domain/run.yaml --dry-run --verbose
    """
    from psweep.cli.commands import (
        _explicit_cli_overrides,
        _print_effective_config,
        _resolve_runtime_command_inputs,
        begin_run,
    )

    view = begin_run("discover", quiet=quiet, verbose=verbose, debug=debug)

    cli_overrides = _explicit_cli_overrides(
        ["domain", "sources", "queries", "target_limit", "dry_run", "fresh"]
    )

    try:
        resolved_inputs = _resolve_runtime_command_inputs(
            command_name="discover",
            config_path=config_path,
            cli_values=cli_overrides,
        )
    except RuntimeConfigError as exc:
        view.error("Runtime config resolution failed", str(exc))
        sys.exit(1)

    if show_effective_config and not get_verbosity().is_quiet:
        _print_effective_config("discover", resolved_inputs)
        console.print()

    try:
        request_fields = discovery_request_fields(resolved_inputs)
    except ValueError as exc:
        view.error("Discovery preflight failed", str(exc))
        sys.exit(1)

    if validate_config_only:
        if get_verbosity().is_quiet:
            click.echo(
                json.dumps(
                    {
                        "command": "discover",
                        "status": "valid",
                        "resolved": request_fields,
                    },
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
            )
        else:
            print_success("Runtime config validation passed for discover command")
        return

    request = DiscoveryRequest(**request_fields)
    targets = request.targets or []
    search_enabled = DiscoveryEngine._search_enabled(request)  # noqa: SLF001 - package-internal helper

    if not view.is_quiet:
        config_info = {
            "Domain": request.domain,
            "Input": (
                f"sources={len(request.sources)}, targets={len(targets)}, "
                f"queries={len(request.queries)}"
            ),
            "Search": "serpapi" if search_enabled else "off",
            "Crawl": (
                f"{request.crawl_provider}, depth {request.max_depth}"
                if request.max_depth
                else "off"
            ),
            "Mode": "dry-run" if request.dry_run else "run",
            "Retention": request.retention_documents,
        }
        if request.fresh:
            config_info["Fresh"] = "checkpoint cleared, search cache bypassed"
        if resolved_inputs.get("target_limit") is not None:
            config_info["Target Limit"] = str(resolved_inputs["target_limit"])
        view.header("DISCOVERY")
        view.config(config_info)

        if targets:
            view.info(f"Preparing discovery plan for {len(targets)} target(s)...")
            if view.verbosity.shows_detail:
                preview_limit = 8
                for idx, target in enumerate(targets[:preview_limit], start=1):
                    label = DiscoveryEngine._target_label(target, idx, request.partition_by)  # noqa: SLF001 - package-internal helper
                    detail = target.get("query") or (
                        "query templates" if request.queries else "configured urls"
                    )
                    view.status("info", f"{idx}. {label}", detail)
                if len(targets) > preview_limit:
                    remaining = len(targets) - preview_limit
                    view.status("info", f"... and {remaining} more target(s)")
            console.print()

    from psweep.security import resolve_tls_verification

    try:
        resolve_tls_verification("DISCOVERY_SSL_VERIFY")
        if search_enabled:
            resolve_tls_verification("SERPAPI_SSL_VERIFY")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    discover_live = None
    discover_dashboard = None
    if view.verbosity in {Verbosity.NORMAL, Verbosity.VERBOSE} and not request.dry_run:
        discover_live, discover_dashboard = create_discovery_live_dashboard(
            domain=request.domain,
            mode="run",
            total_targets=len(targets),
            seeker_enabled=search_enabled,
        )

    def _discover_progress(message: str) -> None:
        if view.is_quiet:
            return
        if discover_dashboard is not None:
            discover_dashboard.push_event(message)
            return
        print_info(message)

    request.progress_callback = _discover_progress

    view.phase("Scanning for document sources")
    try:
        with view.live(discover_live):
            result = DiscoveryEngine().run(request)
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report any failure, then exit non-zero
        view.error("Discovery failed", str(exc))
        if view.verbosity is Verbosity.DEBUG:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    try:
        manifest_data = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        exit_code = discovery_exit_code(manifest_data)
    except (OSError, ValueError, TypeError) as exc:
        view.error("Cannot validate discovery manifest", str(exc))
        sys.exit(1)

    if view.is_quiet:
        click.echo(str(result.manifest_path))
        if exit_code:
            sys.exit(exit_code)
        return

    view.phase("Writing discovery index")
    notes = manifest_data.get("notes") or []
    errors = manifest_data.get("errors") or []
    held_count = sum(eligibility_held(record) for record in manifest_data.get("downloads", []))
    if errors:
        view.warning(
            "Discovery finished with errors; results may be partial.",
            f"run_id={result.run_id}; inspect the manifest before using results.",
        )
    elif manifest_data["status"] == "empty":
        view.warning("No usable documents acquired", str(result.manifest_path))
    else:
        view.success(f"Discovery complete (run_id={result.run_id})")
    download_summary = (manifest_data.get("stage_summaries") or {}).get("downloads") or {}
    downloaded_count = int(download_summary.get("downloaded") or 0)
    total_count = int(download_summary.get("total") or 0)
    reused_count = int(download_summary.get("reused") or 0)

    view.summary(
        {
            "Run ID": result.run_id,
            "Mode": "dry-run" if request.dry_run else "run",
            "Status": manifest_data.get("status") or "unknown",
            "Downloaded": f"{downloaded_count}/{total_count}"
            + (f" (+{reused_count} reused)" if reused_count else ""),
            **({"Held for review": str(held_count)} if held_count else {}),
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
            if error.get("target_label"):
                stage = f"{stage} [{error['target_label']}]"
            message = error.get("message") or error.get("error") or "unknown error"
            error_lines.append(f"{stage}: {message}")
        if len(errors) > 5:
            error_lines.append(f"... and {len(errors) - 5} more (see manifest for full details)")
        view.warnings(error_lines)

    outputs = {"Run folder": str(result.manifest_path.parent)}
    if result.documents_dir.is_dir():
        outputs["Downloads"] = str(result.documents_dir)
    if result.curated_dir is not None:
        outputs["Curated"] = f"{result.curated_count} document(s) → {result.curated_dir}"
    elif result.review_index_path is not None:
        outputs["Curated"] = "0 documents passed review"
    view.outputs(outputs)

    next_steps = []
    if result.curated_count == 0 and result.review_index_path is not None:
        # Zero curated — guide the user toward fixing the problem.
        next_steps.extend(
            (
                (
                    f"{held_count} PDF(s) held for human review; inspect eligibility evidence."
                    if held_count
                    else "No documents matched the review criteria. Options:"
                ),
                "  • Adjust discovery queries or selection filters in your config",
                (
                    f"  • Override automated selection: edit {result.review_index_path} "
                    "(set human_decision=keep), then run: pixi run psweep curate"
                ),
            )
        )
    elif result.review_index_path is not None:
        next_steps.append(
            f"Review/adjust picks: edit {result.review_index_path}, "
            "then run: pixi run psweep curate"
        )
    if result.curated_count > 0:
        extract_cmd = (
            f"pixi run psweep extract --config {Path(config_path).as_posix()}"
            if config_path
            else "pixi run psweep extract "
            f"{result.curated_dir or result.documents_dir} --schema <schema>"
        )
        next_steps.append(f"Extract the documents: {extract_cmd}")
    view.next_steps(next_steps)
    if exit_code:
        sys.exit(exit_code)
