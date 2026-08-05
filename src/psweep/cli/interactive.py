"""Interactive guided menu for ParseSweep.

Invoked when ``psweep`` is run with no arguments. Presents an InquirerPy
selection menu that guides users through the most common workflows without
needing to know command syntax.

Design principles:
- First-time-user friendly: no prior knowledge of CLI flags needed
- Ask only what is required; derive the rest from context
- Bail out cleanly if the user presses Ctrl-C or Escape
- Fall back to showing --help if InquirerPy fails (TTY-less environments)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

# ---------------------------------------------------------------------------
# Menu definition
# ---------------------------------------------------------------------------

_MENU_CHOICES = [
    {"name": "🚀  Run full pipeline   (discover → extract → compile)", "value": "run"},
    {"name": "📥  Extract documents   (folder → JSON)", "value": "extract"},
    {"name": "📊  Compile results     (JSON → Excel/CSV)", "value": "compile"},
    {"name": "🌐  Discover documents  (web → curated folder)", "value": "discover"},
    {"name": "🧪  Validate QA/QC      (multi-model extract + compare)", "value": "validate"},
    {"name": "─────────────────────────────────────────────────────", "value": "_sep1", "disabled": True},
    {"name": "🛠️   Init domain schema  (guided schema creation)", "value": "init-domain-schema"},
    {"name": "✅  Validate schema     (check $metadata + fields)", "value": "check-schema"},
    {"name": "💰  Estimate cost       (before running)", "value": "estimate"},
    {"name": "─────────────────────────────────────────────────────", "value": "_sep2", "disabled": True},
    {"name": "❓  Show help           (psweep --help)", "value": "help"},
    {"name": "✖   Exit", "value": "exit"},
]

_WORKFLOW_DESCRIPTIONS = {
    "run": "Run the full pipeline from a config file",
    "extract": "Extract structured data from documents in a folder",
    "compile": "Compile extracted JSON files into Excel/CSV",
    "discover": "Discover and download documents from the web",
    "validate": "Run explicit QA/QC validation stage",
    "init-domain-schema": "Create a new schema for a document domain",
    "check-schema": "Validate a schema file",
    "estimate": "Estimate extraction cost before running",
}


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------


def _ask_path(
    message: str,
    *,
    must_exist: bool = True,
    default: str | None = None,
) -> str | None:
    """Prompt for a file/folder path with optional existence check."""
    from InquirerPy import inquirer
    from InquirerPy.validator import PathValidator

    validators = []
    if must_exist:
        validators.append(PathValidator(is_file=False, message="Path does not exist."))

    try:
        value = inquirer.filepath(
            message=message,
            default=default or "",
            validate=PathValidator(message="Path does not exist.") if must_exist else None,
            only_directories=False,
        ).execute()
        return value.strip() if value else None
    except (KeyboardInterrupt, EOFError):
        return None


def _ask_text(message: str, *, default: str = "") -> str | None:
    """Prompt for free-form text input."""
    from InquirerPy import inquirer
    try:
        value = inquirer.text(message=message, default=default).execute()
        return value.strip() if value else None
    except (KeyboardInterrupt, EOFError):
        return None


def _ask_confirm(message: str, *, default: bool = True) -> bool:
    """Yes/no confirmation prompt."""
    from InquirerPy import inquirer
    try:
        return inquirer.confirm(message=message, default=default).execute()
    except (KeyboardInterrupt, EOFError):
        return False


# ---------------------------------------------------------------------------
# Per-workflow argument collection
# ---------------------------------------------------------------------------


def _collect_run_args() -> list[str] | None:
    """Collect arguments for the `run` command."""
    console.print("\n[bold]Run full pipeline[/bold] — needs a [cyan]run.yaml[/cyan] config file.")
    config = _ask_path("Config file path (run.yaml):", default="config/")
    if not config:
        return None
    args = ["run", "--config", config]
    if _ask_confirm("Extract all files fresh (default: skip already-extracted)?", default=False):
        args.append("--fresh")
    return args


def _collect_extract_args() -> list[str] | None:
    """Collect arguments for the `extract` command."""
    console.print(
        "\n[bold]Extract documents[/bold] — needs a [cyan]documents folder[/cyan] "
        "and a [cyan]schema file[/cyan]."
    )
    path = _ask_path("Documents folder:", default="documents/")
    if not path:
        return None
    schema = _ask_path("Schema file (.json):", default="schemas/personal/")
    if not schema:
        return None
    args = ["extract", path, "--schema", schema]
    if _ask_confirm("Use a config file instead? (for advanced options)", default=False):
        config = _ask_path("Config file path:")
        if config:
            args = ["extract", "--config", config]
    return args


def _collect_compile_args() -> list[str] | None:
    """Collect arguments for the `compile` command."""
    console.print("\n[bold]Compile extractions[/bold] — needs an [cyan]extracted/ folder[/cyan] and schema.")
    path = _ask_path("Extraction directory:", default="extracted/")
    if not path:
        return None
    schema = _ask_path("Schema file (.json):", default="schemas/personal/")
    if not schema:
        return None
    return ["compile", path, "--schema", schema]


def _collect_discover_args() -> list[str] | None:
    """Collect arguments for the `discover` command."""
    console.print("\n[bold]Discover documents[/bold] — runs from a [cyan]run.yaml[/cyan] config.")
    config = _ask_path("Config file path (run.yaml):", default="config/")
    if not config:
        return None
    return ["discover", "--config", config]


def _collect_validate_args() -> list[str] | None:
    """Collect arguments for the `validate` command."""
    console.print("\n[bold]Validate QA/QC[/bold] — runs multi-model extraction and comparison from [cyan]run.yaml[/cyan].")
    config = _ask_path("Config file path (run.yaml):", default="config/")
    if not config:
        return None
    args = ["validate", "--config", config]
    if _ask_confirm("Regenerate reports only (skip re-extraction)?", default=False):
        args.append("--compare-only")
    return args


def _collect_init_domain_schema_args() -> list[str] | None:
    """Collect arguments for the `init-domain-schema` command."""
    console.print("\n[bold]Create domain schema[/bold] — guided schema creation.")
    name = _ask_text("Domain name (e.g. solar_ordinances):", default="")
    if not name:
        return None
    args = ["init-domain-schema", "--name", name]
    ref = _ask_path(
        "Reference schema to base this on (optional, press Enter to use guided prompts):",
        must_exist=False,
    )
    if ref and Path(ref).exists():
        args += ["--reference-schema", ref]
    else:
        args.append("--interactive")
    return args


def _collect_check_schema_args() -> list[str] | None:
    """Collect arguments for the `check-schema` command."""
    path = _ask_path("Schema file (.json):", default="schemas/personal/")
    if not path:
        return None
    return ["check-schema", path]


def _collect_estimate_args() -> list[str] | None:
    """Collect arguments for the `estimate` command."""
    path = _ask_path("Documents folder to estimate:", default="documents/")
    if not path:
        return None
    return ["estimate", path]


_ARG_COLLECTORS: dict[str, Any] = {
    "run": _collect_run_args,
    "extract": _collect_extract_args,
    "compile": _collect_compile_args,
    "discover": _collect_discover_args,
    "validate": _collect_validate_args,
    "init-domain-schema": _collect_init_domain_schema_args,
    "check-schema": _collect_check_schema_args,
    "estimate": _collect_estimate_args,
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_interactive_menu() -> None:
    """Display the interactive menu and execute the selected workflow.

    Falls back to ``psweep --help`` if not running in an interactive TTY
    (e.g. piped output, CI environments).
    """
    if not sys.stdin.isatty():
        # Non-interactive: show help instead
        subprocess.run([sys.executable, "-m", "psweep.cli.main", "--help"])
        return

    try:
        from InquirerPy import inquirer
    except ImportError:
        subprocess.run([sys.executable, "-m", "psweep.cli.main", "--help"])
        return

    # Welcome banner
    console.print(
        Panel(
            Text.from_markup(
                "[bold white]📄 ParseSweep[/bold white]\n"
                "[dim]AI-powered structured data extraction from documents[/dim]"
            ),
            border_style="blue",
            padding=(0, 2),
        )
    )
    console.print()

    try:
        # Filter out disabled separator items for selection
        selectable = [c for c in _MENU_CHOICES if not c.get("disabled")]
        choice = inquirer.select(
            message="What would you like to do?",
            choices=[
                c["name"] if c.get("disabled") else c
                for c in selectable
            ],
            default=selectable[0]["name"],
        ).execute()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Bye![/dim]")
        raise SystemExit(0)

    # Resolve the value from the display name
    action = next(
        (c["value"] for c in selectable if c["name"] == choice),
        "exit",
    )

    if action == "exit":
        raise SystemExit(0)

    if action == "help":
        subprocess.run(
            [sys.executable, "-m", "psweep.cli.main", "--help"],
            check=False,
        )
        return

    # Collect required arguments via prompts
    collector = _ARG_COLLECTORS.get(action)
    if collector is None:
        # Fall through to show command help
        subprocess.run(
            [sys.executable, "-m", "psweep.cli.main", action, "--help"],
            check=False,
        )
        return

    console.print()
    args = collector()
    if args is None:
        console.print("\n[dim]Cancelled.[/dim]")
        raise SystemExit(0)

    # Show the resolved command before running it
    cmd_str = "psweep " + " ".join(args)
    console.print(f"\n[dim]Running:[/dim] [cyan]{cmd_str}[/cyan]\n")

    result = subprocess.run(
        [sys.executable, "-m", "psweep.cli.main"] + args,
        check=False,
    )
    raise SystemExit(result.returncode)
