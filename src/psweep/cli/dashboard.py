"""Live dashboard for discovery progress."""

from collections import deque
from dataclasses import dataclass
import re

from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
)

from psweep.cli.ui import console, key_values, events_panel


def _create_live_session(
    layout: Layout,
    *,
    refresh_per_second: int,
    transient: bool = False,
) -> Live:
    """Create a Rich Live session bound to the shared CLI console.

    Binding Live to the same console the logging RichHandler uses lets Rich
    serialize log records with live frames instead of two Console objects
    racing on one stdout — which is what caused duplicated/flickering frames.
    """
    return Live(
        layout,
        console=console,
        refresh_per_second=refresh_per_second,
        transient=transient,
    )


@dataclass(frozen=True)
class DiscoveryDashboardConfig:
    """Presentation settings for discovery live dashboards."""

    header_title: str = "Discovery In Progress"
    max_recent_events: int = 6
    refresh_per_second: int = 8
    transient: bool = False


class DiscoveryDashboard:
    """Compact live dashboard for discovery progress and stage telemetry."""

    _SEEKER_SEARCH_RE = re.compile(
        r"^seeker\s+(\d+)/(\d+):\s+searching\s+(.+?)\s+\|\s+query=",
        re.IGNORECASE,
    )
    _SEEKER_RESULT_RE = re.compile(
        r"^seeker\s+(\d+)/(\d+):\s+(.+?)\s+->\s+(\d+)\s+candidate",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        domain: str,
        mode: str,
        total_targets: int,
        seeker_enabled: bool,
        config: DiscoveryDashboardConfig | None = None,
    ):
        self.config = config or DiscoveryDashboardConfig()
        self.domain = domain
        self.mode = mode
        self.total_targets = total_targets
        self.seeker_enabled = seeker_enabled
        self.stage = "initializing"
        self.current_target = "(none)"
        self.targets_completed = 0
        self.candidates_seen = 0
        self.download_events = 0
        self.recent_events: deque[str] = deque(maxlen=self.config.max_recent_events)

        self.layout = Layout()
        self.layout.split_column(
            Layout(name="header", size=3),
            Layout(name="status", size=7),
            Layout(name="events", size=8),
        )

        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            TextColumn("[dim]{task.fields[detail]}[/dim]"),
        )
        self.task = self.progress.add_task(
            "Discovery",
            total=None,
            detail="bootstrapping",
        )

    def push_event(self, message: str) -> None:
        """Add an event to the panel, deduping consecutive repeats."""
        text = str(message or "").strip()
        if not text:
            return
        # Deterministic dedupe: drop consecutive identical events so repeated
        # progress lines never stack up in the recent-events panel.
        if self.recent_events and self.recent_events[0] == text:
            return
        self._ingest_progress_line(text)
        self.recent_events.appendleft(text)
        self._update_layout()

    def _ingest_progress_line(self, text: str) -> None:
        lowered = text.lower()
        if lowered.startswith("run: discovery started"):
            self.stage = "starting"
            self.progress.update(
                self.task,
                description="Discovery",
                detail="starting run",
            )
            return
        if lowered.startswith("run: seeker stage started"):
            self.stage = "discovering"
            self.progress.update(
                self.task,
                description="Seeker",
                detail="running target queries",
            )
            return
        if lowered.startswith("run: seeker stage completed"):
            self.stage = "selection"
            self.progress.update(
                self.task,
                description="Selection",
                detail="ranking and filtering candidates",
            )
            return
        if lowered.startswith("run: download stage started"):
            self.stage = "downloading"
            self.progress.update(
                self.task,
                description="Download",
                detail="fetching source documents",
            )
            return
        if lowered.startswith("run: discovery finished"):
            self.stage = "completed"
            self.progress.update(
                self.task,
                description="Completed",
                detail=text.replace("run: ", ""),
            )
            return

        seeker_search = self._SEEKER_SEARCH_RE.match(text)
        if seeker_search:
            idx = int(seeker_search.group(1))
            total = int(seeker_search.group(2))
            label = seeker_search.group(3).strip()
            self.current_target = label
            self.targets_completed = max(self.targets_completed, idx - 1)
            self.total_targets = max(self.total_targets, total)
            self.progress.update(
                self.task,
                description=f"Seeker {idx}/{total}",
                detail=label,
            )
            return

        seeker_result = self._SEEKER_RESULT_RE.match(text)
        if seeker_result:
            idx = int(seeker_result.group(1))
            total = int(seeker_result.group(2))
            label = seeker_result.group(3).strip()
            discovered = int(seeker_result.group(4))
            self.current_target = label
            self.targets_completed = max(self.targets_completed, idx)
            self.total_targets = max(self.total_targets, total)
            self.candidates_seen += discovered
            self.progress.update(
                self.task,
                description=f"Seeker {idx}/{total}",
                detail=f"{label} -> {discovered} candidate(s)",
            )
            return

        if lowered.startswith("download:"):
            self.download_events += 1
            self.progress.update(
                self.task,
                description="Download",
                detail=text.replace("download:", "", 1).strip(),
            )

    def _update_layout(self) -> None:
        self.layout["header"].update(
            Panel(
                self.progress,
                title=self.config.header_title,
                border_style="blue",
            )
        )

        status = key_values(
            [
                ("Domain", self.domain),
                ("Mode", self.mode),
                ("Stage", self.stage),
                (
                    "Targets",
                    f"{self.targets_completed}/{max(self.total_targets, 0)}",
                ),
                ("Current Target", self.current_target),
                ("Candidates Seen", str(self.candidates_seen)),
                ("Download Events", str(self.download_events)),
                (
                    "Seeker",
                    "enabled" if self.seeker_enabled else "disabled",
                ),
            ]
        )
        self.layout["status"].update(Panel(status, title="Status", border_style="cyan"))

        self.layout["events"].update(events_panel(list(self.recent_events)))

    def get_layout(self) -> Layout:
        """Return the refreshed Rich layout for the live dashboard."""
        self._update_layout()
        return self.layout


def create_discovery_live_dashboard(
    *,
    domain: str,
    mode: str,
    total_targets: int,
    seeker_enabled: bool,
    config: DiscoveryDashboardConfig | None = None,
) -> tuple[Live, DiscoveryDashboard]:
    """Create live dashboard widgets for discover command UX."""
    dashboard_config = config or DiscoveryDashboardConfig()
    dashboard = DiscoveryDashboard(
        domain=domain,
        mode=mode,
        total_targets=total_targets,
        seeker_enabled=seeker_enabled,
        config=dashboard_config,
    )
    live = _create_live_session(
        dashboard.get_layout(),
        refresh_per_second=dashboard_config.refresh_per_second,
        transient=dashboard_config.transient,
    )
    return live, dashboard
