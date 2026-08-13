"""RunView: the narrative controller for CLI commands.

Every command creates one :class:`RunView` and drives it through a fixed
section contract so the whole tool tells the same story in the same order:

    header -> config -> progress (phase/live) -> summary -> notes/warnings
    -> outputs -> next steps

The RunView owns the console, verbosity, and theme, and self-gates every
section on verbosity. This centralizes what used to be scattered
``if VERBOSITY != "quiet"`` checks and guarantees consistent structure.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator, Sequence

if TYPE_CHECKING:
    from rich.progress import Progress

from rich.console import Console

from psweep.cli.ui import (
    Rows,
    TerminalUI,
    UITheme,
    Verbosity,
    console as default_console,
    get_verbosity,
)


class RunView:
    """Owns the presentation of a single command run."""

    def __init__(
        self,
        command: str,
        *,
        verbosity: Verbosity | None = None,
        console: Console | None = None,
        theme: UITheme | None = None,
    ):
        self.command = command
        self.verbosity = verbosity or get_verbosity()
        self.console = console or default_console
        self.theme = theme or UITheme()
        self.ui = TerminalUI(self.console, self.theme)

    @property
    def is_quiet(self) -> bool:
        """True when this run is operating under quiet verbosity."""
        return self.verbosity.is_quiet

    # -- context: what will run -------------------------------------------

    def header(self, title: str | None = None) -> None:
        """Print the command header (skipped in quiet mode)."""
        if self.is_quiet:
            return
        self.ui.header(title or self.command.title())

    def config(self, rows: Rows, title: str | None = "Configuration") -> None:
        """Print the resolved configuration table (skipped in quiet mode)."""
        if self.is_quiet:
            return
        self.console.print(self.ui.key_values(rows, title=title))
        self.console.print()

    def section(self, title: str) -> None:
        """A subsection heading within a command's output."""
        if not self.is_quiet:
            self.ui.section(title)

    # -- progress: what is happening now ----------------------------------

    def phase(self, title: str) -> None:
        """Announce a processing phase (replaces ad-hoc arrow lines)."""
        if self.is_quiet:
            return
        self.console.print(
            f"[{self.theme.accent}]{self.theme.icon_arrow}[/{self.theme.accent}] {title}"
        )

    @contextmanager
    def spinner(self, message: str, *, spinner: str = "dots") -> Iterator[None]:
        """Show a spinner with a message while work is in progress."""
        if self.is_quiet:
            yield
            return
        with self.console.status(
            f"[{self.theme.accent}]{message}[/{self.theme.accent}]",
            spinner=spinner,
        ):
            yield

    def make_progress(self) -> "Progress | None":
        """Return a configured Progress bar, or None in quiet mode."""
        if self.is_quiet:
            return None
        from psweep.cli.ui import create_extraction_progress
        return create_extraction_progress()

    @contextmanager
    def live(self, live_display) -> Iterator[None]:
        """Run a Rich Live display unless quiet (then it is skipped)."""
        if self.is_quiet or live_display is None:
            yield
            return
        with live_display:
            yield

    # -- messages ---------------------------------------------------------

    def info(self, message: str) -> None:
        """Print a compact info message (skipped in quiet mode)."""
        if not self.is_quiet:
            self.ui.message(level="info", message=message, compact=True)

    def success(self, message: str) -> None:
        """Print a compact success message (skipped in quiet mode)."""
        if not self.is_quiet:
            self.ui.message(level="success", message=message, compact=True)

    def warning(self, message: str, details: str | None = None) -> None:
        """Print a warning; surfaces under normal verbosity, quiet suppresses."""
        # Warnings surface even under normal verbosity; only quiet suppresses.
        if not self.is_quiet:
            self.ui.message(level="warning", message=message, details=details)

    def error(
        self,
        message: str,
        details: str | None = None,
        suggestions: Sequence[str] | None = None,
    ) -> None:
        """Print an error with optional details/suggestions; always surfaces."""
        # Errors always surface, even in quiet mode.
        self.ui.message(
            level="error",
            message=message,
            details=details,
            suggestions=list(suggestions) if suggestions else None,
        )

    def status(self, level: str, text: str, detail: str | None = None) -> None:
        """Print a per-item status row (skipped in quiet mode)."""
        if not self.is_quiet:
            self.ui.status_item(level, text, detail)

    def detail(self, text: str) -> None:
        """Indented muted secondary line under a status item."""
        if not self.is_quiet:
            self.ui.detail(text)

    def runtime_config_error(
        self, exc: Exception, warnings: Sequence[str] | None = None
    ) -> None:
        """Print a runtime-config resolution error with optional warnings."""
        self.ui.runtime_config_error(exc, warnings)

    # -- outcome: what happened -------------------------------------------

    def summary(self, rows: Rows, title: str = "Summary") -> None:
        """Print the outcome summary panel (skipped in quiet mode)."""
        if self.is_quiet:
            return
        self.console.print()
        self.console.print(
            self.ui.key_values(rows, title=title, as_panel=True, border=self.theme.accent)
        )

    def notes(
        self,
        items: Sequence[str],
        *,
        title: str = "Notes",
        normal_limit: int = 4,
    ) -> None:
        """Show notes, expanded under -v/--debug and capped under normal."""
        if self.is_quiet or not items:
            return
        limit = len(items) if self.verbosity.shows_detail else normal_limit
        shown = [f"{self.theme.icon_bullet} {n}" for n in items[:limit]]
        if len(items) > limit:
            shown.append(
                f"{self.theme.icon_bullet} ... and {len(items) - limit} more "
                f"(use -v for full detail)"
            )
        self.console.print(self.ui.events_panel(shown, title=title))

    def warnings(self, items: Sequence[str], *, title: str = "Warnings") -> None:
        """Print each warning as a status row (skipped in quiet mode)."""
        if self.is_quiet or not items:
            return
        for item in items:
            self.ui.status_item("warning", str(item))

    # -- follow-up: what to do next ---------------------------------------

    def outputs(self, mapping: Rows, title: str = "Output") -> None:
        """Print produced output paths (skipped in quiet mode)."""
        if self.is_quiet:
            return
        self.ui.outputs(mapping, title=title)

    def next_steps(self, steps: Sequence[str], title: str = "Next Steps") -> None:
        """Print suggested next steps (skipped in quiet mode)."""
        if self.is_quiet:
            return
        self.ui.next_steps(steps, title=title)
