"""Rich UI design system for the StreamlineExtract CLI.

This module is the single source of truth for terminal presentation. Every
command renders through the primitives defined here so the whole tool reads as
one product: one theme, one set of icons, one divider, one way to show
key/value data, status, notes, outputs, and next steps.

Higher-level commands assemble these primitives through
``streamline_extract.cli.run_view.RunView``, which enforces the section
contract (header -> config -> progress -> summary -> notes -> next steps).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, List, Mapping, Sequence, Tuple, Union
from pathlib import Path

from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TaskProgressColumn,
    MofNCompleteColumn,
)
from rich.prompt import Prompt, Confirm
from rich.syntax import Syntax
from rich.tree import Tree

# Global console instance shared by the whole CLI (and the logging RichHandler,
# so live displays and log records serialize onto one stream).
console = Console()

# A row is a (key, value) pair; callers may also pass a mapping.
Row = Tuple[str, str]
Rows = Union[Mapping[str, Any], Sequence[Row]]


class Verbosity(str, Enum):
    """Shared verbosity policy for every command in the CLI."""

    QUIET = "quiet"
    NORMAL = "normal"
    VERBOSE = "verbose"
    DEBUG = "debug"

    @classmethod
    def from_flags(
        cls, *, quiet: bool = False, verbose: bool = False, debug: bool = False
    ) -> "Verbosity":
        """Resolve a verbosity from mutually-exclusive CLI flags."""
        if quiet:
            return cls.QUIET
        if debug:
            return cls.DEBUG
        if verbose:
            return cls.VERBOSE
        return cls.NORMAL

    @property
    def shows_detail(self) -> bool:
        """True when expanded notes/diagnostics should be shown."""
        return self in (Verbosity.VERBOSE, Verbosity.DEBUG)

    @property
    def is_quiet(self) -> bool:
        return self is Verbosity.QUIET


_ACTIVE_VERBOSITY = Verbosity.NORMAL


def set_verbosity(verbosity: Verbosity) -> None:
    """Set the process-wide verbosity used by default across the CLI."""
    global _ACTIVE_VERBOSITY
    _ACTIVE_VERBOSITY = verbosity


def get_verbosity() -> Verbosity:
    """Get the process-wide verbosity."""
    return _ACTIVE_VERBOSITY


@dataclass(frozen=True)
class UITheme:
    """Centralized styling tokens for terminal UX consistency.

    Nothing else in the CLI should hardcode a color, icon, or divider width.
    """

    # Colors
    accent: str = "cyan"
    success: str = "green"
    warning: str = "yellow"
    danger: str = "red"
    muted: str = "dim"
    cost: str = "magenta"
    panel_border: str = "bright_blue"
    table_key: str = "bright_cyan"
    table_value: str = "white"

    # Icons (single source so iconography is consistent across commands)
    icon_ok: str = "✓"
    icon_warn: str = "⚠"
    icon_err: str = "✗"
    icon_info: str = "ℹ"
    icon_arrow: str = "→"
    icon_bullet: str = "•"

    # Layout
    rule_width: int = 72

    def color_for(self, level: str) -> str:
        return {
            "success": self.success,
            "warning": self.warning,
            "error": self.danger,
            "info": self.accent,
        }.get(level, self.accent)

    def icon_for(self, level: str) -> str:
        return {
            "success": self.icon_ok,
            "warning": self.icon_warn,
            "error": self.icon_err,
            "info": self.icon_info,
        }.get(level, self.icon_bullet)


def _as_rows(data: Rows) -> List[Row]:
    """Normalize a mapping or sequence of pairs into a list of string rows."""
    if isinstance(data, Mapping):
        return [(str(k), str(v)) for k, v in data.items()]
    return [(str(k), str(v)) for k, v in data]


class TerminalUI:
    """Reusable terminal UI renderer with a cohesive visual language.

    Builder methods (``key_values``, ``events_panel``) return Rich renderables.
    Flow methods (``header``, ``section``, ``status_item``, ``next_steps``,
    ``outputs``, ``runtime_config_error``, ``message``) print to the console.
    """

    def __init__(self, console_instance: Console, theme: UITheme | None = None):
        self.console = console_instance
        self.theme = theme or UITheme()

    # -- flow: structure ---------------------------------------------------

    def header(self, title: str) -> None:
        self.console.print()
        self.console.print(
            Panel.fit(
                f"[bold]{title}[/bold]",
                border_style=self.theme.panel_border,
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )
        self.console.print()

    def section(self, title: str) -> None:
        """Print a subsection heading (replaces ad-hoc bold text + dividers)."""
        self.console.print()
        self.console.print(f"[bold]{title}[/bold]")

    def rule(self) -> None:
        """Print the one canonical horizontal divider."""
        self.console.print(
            f"[{self.theme.muted}]{'─' * self.theme.rule_width}[/{self.theme.muted}]"
        )

    # -- flow: messages ----------------------------------------------------

    def message(
        self,
        *,
        level: str,
        message: str,
        details: str | None = None,
        suggestions: List[str] | None = None,
        compact: bool = False,
    ) -> None:
        color = self.theme.color_for(level)
        icon = self.theme.icon_for(level)
        label = {
            "error": "Error",
            "warning": "Warning",
            "success": "Success",
            "info": "Info",
        }.get(level, "Message")

        if compact and not details and not suggestions:
            self.console.print(f"[{color}]{icon}[/{color}] {message}")
            return

        content = f"[{color}]{icon}[/{color}] [bold]{label}:[/bold] {message}"
        if details:
            content += f"\n[{self.theme.muted}]{details}[/{self.theme.muted}]"
        if suggestions:
            content += "\n\n[bold]Suggestions:[/bold]"
            for suggestion in suggestions:
                content += f"\n  {self.theme.icon_bullet} {suggestion}"

        border_color = color if level != "info" else self.theme.panel_border
        self.console.print(
            Panel(
                content,
                border_style=border_color,
                box=box.ROUNDED,
                padding=(0, 1),
                expand=False,
            )
        )

    def status_item(
        self, level: str, text: str, detail: str | None = None
    ) -> None:
        """Print a single per-item status row (✓ / ⚠ / ✗ + text)."""
        color = self.theme.color_for(level)
        icon = self.theme.icon_for(level)
        line = f"  [{color}]{icon}[/{color}] {text}"
        if detail:
            line += f" [{self.theme.muted}]{detail}[/{self.theme.muted}]"
        self.console.print(line)

    def detail(self, text: str) -> None:
        """Print an indented, muted secondary line (sub-detail of an item)."""
        self.console.print(f"    [{self.theme.muted}]{text}[/{self.theme.muted}]")

    # -- builders: key/value data -----------------------------------------

    def key_values(
        self,
        data: Rows,
        *,
        title: str | None = None,
        as_panel: bool = False,
        border: str | None = None,
    ) -> RenderableType:
        """The one key/value renderer for config, metrics, and summaries.

        Args:
            data: mapping or sequence of ``(key, value)`` pairs.
            title: optional title (shown above the table, or as a panel title).
            as_panel: wrap the table in a bordered panel.
            border: panel border color (defaults to the theme accent).
        """
        table = Table(
            title=None if as_panel else title,
            show_header=False,
            box=None,
            padding=(0, 2),
        )
        table.add_column("Key", style=self.theme.table_key, no_wrap=True)
        table.add_column("Value", style=self.theme.table_value)
        for key, value in _as_rows(data):
            table.add_row(key, value)

        if as_panel:
            return Panel(
                table,
                title=title,
                border_style=border or self.theme.accent,
                box=box.ROUNDED,
            )
        return table

    def events_panel(
        self, events: Iterable[str], title: str = "Recent Events"
    ) -> Panel:
        event_table = Table(show_header=False, box=None, padding=(0, 1))
        event_table.add_column(title, style=self.theme.muted)
        for event in events:
            event_table.add_row(event)
        return Panel(
            event_table,
            title=title,
            border_style=self.theme.panel_border,
            box=box.ROUNDED,
        )

    # -- flow: outcome & follow-up ----------------------------------------

    def outputs(self, mapping: Rows, title: str = "Output") -> None:
        """Print the canonical 'what was produced and where' block."""
        self.console.print()
        self.console.print(
            f"[{self.theme.success}]{self.theme.icon_ok}[/{self.theme.success}] "
            f"[bold]{title}[/bold]"
        )
        for label, location in _as_rows(mapping):
            self.console.print(
                f"  [{self.theme.table_key}]{label}[/{self.theme.table_key}]: "
                f"[{self.theme.muted}]{location}[/{self.theme.muted}]"
            )

    def next_steps(self, steps: Sequence[str], title: str = "Next Steps") -> None:
        """Print the shared follow-up hint block.

        Steps are soft-wrapped so long, copy-pasteable command hints stay on a
        single line instead of being hard-wrapped mid-command.
        """
        if not steps:
            return
        self.console.print()
        self.console.print(f"[bold]{title}[/bold]")
        for idx, step in enumerate(steps, start=1):
            self.console.print(
                f"  [{self.theme.accent}]{idx}.[/{self.theme.accent}] {step}",
                soft_wrap=True,
            )

    def runtime_config_error(
        self, exc: Exception, warnings: Sequence[str] | None = None
    ) -> None:
        """Print the standardized runtime-config resolution failure block."""
        self.message(
            level="error",
            message="Runtime config resolution failed",
            details=str(exc),
        )
        for warning in warnings or []:
            self.message(level="warning", message=str(warning), compact=True)


# Module-level renderer and thin functional wrappers ----------------------

ui = TerminalUI(console)


def print_header(title: str) -> None:
    ui.header(title)


def section(title: str) -> None:
    ui.section(title)


def rule() -> None:
    ui.rule()


def print_error(
    message: str, details: str = None, suggestions: List[str] = None
) -> None:
    ui.message(
        level="error", message=message, details=details, suggestions=suggestions
    )


def print_warning(message: str, details: str = None) -> None:
    ui.message(level="warning", message=message, details=details)


def print_success(message: str) -> None:
    ui.message(level="success", message=message, compact=True)


def print_info(message: str) -> None:
    ui.message(level="info", message=message, compact=True)


def status_item(level: str, text: str, detail: str = None) -> None:
    ui.status_item(level, text, detail)


def key_values(
    data: Rows,
    *,
    title: str = None,
    as_panel: bool = False,
    border: str = None,
) -> RenderableType:
    return ui.key_values(data, title=title, as_panel=as_panel, border=border)


def events_panel(events: Iterable[str], title: str = "Recent Events") -> Panel:
    return ui.events_panel(events, title=title)


def print_outputs(mapping: Rows, title: str = "Output") -> None:
    ui.outputs(mapping, title=title)


def print_next_steps(steps: Sequence[str], title: str = "Next Steps") -> None:
    ui.next_steps(steps, title=title)


def print_runtime_config_error(
    exc: Exception, warnings: Sequence[str] = None
) -> None:
    ui.runtime_config_error(exc, warnings)


def create_extraction_progress() -> Progress:
    """Create the standard progress bar for per-document extraction."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TextColumn(ui.theme.icon_bullet),
        TimeElapsedColumn(),
        TextColumn(ui.theme.icon_bullet),
        TimeRemainingColumn(),
        console=console,
    )


