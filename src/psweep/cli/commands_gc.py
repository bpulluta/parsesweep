"""`discover gc` command for backlog cleanup."""

from __future__ import annotations

from pathlib import Path

import click

from psweep.cli.ui import console, print_success
from psweep.discovery.garbage_collector import GarbageCollector
from psweep.workspace import Workspace


@click.command()
@click.argument("domain", type=str)
@click.option(
    "--domain-dir",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to domain output directory (usually outputs/<domain>)",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Delete orphaned runs. Must be explicitly confirmed.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip confirmation prompt when --apply is set.",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Minimal output.",
)
def gc(
    domain: str,
    domain_dir: Path | None = None,
    apply: bool = False,
    yes: bool = False,
    quiet: bool = False,
) -> None:
    """Scan for and optionally delete orphaned discovery run folders.

    Orphaned runs are those that were interrupted before completion or whose
    targets were removed from the roster. This command is safe: it only
    deletes runs that are not referenced by any checkpoint entry.

    Usage (dry-run, no changes):
        psweep gc utility_rate_tariffs

    Usage (delete after review):
        psweep gc utility_rate_tariffs --apply --yes

    Without --apply, this command only reports what would be deleted.
    With --apply, it deletes orphaned runs and reports the cleanup.
    """
    # Resolve domain directory
    if domain_dir is None:
        workspace = Workspace(domain=domain)
        domain_dir = workspace.root
    else:
        domain_dir = domain_dir.resolve()

    if not domain_dir.exists():
        raise click.ClickException(
            f"Domain directory not found: {domain_dir}\n"
            f"Try: psweep gc {domain} --domain-dir outputs/{domain}"
        )

    # Initialize garbage collector
    gc_obj = GarbageCollector(domain_dir)

    # Generate report
    if not quiet:
        console.print(f"[bold]Scanning {domain} for orphaned runs...[/]")
    report = gc_obj.report()

    # Display report
    _print_report(report, domain, quiet)

    # Early exit if nothing to do
    if not report.orphaned_runs:
        if not quiet:
            print_success(f"No orphaned runs found in {domain}.")
        return

    # Deletion logic
    if apply:
        if not yes:
            # Ask for confirmation
            console.print()
            console.print(
                "[bold yellow]⚠️  This will delete the runs listed above.[/]\n"
                "These deletions cannot be undone.\n"
            )
            if not click.confirm(
                "Are you sure you want to delete these orphaned runs?",
                default=False,
            ):
                console.print("[dim]Cancelled.[/]")
                return

        # Perform deletion
        if not quiet:
            console.print(f"[bold]Deleting {len(report.orphaned_runs)} orphaned run(s)...[/]")

        try:
            runs_deleted, bytes_deleted = gc_obj.delete_orphaned_runs(report)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        # Report success
        console.print()
        print_success(f"Deleted {runs_deleted} orphaned run(s)")
        if bytes_deleted > 0:
            size_str = _format_bytes(bytes_deleted)
            print_success(f"Freed {size_str} of disk space")
    else:
        # Dry-run: prompt to delete
        console.print()
        if not quiet:
            console.print(
                "[dim]This was a dry-run. Use --apply to delete the orphaned runs above.[/]"
            )


def _print_report(report, domain: str, quiet: bool) -> None:
    """Print garbage collection report."""
    if quiet:
        return

    console.print()
    console.print(f"[bold]Discovery Garbage Collection Report: {domain}[/]")
    console.print()

    # Summary
    console.print("Checkpoint status:")
    console.print(f"  • Reachable run folders: {len(report.reachability.referenced_run_ids)}")
    console.print(
        f"  • Checkpoint entries: {report.reachability.total_referenced_entries} "
        f"({len(report.reachability.completed_owners)} completed, "
        f"{len(report.reachability.incomplete_owners)} incomplete)"
    )
    console.print()

    if report.orphaned_entries == 0:
        console.print("[green]✓ No orphaned runs found[/]")
        return

    # Orphaned runs
    console.print(
        f"[bold yellow]Orphaned runs (not referenced by checkpoint): {report.orphaned_entries}[/]"
    )
    console.print()

    # Size estimate
    size_str = _format_bytes(report.reclaimable_bytes)
    console.print(f"Reclaimable disk space: {size_str}")
    console.print(f"Reclaimable files: {report.reclaimable_files}")
    console.print()

    # List runs
    console.print("Orphaned run folders:")
    for run_path in report.orphaned_runs:
        console.print(f"  • {run_path.name}")
    console.print()

    # Hardlink note
    if any(len(inodes) > 1 for inodes in report.inode_map.values()):
        hardlink_count = sum(len(inodes) - 1 for inodes in report.inode_map.values())
        console.print(
            f"[dim]Note: {hardlink_count} hardlinks detected; size counts each unique file once[/]"
        )
        console.print()


def _format_bytes(num_bytes: int) -> str:
    """Format bytes as human-readable size."""
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"
