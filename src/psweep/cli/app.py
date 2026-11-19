"""Canonical Click application and full-pipeline command."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import click

from psweep import __version__
from psweep.cli.commands import domain_option
from psweep.cli.commands_compile import compile as compile_command
from psweep.cli.commands_curate import curate
from psweep.cli.commands_discover import discover
from psweep.cli.commands_extract import extract
from psweep.cli.commands_gc import gc
from psweep.cli.commands_validate import validate
from psweep.discovery.constants import DEFAULT_REVIEW_KEEP_TOP
from psweep.discovery.engine import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.garbage_collector import GarbageCollector
from psweep.discovery.request_inputs import discovery_request_fields
from psweep.discovery.targets import iter_targets
from psweep.exceptions import PipelineError
from psweep.outcomes import EXIT_PARTIAL
from psweep.pipeline import (
    build_run_stage_commands,
    count_curated_documents,
    count_extracted_documents,
    execute_run_stages,
    resolve_run_discovery_enabled,
)
from psweep.workspace import Workspace


def _paid_call_rows(
    cfg: dict[str, Any],
    discover_fields: dict[str, object] | None,
    stages: Sequence[str],
) -> dict[str, str]:
    """Run-plan rows for the paid stages; no network or LLM call is made.

    Search counts are an upper bound before the cache. Models resolve through
    the config's registry; any without a catalog price are listed because
    their spend is not metered in dollars.
    """
    from psweep.discovery.connectors.serpapi_seeker import resolve_search_queries
    from psweep.utils.model_pricing import get_model_pricing

    registry = cfg["_model_registry"]
    rows: dict[str, str] = {}
    models: list[str] = []

    def resolve(alias: object) -> str:
        name = registry.get_model(str(alias) if alias else None).model
        models.append(name)
        return name

    if discover_fields is not None and "discover" in stages:
        request = DiscoveryRequest(**discover_fields)
        # The engine's own per-target query builder, so the plan counts what search runs.
        inputs = DiscoveryEngine._build_seeker_inputs(request)  # noqa: SLF001
        queries = sum(len(resolve_search_queries(item)) for item in inputs)
        if queries:
            ceiling = request.max_search_calls
            rows["Search"] = (
                f"SerpApi ≤{queries} queries for {len(inputs)} "
                f"target{'' if len(inputs) == 1 else 's'} "
                f"(cache {'on' if request.seeker_cache else 'off'}; "
                f"ceiling {ceiling if ceiling is not None else 'none'})"
                + (" + fallback for weak targets" if request.fallback_queries else "")
            )
        review = request.document_review or {}
        if str(review.get("document_description") or "").strip():
            keep_top = review.get("keep_top", DEFAULT_REVIEW_KEEP_TOP)
            parts = [
                (
                    f"{resolve(review.get('model'))} grades "
                    f"≤{request.selection_primary_per_target} downloads/target, keeps {keep_top}"
                )
            ]
            if review.get("escalate_model"):
                parts.append(f"escalate {resolve(review['escalate_model'])}")
            if request.max_review_calls is not None:
                parts.append(f"ceiling {request.max_review_calls}/target")
            rows["Review"] = "; ".join(parts)

    if "extract" in stages:
        extraction = cfg.get("extraction") or {}
        extract_model = resolve(extraction.get("model"))
        row = extract_model
        targeting = extraction.get("page_targeting") or {}
        if targeting.get("enabled"):
            locator = resolve(targeting["model"]) if targeting.get("model") else extract_model
            row += f"; page targeting {locator}"
        rows["Extract"] = row
        if extraction.get("transcription_model"):
            resolve(extraction["transcription_model"])

    reconciliation = (cfg.get("compilation") or {}).get("reconciliation") or {}
    if reconciliation.get("enabled"):
        rows["Reconcile"] = resolve(reconciliation.get("model"))

    validation = cfg.get("validation") if "validate" in stages else None
    if validation:
        for alias in validation["models"]:
            resolve(alias)
        if validation["judge"]:
            resolve(validation["judge"])

    unpriced = [name for name in dict.fromkeys(models) if get_model_pricing(name) is None]
    if unpriced:
        rows["Price unknown"] = f"{', '.join(unpriced)} (spend will not be metered in dollars)"
    return rows


def partial_discovery_warning(workspace: Workspace) -> str:
    """Warn that extraction follows a partial discovery (read-only checkpoint scan)."""
    try:
        incomplete = len(
            GarbageCollector(workspace.root).scan_checkpoint_reachability().incomplete_owners
        )
        count = f" {incomplete} targets in the checkpoint are incomplete." if incomplete else ""
    except (OSError, ValueError):
        count = ""
    return (
        f"Discovery ended partial.{count} Extraction will use the current curated set, "
        "which keeps earlier documents for incomplete targets. Re-run `psweep discover` "
        "(completed work is reused) before paying for extraction, or let this run continue."
    )


@click.group(name="psweep", no_args_is_help=True)
@click.version_option(__version__, "--version", "-V", prog_name="psweep")
def cli() -> None:
    """Extract structured data from documents using LLMs and schemas."""
    from psweep.security import prepare_protected_runtime

    try:
        prepare_protected_runtime()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(path_type=Path),
    help="Path to the domain run configuration.",
)
@click.option("--fresh", is_flag=True, help="Re-run discovery, extraction, and compilation.")
@click.option(
    "--target-limit",
    "-n",
    type=click.IntRange(min=1),
    help="Use only the first N targets (pilot runs).",
)
@domain_option
@click.option("--skip-discover", is_flag=True, help="Use existing curated documents.")
@click.option("--skip-extract", is_flag=True, help="Compile existing extracted records.")
@click.option(
    "--validate-config",
    "validate_config_only",
    is_flag=True,
    help="Validate the run configuration without executing stages.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@click.option("--quiet", "-q", is_flag=True, help="Minimal output.")
@click.option("--verbose", "-v", is_flag=True, help="Detailed output.")
@click.option("--debug", is_flag=True, help="Debug logs from every stage (LLM requests, retries).")
def run(
    config_path: Path,
    fresh: bool = False,
    target_limit: int | None = None,
    domain: str | None = None,
    skip_discover: bool = False,
    skip_extract: bool = False,
    validate_config_only: bool = False,
    yes: bool = False,
    quiet: bool = False,
    verbose: bool = False,
    debug: bool = False,
) -> None:
    """Run the full pipeline: discover → extract → compile → validate.

    Shows what will be reused from previous runs and what is new, then
    asks for confirmation before proceeding. Use --fresh to
    ignore all previous work and start fresh.

    Usage:
        psweep run --config config/my_domain/run.yaml
        psweep run --config config/my_domain/run.yaml --target-limit 3 --fresh --yes

    Adding new targets:
        1. Add rows to the targets file named by discovery.targets
        2. Run: psweep run --config config/<domain>/run.yaml
        3. Only new targets are processed; previous results are preserved.
    """
    from psweep.config import (
        RuntimeConfigError,
        load_runtime_config_file,
        resolve_command_config,
    )
    from psweep.cli.run_view import RunView
    from psweep.cli.ui import Verbosity, set_verbosity

    if not config_path.exists():
        click.echo(f"Error: Config not found: {config_path}", err=True)
        raise click.exceptions.Exit(1)

    resolved_verbosity = Verbosity.from_flags(quiet=quiet, verbose=verbose, debug=debug)
    set_verbosity(resolved_verbosity)
    view = RunView("run", verbosity=resolved_verbosity)

    try:
        cfg = load_runtime_config_file(config_path, domain=domain)
    except RuntimeConfigError as exc:
        view.error("Runtime config validation failed", str(exc))
        raise click.exceptions.Exit(1) from None

    workspace = Workspace(cfg["domain"])  # the override, when given, was applied at load
    discovery_enabled = resolve_run_discovery_enabled(config_path)

    preflight_commands: list[str] = []
    if not skip_discover and discovery_enabled:
        preflight_commands.append("discover")
    if not skip_extract:
        preflight_commands.append("extract")
    preflight_commands.append("compile")
    validation = cfg.get("validation") if not skip_extract else None
    if validation:
        preflight_commands.append("validate")

    try:
        discover_fields: dict[str, object] | None = None
        configured_targets = 0
        if discovery_enabled and not skip_discover:
            discover_inputs = resolve_command_config(
                command="discover",
                cli_values={"target_limit": target_limit},
                config_data=cfg,
            )
            try:
                discover_fields = discovery_request_fields(discover_inputs)
            except ValueError as exc:
                raise RuntimeConfigError(str(exc)) from exc
            spec = discover_inputs.get("targets")
            if spec is not None:
                configured_targets = sum(1 for _ in iter_targets(spec))
        for command_name in preflight_commands:
            stage_inputs = resolve_command_config(
                command=command_name,
                cli_values={},
                config_data=cfg,
            )
            if command_name == "compile":
                from psweep.config.runtime_config_loader import resolve_compilation_metadata

                resolve_compilation_metadata(stage_inputs)
            elif command_name == "extract":
                from psweep.discovery.targets import TargetSpecError
                from psweep.extraction.roster import load_roster
                from psweep.extraction.schema_utils import load_schema
                from psweep.exceptions import SchemaError, ConfigurationError
                from psweep.utils.schema_metadata import SchemaMetadata

                try:
                    schema_path = Path(str(stage_inputs["schema"]))
                    load_schema(schema_path)
                    load_roster(stage_inputs, SchemaMetadata(schema_path))
                except (SchemaError, ConfigurationError, TargetSpecError) as exc:
                    raise RuntimeConfigError(str(exc)) from exc
        if (
            not skip_extract
            and discover_fields is not None
            and discover_fields.get("retention_documents") == "none"
        ):
            view.error(
                "Run preflight failed",
                "discovery.retention.documents 'none' keeps no local docs and is "
                "incompatible with extract/compile run stages. Use 'curated' "
                "or run discover-only.",
            )
            raise click.exceptions.Exit(1)
    except RuntimeConfigError as exc:
        view.error("Run preflight failed", str(exc))
        raise click.exceptions.Exit(1) from None

    if validate_config_only:
        if not view.is_quiet:
            view.success("Runtime config validation passed for run command")
        raise click.exceptions.Exit(0)

    curated_count = count_curated_documents(workspace.curated)
    extracted_count = count_extracted_documents(workspace.extracted)

    # Show run plan
    view.header()
    plan_rows: dict[str, str] = {"Domain": cfg["domain"]}
    if not discovery_enabled:
        plan_rows["Targets"] = "n/a (discovery not configured)"
    elif discover_fields is None:
        plan_rows["Targets"] = "n/a (--skip-discover)"
    else:
        planned = len(discover_fields["targets"] or [])
        plan_rows["Targets"] = (
            f"{planned}/{configured_targets} configured (limited by --target-limit)"
            if planned < configured_targets
            else f"{planned} configured (reuse verified by discovery)"
        )
    if curated_count and not fresh:
        plan_rows["Curated"] = f"{curated_count} docs (preserved)"
    if extracted_count and not fresh:
        plan_rows["Extracted"] = f"{extracted_count} docs (skip existing)"
    if fresh:
        plan_rows["Mode"] = (
            "REPROCESS (ignore checkpoint/search cache; clear "
            "extracted+compiled outputs; re-extract docs)"
        )

    stages_list: list[str] = []
    if not skip_discover and discovery_enabled:
        stages_list.append("discover")

    if not skip_extract:
        stages_list.append("extract")
    stages_list.append("compile")
    if validation:
        stages_list.append("validate")
        plan_rows["Validate"] = ", ".join(
            [
                f"{1 + len(validation['models'])} contenders",
                f"{len(validation['references'])} references",
                f"judge {validation['judge'] or 'off'}",
                *(
                    [f"min_accuracy {validation['min_accuracy']}"]
                    if validation["min_accuracy"] is not None
                    else []
                ),
            ]
        )
    plan_rows["Stages"] = " → ".join(stages_list)
    plan_rows.update(_paid_call_rows(cfg, discover_fields, stages_list))

    view.config(plan_rows, title="Run Plan")

    # Confirm
    if not yes and not view.is_quiet and not click.confirm("Proceed?", default=True):
        view.info("Cancelled.")
        raise click.exceptions.Exit(0)

    flags = {
        Verbosity.QUIET: ["-q"],
        Verbosity.VERBOSE: ["-v"],
        Verbosity.DEBUG: ["--debug"],
    }.get(resolved_verbosity, [])
    base_cmd = [sys.executable, "-m", "psweep.cli.main"]

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=base_cmd,
        skip_discover=skip_discover,
        skip_extract=skip_extract,
        discovery_enabled=discovery_enabled,
        fresh=fresh,
        target_limit=target_limit,
        extract_input_path=(
            workspace.latest / "curated"
            if (not skip_discover and discovery_enabled and target_limit is not None)
            else None
        ),
        extra_flags=flags,
        domain=domain,
    )

    stage_labels = {
        "discover": "Discovering documents",
        "extract": "Extracting data",
        "compile": "Compiling results",
        "validate": "Validating results",
    }
    total_stages = len(stage_cmds)

    def _announce(index: int, stage_name: str) -> None:
        view.phase(
            f"Phase {index}/{total_stages}: {stage_labels.get(stage_name, stage_name.title())}"
        )

    def _warn_partial(stage_name: str) -> None:
        if stage_name == "discover" and any(name == "extract" for name, _ in stage_cmds):
            view.warning(partial_discovery_warning(workspace))

    try:
        partial_stages = execute_run_stages(
            stage_cmds, on_stage=_announce, on_partial=_warn_partial
        )
    except PipelineError as exc:
        view.error(str(exc))
        raise click.exceptions.Exit(exc.returncode or 1) from exc

    # Build a richer post-run summary from on-disk artifacts.
    summary_rows: dict[str, str] = {
        "Status": (
            f"partial ({', '.join(partial_stages)} had errors; re-run retries transient ones)"
            if partial_stages
            else "complete"
        ),
        "Stages": " → ".join(stages_list),
    }
    # Report run-scoped discovery output when this invocation executed discovery.
    # Fallback to consolidated curated only when latest is unavailable.
    latest_curated_dir = workspace.latest / "curated"
    final_curated_dir = (
        latest_curated_dir
        if (not skip_discover and discovery_enabled and latest_curated_dir.exists())
        else workspace.curated
    )
    doc_count = count_curated_documents(final_curated_dir)
    if doc_count:
        summary_rows["Documents found"] = str(doc_count)
    final_extracted = count_extracted_documents(workspace.extracted)
    if final_extracted:
        summary_rows["Extracted"] = str(final_extracted)
    view.summary(summary_rows, title="Pipeline Complete")
    if partial_stages:
        raise click.exceptions.Exit(EXIT_PARTIAL)


def _register_commands() -> None:
    """Register every stage on the single public Click group."""
    from psweep.cli.utils_commands import (
        check_schema_cmd,
        config,
        estimate,
        init,
        init_domain_schema_cmd,
        preview,
    )

    for cmd in (
        extract,
        compile_command,
        validate,
        discover,
        curate,
        gc,
        init,
        init_domain_schema_cmd,
        preview,
        estimate,
        check_schema_cmd,
        config,
    ):
        cli.add_command(cmd)


_register_commands()


def main() -> None:
    """CLI entry point registered in pyproject.toml."""
    cli()


if __name__ == "__main__":
    main()