def display_json(data: dict, title: str = None) -> None:
    """Display JSON data with syntax highlighting."""
    import json

    json_str = json.dumps(data, indent=2)
    syntax = Syntax(json_str, "json", theme="monokai", line_numbers=False)
    if title:
        console.print(Panel(syntax, title=title, border_style=ui.theme.panel_border))
    else:
        console.print(syntax)


def display_yaml(data: dict, title: str = None) -> None:
    """Display YAML data with syntax highlighting."""
    import yaml

    yaml_str = yaml.dump(data, default_flow_style=False)
    syntax = Syntax(yaml_str, "yaml", theme="monokai", line_numbers=False)
    if title:
        console.print(Panel(syntax, title=title, border_style=ui.theme.panel_border))
    else:
        console.print(syntax)


def ask_choice(prompt: str, choices: List[str], default: str = None) -> str:
    """Ask the user to select from a list of choices."""
    return Prompt.ask(prompt, choices=choices, default=default)


def ask_confirm(prompt: str, default: bool = False) -> bool:
    """Ask the user for yes/no confirmation."""
    return Confirm.ask(prompt, default=default)


def ask_text(prompt: str, default: str = None) -> str:
    """Ask the user for free-text input."""
    return Prompt.ask(prompt, default=default)


def create_file_tree(root_path: Path, title: str = None) -> Tree:
    """Create a visual tree representation of a directory."""
    tree = Tree(f"📁 [bold]{title or root_path.name}[/bold]", guide_style="dim")

    def add_items(
        parent_tree: Tree,
        path: Path,
        max_depth: int = 2,
        current_depth: int = 0,
    ) -> None:
        if current_depth >= max_depth:
            return
        try:
            items = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name))
            for item in items[:20]:
                if item.name.startswith("."):
                    continue
                if item.is_dir():
                    branch = parent_tree.add(f"📁 [bold]{item.name}[/bold]")
                    add_items(branch, item, max_depth, current_depth + 1)
                else:
                    parent_tree.add(f"📄 {item.name}")
        except PermissionError:
            parent_tree.add("[dim]Permission denied[/dim]")

    add_items(tree, root_path)
    return tree


def print_cost_estimate(
    total_docs: int,
    estimated_tokens: int,
    estimated_cost: float,
    estimated_time: float,
    model: str = "gpt-4o-mini",
) -> None:
    """Print a formatted cost estimation panel."""
    rows = [
        ("Total documents", str(total_docs)),
        ("Estimated tokens", f"~{estimated_tokens:,}"),
        ("Model", model),
        ("Estimated cost", f"[{ui.theme.cost}]${estimated_cost:.2f}[/{ui.theme.cost}]"),
        ("Processing time", f"~{estimated_time:.1f} min"),
    ]
    console.print()
    console.print(
        ui.key_values(rows, title="Cost Estimation", as_panel=True, border=ui.theme.accent)
    )
    console.print()
