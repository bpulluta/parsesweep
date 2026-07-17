"""Rich UI utilities for StreamlineExtract CLI."""

from typing import List, Dict, Any
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
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

# Global console instance
console = Console()


def print_header(title: str):
    """Print a formatted header."""
    console.print()
    console.print(
        Panel.fit(f"[bold]{title}[/bold]", border_style="blue", padding=(0, 1))
    )
    console.print()


def print_error(
    message: str, details: str = None, suggestions: List[str] = None
):
    """Print a user-friendly error message with optional details and suggestions."""
    content = f"[bold red]✗ Error:[/bold red] {message}"

    if details:
        content += f"\n\n[dim]{details}[/dim]"

    if suggestions:
        content += "\n\n[bold]💡 Try this:[/bold]"
        for suggestion in suggestions:
            content += f"\n  • {suggestion}"

    console.print()
    console.print(Panel(content, border_style="red", padding=(1, 2)))
    console.print()


def print_warning(message: str, details: str = None):
    """Print a user-friendly warning message."""
    content = f"[bold yellow]⚠ Warning:[/bold yellow] {message}"

    if details:
        content += f"\n\n[dim]{details}[/dim]"

    console.print()
    console.print(Panel(content, border_style="yellow", padding=(1, 2)))
    console.print()


def print_success(message: str):
    """Print a success message."""
    console.print(f"[green]✓[/green] {message}")


def print_info(message: str):
    """Print an info message."""
    console.print(f"[cyan]ℹ[/cyan] {message}")


def create_extraction_progress() -> Progress:
    """Create a progress bar for extraction operations."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
    )


def create_config_table(title: str, config: Dict[str, Any]) -> Table:
    """Create a styled configuration table."""
    display_title = f"📄 {title}" if title else ""
    table = Table(
        title=display_title, show_header=False, box=None, padding=(0, 2)
    )
    table.add_column("Key", style="dim", no_wrap=True)
    table.add_column("Value")

    for key, value in config.items():
        table.add_row(key, str(value))

    return table


def create_summary_table(title: str, stats: Dict[str, Any]) -> Table:
    """Create a styled summary table."""
    table = Table(title=f"📊 {title}", show_header=True, box=None)
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="green")

    for metric, value in stats.items():
        table.add_row(metric, str(value))

    return table


def display_json(data: dict, title: str = None):
    """Display JSON data with syntax highlighting."""
    import json

    json_str = json.dumps(data, indent=2)
    syntax = Syntax(json_str, "json", theme="monokai", line_numbers=False)

    if title:
        console.print(Panel(syntax, title=title, border_style="blue"))
    else:
        console.print(syntax)


def display_yaml(data: dict, title: str = None):
    """Display YAML data with syntax highlighting."""
    import yaml

    yaml_str = yaml.dump(data, default_flow_style=False)
    syntax = Syntax(yaml_str, "yaml", theme="monokai", line_numbers=False)

    if title:
        console.print(Panel(syntax, title=title, border_style="blue"))
    else:
        console.print(syntax)


def ask_choice(prompt: str, choices: List[str], default: str = None) -> str:
    """Ask user to select from a list of choices."""
    return Prompt.ask(prompt, choices=choices, default=default)


def ask_confirm(prompt: str, default: bool = False) -> bool:
    """Ask user for yes/no confirmation."""
    return Confirm.ask(prompt, default=default)


def ask_text(prompt: str, default: str = None) -> str:
    """Ask user for text input."""
    return Prompt.ask(prompt, default=default)


def create_file_tree(root_path: Path, title: str = None) -> Tree:
    """Create a visual tree representation of a directory."""
    tree = Tree(
        f"📁 [bold]{title or root_path.name}[/bold]", guide_style="dim"
    )

    def add_items(
        parent_tree: Tree,
        path: Path,
        max_depth: int = 2,
        current_depth: int = 0,
    ):
        if current_depth >= max_depth:
            return

        try:
            items = sorted(
                path.iterdir(), key=lambda x: (not x.is_dir(), x.name)
            )
            for item in items[:20]:  # Limit to 20 items
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
):
    """Print a formatted cost estimation."""
    content = f"""[bold]Document Analysis[/bold]
  Total documents: {total_docs}
  Estimated tokens: ~{estimated_tokens:,}

[bold]Cost Breakdown[/bold]
  Model: {model}
  Estimated cost: [magenta]${estimated_cost:.2f}[/magenta]
  
[bold]Time Estimate[/bold]
  Processing time: ~{estimated_time:.1f} minutes
"""

    console.print()
    console.print(
        Panel(
            content,
            title="💰 Cost Estimation",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()
