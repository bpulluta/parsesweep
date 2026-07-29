"""Live dashboard for real-time extraction monitoring."""

from collections import deque
from dataclasses import dataclass
import re

from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
)

from psweep.cli.cost_tracker import CostTracker
from psweep.cli.ui import console, key_values, events_panel
from psweep.extraction.llm_factory import DEFAULT_MODEL


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


class ExtractionDashboard:
    """Live dashboard for monitoring document extraction progress."""

    def __init__(self, total_documents: int, model: str = DEFAULT_MODEL):
        """
        Initialize the dashboard.

        Args:
            total_documents: Total number of documents to process
            model: Model name for cost tracking
        """
        self.total_documents = total_documents
        self.cost_tracker = CostTracker(model=model)
        self.current_document = None
        self.processed = 0
        self.successful = 0
        self.failed = 0

        # Create layout
        self.layout = Layout()
        self.layout.split_column(
            Layout(name="header", size=3),
            Layout(name="progress", size=3),
            Layout(name="stats", size=8),
        )

        # Create progress bar
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("•"),
            TimeElapsedColumn(),
        )
        self.task = self.progress.add_task(
            "Processing documents...", total=total_documents
        )

    def update_header(self):
        """Update the header section."""
        self.layout["header"].update(
            Panel("[bold]🔄 Extraction in Progress[/bold]", style="blue")
        )

    def update_progress_section(self):
        """Update the progress bar section."""
        self.layout["progress"].update(self.progress)

    def update_stats(self):
        """Update the statistics table."""
        summary = self.cost_tracker.get_summary()

        rows: list[tuple[str, str]] = [
            ("Documents", f"{self.processed}/{self.total_documents}"),
            ("Successful", f"[green]{self.successful}[/green]"),
        ]
        if self.failed > 0:
            rows.append(("Failed", f"[red]{self.failed}[/red]"))

        rows.extend(
            [
                ("", ""),
                (
                    "Total Cost",
                    f"[magenta]${summary['total_cost']:.4f}[/magenta]",
                ),
                (
                    "Avg/Doc",
                    f"[magenta]${summary['avg_cost_per_doc']:.4f}[/magenta]",
                ),
                ("", ""),
                ("Tokens", f"{summary['total_tokens']:,}"),
            ]
        )

        if self.current_document:
            rows.extend(
                [
                    ("", ""),
                    ("Current", f"[dim]{self.current_document}[/dim]"),
                ]
            )

        table = key_values(rows)

        self.layout["stats"].update(
            Panel(table, title="Statistics", border_style="cyan")
        )

    def update_display(self):
        """Update all sections of the dashboard."""
        self.update_header()
        self.update_progress_section()
        self.update_stats()

    def start_document(self, document_name: str):
        """Mark a document as being processed."""
        self.current_document = document_name
        self.update_display()

    def complete_document(
        self,
        document_name: str,
        success: bool,
        cost: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ):
        """
        Mark a document as completed.

        Args:
            document_name: Name of the document
            success: Whether processing was successful
            cost: Cost of processing
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
        """
        self.processed += 1

        if success:
            self.successful += 1
            self.cost_tracker.add_request(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                document_name=document_name,
                cost=cost,
            )
        else:
            self.failed += 1
            self.cost_tracker.add_failure()

        self.progress.update(self.task, advance=1)
        self.current_document = None
        self.update_display()

    def get_layout(self) -> Layout:
        """Get the current layout."""
        self.update_display()
        return self.layout

    def get_final_summary(self) -> dict:
        """Get the final summary statistics."""
        return {
            "processed": self.processed,
            "successful": self.successful,
            "failed": self.failed,
            **self.cost_tracker.get_summary(),
        }


def create_live_dashboard(
    total_documents: int, model: str = DEFAULT_MODEL
) -> tuple[Live, ExtractionDashboard]:
    """
    Create a live dashboard for extraction monitoring.

    Args:
        total_documents: Total number of documents to process
        model: Model name for cost tracking

    Returns:
        Tuple of (Live instance, ExtractionDashboard instance)
    """
    dashboard = ExtractionDashboard(total_documents, model)
    live = _create_live_session(
        dashboard.get_layout(), refresh_per_second=4
    )
    return live, dashboard


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
        self.recent_events: deque[str] = deque(
            maxlen=self.config.max_recent_events
        )

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
        self.layout["status"].update(
            Panel(status, title="Status", border_style="cyan")
        )

        self.layout["events"].update(events_panel(list(self.recent_events)))

    def get_layout(self) -> Layout:
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
