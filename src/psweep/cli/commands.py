"""Shared CLI facade for ParseSweep commands and runtime helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
from click.core import ParameterSource
from rich.logging import RichHandler

from psweep.cli.run_view import RunView
from psweep.cli.ui import (
    Verbosity,
    ask_confirm,
    console,
    key_values,
    print_header,
    set_verbosity,
)
from psweep.config import load_runtime_config_file, resolve_command_config


def _explicit_cli_overrides(param_names: List[str]) -> Dict[str, Any]:
    """Return only values that were explicitly provided on the command line."""
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return {}

    overrides: Dict[str, Any] = {}
    for name in param_names:
        if ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE:
            overrides[name] = ctx.params.get(name)
    return overrides


def _print_effective_config(
    command_name: str, resolved_values: Dict[str, Any]
) -> None:
    """Print resolved runtime config values and their source layer."""
    sources = resolved_values.get("_config_sources", {})
    display = {
        key: f"{value} [dim]({sources.get(key, 'default')})[/dim]"
        for key, value in resolved_values.items()
        if not key.startswith("_")
    }
    print_header(f"{command_name.upper()} EFFECTIVE CONFIG")
    console.print(key_values(display))


def _resolve_runtime_command_inputs(
    *,
    command_name: str,
    config_path: Optional[str],
    strict: bool,
    cli_values: Dict[str, Any],
) -> Dict[str, Any]:
    """Load and resolve command inputs from config files and CLI overrides."""
    config_data = None

    if config_path:
        config_data = load_runtime_config_file(Path(config_path))

    result = resolve_command_config(
        command=command_name,
        cli_values=cli_values,
        config_data=config_data,
        strict=strict,
    )

    if config_data:
        extraction_section = config_data.get("extraction") or {}
        if extraction_section.get("input_dir"):
            result["_extraction_input_dir"] = extraction_section["input_dir"]

    return result


def configure_logging(verbosity: str) -> None:
    """Configure logging with RichHandler for clean integration with Rich UI."""
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    if verbosity == "quiet":
        level = logging.ERROR
        show_level = False
        show_path = False
    elif verbosity == "debug":
        level = logging.DEBUG
        show_level = True
        show_path = True
    else:
        level = logging.WARNING
        show_level = False
        show_path = False

    handler = RichHandler(
        console=console,
        show_time=False,
        show_level=show_level,
        show_path=show_path,
        markup=True,
    )
    handler.setLevel(level)
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


def begin_run(
    command: str,
    *,
    quiet: bool = False,
    verbose: bool = False,
    debug: bool = False,
) -> RunView:
    """Resolve verbosity, wire logging, and return a RunView for a command."""
    resolved = Verbosity.from_flags(quiet=quiet, verbose=verbose, debug=debug)
    set_verbosity(resolved)
    configure_logging(resolved.value)
    return RunView(command, verbosity=resolved)


def _format_runtime_artifact_summary(
    runtime_artifact: Optional[Dict[str, Any]],
) -> str:
    """Return a concise user-facing summary of the runtime configuration."""
    if not runtime_artifact:
        return "config-driven (schema + domain config)"

    lineage = runtime_artifact.get("lineage") or {}
    pack_name = (
        lineage.get("pack_name")
        or runtime_artifact.get("pack_name")
        or "unknown-pack"
    )
    profile_name = (
        lineage.get("profile_id")
        or runtime_artifact.get("profile_name")
        or "default"
    )
    artifact_id = (
        runtime_artifact.get("artifact_id")
        or lineage.get("artifact_id")
        or "artifact://runtime/unresolved"
    )
    artifact_suffix = artifact_id.rsplit("/", 1)[-1]
    return (
        f"pack={pack_name}, profile={profile_name}, artifact={artifact_suffix}"
    )


from psweep.cli.commands_benchmark import benchmark
from psweep.cli.commands_check import check
from psweep.cli.commands_compile import (
    _build_dedup_preview_report,
    _build_pipeline_accounting,
    _resolve_compilation_output_formats,
    _should_fail_on_suspicious,
    compile,
)
from psweep.cli.commands_curate import curate
from psweep.cli.commands_discover import discover
from psweep.cli.commands_extract import (
    _apply_page_targeting,
    _build_index_filters,
    _build_run_manifest,
    _context_budget_suggestions_for_process,
    _extract_and_save_result,
    _extract_one_document,
    _generate_run_id,
    _resolve_schema_ref,
    _row_matches_filters,
    _run_validation_extraction,
    _write_run_manifest,
    extract,
)
from psweep.cli.commands_validate import validate
