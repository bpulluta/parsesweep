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

import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any, Optional, Sequence

import click
import typer
import typer.main
from typer.core import TyperGroup

from psweep import __version__
from psweep.pipeline import build_run_stage_commands, resolve_run_qaqc

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
        typer.Option("--config", help="Path to the domain run config file (e.g. config/<domain>/<domain>.yaml).", show_default=False),
    ],
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help="Ignore all previous work and start fresh — re-run every "
            "discovery target (ignoring the checkpoint and search cache) and "
            "re-extract every document.",
        ),
    ] = False,
    skip_discover: Annotated[
        bool,
        typer.Option("--skip-discover", help="Skip discovery (use existing curated docs)."),
    ] = False,
    skip_extract: Annotated[
        bool,
        typer.Option("--skip-extract", help="Skip extraction (compile from existing JSON)."),
    ] = False,
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
        psweep run --config config/my_domain/my_domain.yaml

    [bold]Adding new targets:[/bold]
        1. Add rows to config/<domain>/targets.csv
        2. Run: psweep run --config config/<domain>/<domain>.yaml
        3. Only new targets are processed; previous results are preserved.
    """
    from psweep.config import load_yaml_file
    from psweep.cli.run_view import RunView
    from psweep.cli.ui import Verbosity, set_verbosity

    if not config_path.exists():
        typer.echo(f"Error: Config not found: {config_path}", err=True)
        raise typer.Exit(1)

    resolved_verbosity = Verbosity.from_flags(quiet=quiet, verbose=verbose, debug=False)
    set_verbosity(resolved_verbosity)
    view = RunView("run", verbosity=resolved_verbosity)

    cfg = load_yaml_file(config_path)

    domain = cfg.get("domain", config_path.parent.name)
    discovery_cfg = cfg.get("discovery", {})
    targets_csv = config_path.parent / discovery_cfg.get("targets_csv", "targets.csv")

    # Build run plan
    target_labels: list[str] = []
    if targets_csv.exists():
        with targets_csv.open() as fh:
            target_labels = [row.get("label", "") for row in csv.DictReader(fh)]

    checkpoint_path = Path(f"discovered/{domain}/checkpoint.json")
    checkpointed: list[str] = []
    if checkpoint_path.exists() and not fresh:
        try:
            checkpointed = list(
                json.loads(checkpoint_path.read_text()).get("entries", {}).keys()
            )
        except Exception:
            pass

    new_targets = [t for t in target_labels if t not in checkpointed]
    curated_dir = Path(f"discovered/{domain}/curated")
    curated_count = (
        sum(
            1
            for f in curated_dir.rglob("*")
            if f.is_file() and ".text" not in str(f) and f.suffix != ".json"
        )
        if curated_dir.exists()
        else 0
    )

    extraction_dir = Path(
        cfg.get("extraction", {}).get("output_dir", f"extracted/{domain}")
    )
    extracted_count = 0
    if extraction_dir.exists():
        manifests_dir = extraction_dir / "run_manifests"
        all_json = sum(1 for _ in extraction_dir.rglob("*.json"))
        manifest_json = (
            sum(1 for _ in manifests_dir.rglob("*.json")) if manifests_dir.exists() else 0
        )
        extracted_count = all_json - manifest_json

    # Show run plan
    view.header()
    plan_rows: dict[str, str] = {"Domain": domain}
    if checkpointed and not fresh:
        plan_rows["Targets"] = (
            f"{len(target_labels)} total "
            f"({len(checkpointed)} cached, {len(new_targets)} new)"
        )
    else:
        plan_rows["Targets"] = (
            f"{len(target_labels)} total (all {'fresh' if fresh else 'new'})"
        )
    if curated_count and not fresh:
        plan_rows["Curated"] = f"{curated_count} docs (preserved)"
    if extracted_count and not fresh:
        plan_rows["Extracted"] = f"{extracted_count} docs (skip existing)"
    if fresh:
        plan_rows["Mode"] = "REPROCESS (ignore checkpoint, cache & extractions)"

    stages_list: list[str] = []
    if not skip_discover:
        stages_list.append("discover")

    qaqc = resolve_run_qaqc(config_path)
    qaqc_model_count: Optional[int] = None
    if qaqc:
        qaqc_model_count = len(qaqc.get("models") or []) or None

    if not skip_extract:
        stages_list.append("extract")
    if bool(qaqc) and not skip_extract:
        count = f" ×{qaqc_model_count} models" if qaqc_model_count else ""
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
    stage_cmds = build_run_stage_commands(
        config_path,
        base_cmd=["pixi", "run", "psweep"],
        skip_discover=skip_discover,
        skip_extract=skip_extract,
        fresh=fresh,
        extra_flags=flags,
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
    final_curated_dir = Path(f"discovered/{domain}/curated")
    if final_curated_dir.exists():
        doc_count = sum(
            1
            for f in final_curated_dir.rglob("*")
            if f.is_file() and ".text" not in str(f) and f.suffix != ".json"
        )
        if doc_count:
            summary_rows["Documents found"] = str(doc_count)
    if extraction_dir.exists():
        manifests_dir = extraction_dir / "run_manifests"
        all_json = sum(1 for _ in extraction_dir.rglob("*.json"))
        manifest_json = (
            sum(1 for _ in manifests_dir.rglob("*.json"))
            if manifests_dir.exists()
            else 0
        )
        final_extracted = all_json - manifest_json
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
