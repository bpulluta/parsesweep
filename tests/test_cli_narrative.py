"""Regression tests for CLI UI cohesion.

These lock in the design-system contract established by the UI refactor:
- the shared primitives render in a consistent, theme-driven way,
- the RunView narrative controller emits sections in the right order and
  self-gates on verbosity,
- the discovery live dashboard is bound to the shared console and dedupes
  repeated events (the fix for duplicated/flickering discover frames), and
- command source no longer carries ad-hoc colored markup / ASCII dividers.
"""

import re
from pathlib import Path

from rich.console import Console

from psweep.cli.ui import (
    TerminalUI,
    UITheme,
    Verbosity,
    key_values,
)
from psweep.cli.run_view import RunView
from psweep.cli.dashboard import (
    DiscoveryDashboard,
    create_discovery_live_dashboard,
    create_live_dashboard,
)
import psweep.cli.ui as ui_module


def _record(width: int = 100) -> Console:
    return Console(record=True, width=width, force_terminal=False)


class TestPrimitives:
    def test_key_values_dict_and_rows_equivalent_shape(self):
        assert key_values({"a": "1"}).row_count == 1
        assert key_values([("a", "1"), ("b", "2")]).row_count == 2

    def test_status_item_uses_theme_icons(self):
        console = _record()
        ui = TerminalUI(console, UITheme())
        ui.status_item("success", "done")
        ui.status_item("error", "boom", "detail")
        text = console.export_text()
        assert UITheme().icon_ok in text
        assert UITheme().icon_err in text
        assert "detail" in text

    def test_next_steps_numbered(self):
        console = _record()
        TerminalUI(console, UITheme()).next_steps(["first", "second"])
        text = console.export_text()
        assert "1." in text and "first" in text
        assert "2." in text and "second" in text

    def test_outputs_block(self):
        console = _record()
        TerminalUI(console, UITheme()).outputs({"CSV": "/tmp/a.csv"})
        text = console.export_text()
        assert "CSV" in text and "/tmp/a.csv" in text


class TestRunViewNarrative:
    def test_section_order(self):
        console = _record()
        view = RunView("demo", verbosity=Verbosity.NORMAL, console=console)
        view.header("DEMO")
        view.config({"Domain": "x"})
        view.phase("Working")
        view.summary({"Done": "1"}, title="Demo Summary")
        view.outputs({"Result": "/tmp/out"})
        view.next_steps(["do the next thing"])
        text = console.export_text()

        positions = [
            text.index("DEMO"),
            text.index("Domain"),
            text.index("Working"),
            text.index("Demo Summary"),
            text.index("Result"),
            text.index("do the next thing"),
        ]
        assert positions == sorted(positions), text

    def test_quiet_suppresses_narrative_but_not_errors(self):
        console = _record()
        view = RunView("demo", verbosity=Verbosity.QUIET, console=console)
        view.header("DEMO")
        view.config({"Domain": "x"})
        view.summary({"Done": "1"})
        view.success("ok")
        view.error("boom", "because")
        text = console.export_text()

        assert "DEMO" not in text
        assert "Domain" not in text
        assert "ok" not in text
        # Errors always surface, even in quiet mode.
        assert "boom" in text

    def test_notes_capped_under_normal_expanded_under_verbose(self):
        notes = [f"note-{i}" for i in range(10)]

        normal_console = _record()
        RunView("d", verbosity=Verbosity.NORMAL, console=normal_console).notes(notes)
        normal_text = normal_console.export_text()
        assert "more" in normal_text  # truncation hint
        assert "note-9" not in normal_text

        verbose_console = _record()
        RunView("d", verbosity=Verbosity.VERBOSE, console=verbose_console).notes(notes)
        assert "note-9" in verbose_console.export_text()

    def test_verbosity_from_flags(self):
        assert Verbosity.from_flags(quiet=True) is Verbosity.QUIET
        assert Verbosity.from_flags(debug=True) is Verbosity.DEBUG
        assert Verbosity.from_flags(verbose=True) is Verbosity.VERBOSE
        assert Verbosity.from_flags() is Verbosity.NORMAL


class TestLiveDashboard:
    def test_acquire_live_bound_to_shared_console(self):
        live, _ = create_discovery_live_dashboard(
            domain="x", mode="run", total_targets=3, seeker_enabled=True
        )
        assert live.console is ui_module.console

    def test_extraction_live_bound_to_shared_console(self):
        live, _ = create_live_dashboard(total_documents=3)
        assert live.console is ui_module.console

    def test_push_event_dedupes_consecutive_duplicates(self):
        dash = DiscoveryDashboard(
            domain="x", mode="run", total_targets=3, seeker_enabled=True
        )
        dash.push_event("seeker 1/3: searching A | query=q")
        dash.push_event("seeker 1/3: searching A | query=q")
        dash.push_event("seeker 1/3: A -> 5 candidate(s)")
        events = list(dash.recent_events)
        assert events.count("seeker 1/3: searching A | query=q") == 1


class TestNoAdHocMarkupInCommands:
    """Command modules must render through the design system, not raw markup."""

    _COLOR = re.compile(r'console\.print\([^)]*\[(bold )?(red|green|yellow|cyan|magenta|dim)\]')
    _DIVIDER = re.compile(r'"[─=-]" \* \d+')

    def _src(self, name: str) -> str:
        root = Path(__file__).resolve().parents[1]
        return (root / "src" / "psweep" / "cli" / name).read_text()

    def test_commands_have_no_colored_console_print(self):
        assert not self._COLOR.search(self._src("commands.py"))

    def test_utils_commands_have_no_colored_console_print(self):
        assert not self._COLOR.search(self._src("utils_commands.py"))

    def test_no_ascii_dividers(self):
        assert not self._DIVIDER.search(self._src("commands.py"))
        assert not self._DIVIDER.search(self._src("utils_commands.py"))
