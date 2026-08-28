"""Typer application definition for ParseSweep.

This module defines the top-level Typer app and the ``run`` command.
All other commands are registered here — either as native Typer commands
(after migration) or via the Click compatibility bridge (during migration).

The entry point in pyproject.toml points to :func:`main` in this module:
    psweep = "psweep.cli.app:main"

Architecture note:
    Typer is used for newly written commands (type-hint driven, auto-help,
    tab completion). Existing Click commands are bridged in via
    ``typer.main.get_command(app).add_command()``. The resolved Click group
    is used as the actual entry point so bridged commands show up in --help.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any, Optional, Sequence

import click
import typer
import typer.main
from typer.core import TyperGroup

from psweep import __version__
from psweep.pipeline import (
    build_run_stage_commands,
    clear_run_extract_output,
    count_curated_documents,
    count_extracted_documents,
    read_checkpoint_entries,
    resolve_run_discovery_enabled,
    resolve_run_extraction_dir,
    resolve_run_validation,
    run_checkpoint_path,
)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class ParseSweepTyperGroup(TyperGroup):
    """Restore ClickException rendering for bridged legacy Click commands."""

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        """Run the group, keeping ClickException rendering for bridged cmds."""
        try:
            return super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except click.exceptions.Exit as exc:
            if standalone_mode:
                raise SystemExit(exc.exit_code) from exc
            return exc.exit_code
        except click.ClickException as exc:
            if not standalone_mode:
                raise
            exc.show()
            raise SystemExit(exc.exit_code) from exc


app = typer.Typer(
    name="psweep",
    help=(
        "📄 [bold]ParseSweep[/bold] — AI-powered structured data extraction "
        "from documents.\n\n"
        "[dim]Run [cyan]psweep[/cyan] with no arguments for an interactive "
        "guided menu.[/dim]"
    ),
    invoke_without_command=True,
    rich_markup_mode="rich",
    add_completion=True,
    no_args_is_help=False,
    cls=ParseSweepTyperGroup,
    pretty_exceptions_enable=False,
)


# ---------------------------------------------------------------------------
# Top-level callback — handles --version and no-args interactive mode
# ---------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"psweep {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show version and exit.",
            is_eager=True,
            callback=_version_callback,
            expose_value=False,
        ),
    ] = False,
) -> None:
    """ParseSweep: extract structured data from any document using LLMs + schemas."""
    if ctx.invoked_subcommand is None:
        # No subcommand given — launch the interactive guided menu
        from psweep.cli.interactive import run_interactive_menu
        run_interactive_menu()


# ---------------------------------------------------------------------------
# `run` command — full pipeline orchestrator
# ---------------------------------------------------------------------------


@app.command()
def run(
    config_path: Annotated[
        Path,
        typer.Option("--config", help="Path to the domain run config file (e.g. config/<domain>/run.yaml).", show_default=False),
    ],
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help="Ignore all previous work and start fresh — re-run every "
            "discovery target (ignoring the checkpoint and search cache), "
            "clear extracted and compiled outputs, then re-extract every "
            "document from the newly discovered curated set before "
            "recompiling.",
        ),
    ] = False,
    target_limit: Annotated[
        Optional[int],
        typer.Option(
            "--target-limit",
            "-n",
            min=1,
            help="Limit discovery to the first N configured targets for this run.",
        ),
    ] = None,
    retention_documents: Annotated[
        Optional[str],
        typer.Option(
            "--retention-documents",
            help="Discovery document retention mode: all, curated, or none.",
        ),
    ] = None,
    skip_discover: Annotated[
        bool,
        typer.Option("--skip-discover", help="Skip discovery (use existing curated docs)."),
    ] = False,
    skip_extract: Annotated[
        bool,
        typer.Option("--skip-extract", help="Skip extraction (compile from existing JSON)."),
    ] = False,
    validate_config_only: Annotated[
        bool,
        typer.Option(
            "--validate-config",
            help="Validate resolved run-stage config and exit without executing stages.",
        ),
    ] = False,
    config_strict: Annotated[
        bool,
        typer.Option(
            "--config-strict/--no-config-strict",
            help="Fail on unknown keys in runtime config sections during run preflight.",
        ),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt."),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Minimal output."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Detailed output."),
    ] = False,
) -> None:
    """Run the full pipeline: discover → extract → validate (optional) → compile.

    Shows what will be reused from previous runs and what is new, then
    asks for confirmation before proceeding. Use [cyan]--fresh[/cyan] to
    ignore all previous work and start fresh.

    [bold]Usage:[/bold]
        psweep run --config config/my_domain/run.yaml
        psweep run --config config/my_domain/run.yaml --target-limit 3 --fresh --yes

    [bold]Adding new targets:[/bold]
        1. Add rows to config/<domain>/targets.csv
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
        typer.echo(f"Error: Config not found: {config_path}", err=True)
        raise typer.Exit(1)

    resolved_verbosity = Verbosity.from_flags(quiet=quiet, verbose=verbose, debug=False)
    set_verbosity(resolved_verbosity)
    view = RunView("run", verbosity=resolved_verbosity)
    if retention_documents is not None:
        retention_documents = retention_documents.strip().lower()
        if retention_documents not in {"all", "curated", "none"}:
            view.error(
                "Invalid --retention-documents value",
                "Expected one of: all, curated, none",
            )
            raise typer.Exit(1)

    try:
        cfg = load_runtime_config_file(config_path)
    except RuntimeConfigError as exc:
        view.error("Runtime config validation failed", str(exc))
        raise typer.Exit(1)

    domain = cfg.get("domain", config_path.parent.name)
    discovery_enabled = resolve_run_discovery_enabled(config_path)
    discovery_cfg = cfg.get("discovery", {}) if discovery_enabled else {}

    # Build run plan
    target_labels: list[str] = []
    configured_targets = discovery_cfg.get("targets")
    if isinstance(configured_targets, list):
        target_labels = [
            str(row.get("label") or "").strip()
            for row in configured_targets
            if isinstance(row, dict)
        ]
        target_labels = [label for label in target_labels if label]
    planned_target_labels = (
        target_labels[:target_limit]
        if target_limit is not None
        else target_labels
    )

    preflight_commands: list[str] = []
    if not skip_discover and discovery_enabled:
        preflight_commands.append("discover")
    if not skip_extract:
        preflight_commands.append("extract")
    preflight_commands.append("compile")

    try:
        discover_inputs: dict[str, object] | None = None
        if discovery_enabled and not skip_discover:
            discover_inputs = resolve_command_config(
                command="discover",
                cli_values={
                    **(
                        {"target_limit": target_limit}
                        if target_limit is not None
                        else {}
                    ),
                    **(
                        {"retention_documents": retention_documents}
                        if retention_documents is not None
                        else {}
                    ),
                },
                config_data=cfg,
                strict=config_strict,
            )
        for command_name in preflight_commands:
            resolve_command_config(
                command=command_name,
                cli_values={},
                config_data=cfg,
                strict=config_strict,
            )
        if (
            not skip_extract
            and discover_inputs is not None
            and str(discover_inputs.get("retention_documents", "all")).lower() == "none"
        ):
            view.error(
                "Run preflight failed",
                "retention_documents='none' keeps no local docs and is incompatible "
                "with extract/compile run stages. Use --retention-documents curated "
                "or run discover-only.",
            )
            raise typer.Exit(1)
    except RuntimeConfigError as exc:
        view.error("Run preflight failed", str(exc))
        raise typer.Exit(1)

    if validate_config_only:
        if not view.is_quiet:
            view.success("Runtime config validation passed for run command")
        raise typer.Exit(0)

    checkpointed: list[str] = (
        [] if fresh else read_checkpoint_entries(run_checkpoint_path(domain))
    )

    new_targets = [t for t in planned_target_labels if t not in checkpointed]
    curated_dir = Path(f"discovered/{domain}/curated")
    curated_count = count_curated_documents(curated_dir)

    extraction_dir = resolve_run_extraction_dir(cfg, domain)
    extracted_count = count_extracted_documents(extraction_dir)

    # Show run plan
    view.header()
    plan_rows: dict[str, str] = {"Domain": domain}
    if not discovery_enabled:
        plan_rows["Targets"] = "n/a (discovery not configured)"
    elif target_limit is not None:
        limited_total = len(planned_target_labels)
        plan_rows["Targets"] = (
            f"{limited_total}/{len(target_labels)} configured "
            f"(limited by --target-limit)"
        )
    elif checkpointed and not fresh:
        plan_rows["Targets"] = (
            f"{len(planned_target_labels)} total "
            f"({len(checkpointed)} cached, {len(new_targets)} new)"
        )
    else:
        plan_rows["Targets"] = (
            f"{len(planned_target_labels)} total (all {'fresh' if fresh else 'new'})"
        )
    if curated_count and not fresh:
        plan_rows["Curated"] = f"{curated_count} docs (preserved)"
    if extracted_count and not fresh:
        plan_rows["Extracted"] = f"{extracted_count} docs (skip existing)"
    if fresh:
        plan_rows["Mode"] = "REPROCESS (ignore checkpoint/search cache; clear extracted+compiled outputs; re-extract docs)"

    stages_list: list[str] = []
    if not skip_discover and discovery_enabled:
        stages_list.append("discover")

    validation = resolve_run_validation(config_path)
    validation_model_count: Optional[int] = None
    if validation:
        validation_model_count = len(validation.get("models") or []) or None

    if not skip_extract:
        stages_list.append("extract")
    if bool(validation) and not skip_extract:
        count = f" ×{validation_model_count} models" if validation_model_count else ""
        stages_list.append(f"validate{count}")
    stages_list.append("compile")
    plan_rows["Stages"] = " → ".join(stages_list)

    view.config(plan_rows, title="Run Plan")

    if new_targets and not view.is_quiet:
        if len(new_targets) <= 10:
            for t in new_targets:
                view.detail(f"→ {t}")
        else:
            view.info(f"{len(new_targets)} new targets to process")

    # Confirm
    if not yes and not view.is_quiet:
        if not typer.confirm("Proceed?", default=True):
            view.info("Cancelled.")
            raise typer.Exit(0)

    flags: list[str] = []
    if quiet:
        flags.append("-q")
    elif verbose:
        flags.append("-v")
    pixi_exe = shutil.which("pixi")
    base_cmd = (
        [pixi_exe, "run", "psweep"]
        if pixi_exe
        else [sys.executable, "-m", "psweep.cli.main"]
    )

    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=base_cmd,
        skip_discover=skip_discover,
        skip_extract=skip_extract,
        discovery_enabled=discovery_enabled,
        fresh=fresh,
        target_limit=target_limit,
        retention_documents=retention_documents,
        extract_input_path=(
            Path(f"discovered/{domain}/latest/curated")
            if (not skip_discover and discovery_enabled and target_limit is not None)
            else None
        ),
        extra_flags=flags,
    )

    if fresh and not skip_extract:
        if clear_run_extract_output(extraction_dir) and not view.is_quiet:
            view.info(
                f"--fresh: cleared extracted output at {extraction_dir.as_posix()}"
            )

    _stage_labels = {
        "discover": "Discovering documents",
        "extract": "Extracting data",
        "validate": "Running QA/QC validation",
        "compile": "Compiling results",
    }
    total_stages = len(stage_cmds)
    for i, (stage_name, cmd) in enumerate(stage_cmds, 1):
        view.phase(
            f"Phase {i}/{total_stages}: {_stage_labels.get(stage_name, stage_name.title())}"
        )
        result = subprocess.run(cmd)
        if result.returncode != 0:
            view.error(f"Stage '{stage_name}' failed (exit {result.returncode})")
            raise typer.Exit(result.returncode)

    # Build a richer post-run summary from on-disk artifacts.
    summary_rows: dict[str, str] = {
        "Status": "complete",
        "Stages": " → ".join(stages_list),
    }
    # Report run-scoped discovery output when this invocation executed discovery.
    # Fallback to consolidated curated only when latest is unavailable.
    latest_curated_dir = Path(f"discovered/{domain}/latest/curated")
    consolidated_curated_dir = Path(f"discovered/{domain}/curated")
    final_curated_dir = (
        latest_curated_dir
        if (not skip_discover and discovery_enabled and latest_curated_dir.exists())
        else consolidated_curated_dir
    )
    doc_count = count_curated_documents(final_curated_dir)
    if doc_count:
        summary_rows["Documents found"] = str(doc_count)
    final_extracted = count_extracted_documents(extraction_dir)
    if final_extracted:
        summary_rows["Extracted"] = str(final_extracted)
    view.summary(summary_rows, title="Pipeline Complete")


# ---------------------------------------------------------------------------
# Build the final CLI: Typer app + bridged Click commands
#
# Pattern: generate the Click group from the Typer app once, then add
# existing Click commands to it. Use this Click group as the entry point.
# This means --help shows ALL commands (Typer-native + bridged Click ones)
# and tab completion works for the full command surface.
# ---------------------------------------------------------------------------


def _build_cli() -> "click.Group":  # type: ignore[name-defined]
    """Build the final Click group with all commands registered."""
    from psweep.cli.commands import (
        benchmark,
        check,
        compile,
        curate,
        discover,
        extract,
        validate,
    )
    from psweep.cli.utils_commands import (
        check_schema_cmd,
        config,
        estimate,
        init,
        init_domain_schema_cmd,
        preview,
    )

    click_group = typer.main.get_command(app)
    for cmd in (
        extract,
        check,
        compile,
        discover,
        curate,
        validate,
        benchmark,
        init,
        init_domain_schema_cmd,
        preview,
        estimate,
        check_schema_cmd,
        config,
    ):
        click_group.add_command(cmd)
    return click_group


# Build once at module load — this is the actual CLI entry point
_cli = _build_cli()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point registered in pyproject.toml."""
    _cli()


if __name__ == "__main__":
    main()
