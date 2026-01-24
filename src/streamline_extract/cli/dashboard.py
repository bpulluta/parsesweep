"""Live dashboard for real-time extraction monitoring."""

from typing import Optional
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from streamline_extract.cli.cost_tracker import CostTracker


class ExtractionDashboard:
    """Live dashboard for monitoring document extraction progress."""
    
    def __init__(self, total_documents: int, model: str = "gpt-4o-mini"):
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
            "Processing documents...",
            total=total_documents
        )
    
    def update_header(self):
        """Update the header section."""
        self.layout["header"].update(
            Panel(
                "[bold]🔄 Extraction in Progress[/bold]",
                style="blue"
            )
        )
    
    def update_progress_section(self):
        """Update the progress bar section."""
        self.layout["progress"].update(self.progress)
    
    def update_stats(self):
        """Update the statistics table."""
        summary = self.cost_tracker.get_summary()
        
        # Create stats table
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Metric", style="cyan", no_wrap=True)
        table.add_column("Value")
        
        # Progress stats
        table.add_row("Documents", f"{self.processed}/{self.total_documents}")
        table.add_row("Successful", f"[green]{self.successful}[/green]")
        if self.failed > 0:
            table.add_row("Failed", f"[red]{self.failed}[/red]")
        
        # Cost stats
        table.add_row("", "")  # Spacer
        table.add_row("Total Cost", f"[magenta]${summary['total_cost']:.4f}[/magenta]")
        table.add_row("Avg/Doc", f"[magenta]${summary['avg_cost_per_doc']:.4f}[/magenta]")
        
        # Token stats
        table.add_row("", "")  # Spacer
        table.add_row("Tokens", f"{summary['total_tokens']:,}")
        
        # Current document
        if self.current_document:
            table.add_row("", "")  # Spacer
            table.add_row("Current", f"[dim]{self.current_document}[/dim]")
        
        self.layout["stats"].update(
            Panel(table, title="📊 Statistics", border_style="cyan")
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
    
    def complete_document(self, document_name: str, success: bool, cost: float = 0.0, 
                         input_tokens: int = 0, output_tokens: int = 0):
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
                cost=cost
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
            **self.cost_tracker.get_summary()
        }


def create_live_dashboard(total_documents: int, model: str = "gpt-4o-mini") -> tuple[Live, ExtractionDashboard]:
    """
    Create a live dashboard for extraction monitoring.
    
    Args:
        total_documents: Total number of documents to process
        model: Model name for cost tracking
    
    Returns:
        Tuple of (Live instance, ExtractionDashboard instance)
    """
    dashboard = ExtractionDashboard(total_documents, model)
    live = Live(dashboard.get_layout(), refresh_per_second=4)
    return live, dashboard
