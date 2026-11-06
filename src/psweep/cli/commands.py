"""Shared CLI facade for ParseSweep commands and runtime helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import click
from click.core import ParameterSource
from rich.logging import RichHandler
from rich.markup import escape

from psweep.cli.run_view import RunView
from psweep.cli.ui import (
    Verbosity,
    console,
    key_values,
    print_header,
    set_verbosity,
)
from psweep.config import (
    config_key_paths,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.utils.log_context import current_document


def _explicit_cli_overrides(param_names: list[str]) -> dict[str, Any]:
    """Return only values that were explicitly provided on the command line."""
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return {}

    overrides: dict[str, Any] = {}
    for name in param_names:
        if ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE:
            overrides[name] = ctx.params.get(name)
    return overrides


def _print_effective_config(command_name: str, resolved_values: dict[str, Any]) -> None:
    """Print resolved values under their config key paths with source layer."""
    sources = resolved_values.get("_config_sources", {})
    paths = config_key_paths(command_name)
    display = {
        paths.get(key, key): (f"{value} [dim]({sources.get(key, 'default').split('.')[0]})[/dim]")
        for key, value in resolved_values.items()
        if not key.startswith("_")
    }
    print_header(f"{command_name.upper()} EFFECTIVE CONFIG")
    console.print(key_values(display))


def _resolve_runtime_command_inputs(
    *,
    command_name: str,
    config_path: str | None,
    cli_values: dict[str, Any],
) -> dict[str, Any]:
    """Load and resolve command inputs from config files and CLI overrides."""
    config_data = None

    if config_path:
        config_data = load_runtime_config_file(Path(config_path))

    result = resolve_command_config(
        command=command_name,
        cli_values=cli_values,
        config_data=config_data,
    )

    from psweep.security import validate_protected_paths
    from psweep.config import RuntimeConfigError

    try:
        validate_protected_paths(result)
    except ValueError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    return result


class _DocumentFormatter(logging.Formatter):
    """Prefix records with the document they were emitted for, unless they name it."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        name = current_document()
        return f"{escape(name)}: {message}" if name and name not in message else message


def configure_logging(verbosity: str) -> None:
    """Route log records through a RichHandler on the shared console.

    ``debug`` lowers only ParseSweep's own loggers; third-party libraries stay
    at WARNING so provider SDK request dumps never reach the terminal. Records
    emitted while a document is processed start with its name.
    """
    level = {"quiet": logging.ERROR, "debug": logging.DEBUG}.get(verbosity, logging.WARNING)
    debug = level == logging.DEBUG
    handler = RichHandler(
        console=console,
        show_time=False,
        show_level=debug,
        show_path=debug,
        markup=True,
    )
    handler.setFormatter(_DocumentFormatter())
    handler.setLevel(level)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(max(level, logging.WARNING))
    # Only ParseSweep's own loggers ever go below WARNING; otherwise they inherit.
    logging.getLogger(__name__.partition(".")[0]).setLevel(level if debug else logging.NOTSET)


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
