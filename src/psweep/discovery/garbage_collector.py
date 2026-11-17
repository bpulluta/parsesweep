"""Garbage collection for discovery backlog: safe removal of orphaned run data.

A run folder becomes orphaned when its contents are never referenced by any
checkpoint entry (completed or incomplete). This happens when runs are
interrupted before completion, or when targets are removed from the roster.

The GarbageCollector implements safe, reachability-based cleanup:
1. Scan checkpoint for all reachable files (both completed and incomplete)
2. Scan disk for all existing run folders
3. Identify orphaned folders (not referenced by any checkpoint entry)
4. Report with accurate size accounting (hardlink-aware) and verification
5. Optionally delete with explicit user confirmation
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ReachabilityReport:
    """Results of checkpoint scanning for reachable files."""

    reachable_paths: set[Path]
    incomplete_owners: set[str]
    completed_owners: set[str]
    referenced_run_ids: set[str]
    total_referenced_entries: int


@dataclass
class GarbageReport:
    """Results of garbage collection scan."""

    orphaned_runs: list[Path]
    orphaned_entries: int
    reclaimable_bytes: int
    reclaimable_files: int
    reachability: ReachabilityReport
    inode_map: dict[tuple[int, int], list[Path]]  # (dev, ino) -> [paths]


class GarbageCollector:
    """Safe garbage collection for orphaned discovery run folders."""

    def __init__(self, domain_dir: Path):
        self.domain_dir = domain_dir
        self.checkpoint_path = domain_dir / "discovered" / "checkpoint.json"
        self.runs_dir = domain_dir / "discovered" / "runs"

    def scan_checkpoint_reachability(self) -> ReachabilityReport:
        """Scan checkpoint.json for all file paths that are currently reachable.

        Returns paths referenced by both completed and incomplete entries,
        including artifacts, curated_paths, and incomplete outcomes.
        """
        reachable = set()
        incomplete_owners = set()
        completed_owners = set()
        referenced_run_ids = set()

        if not self.checkpoint_path.exists():
            return ReachabilityReport(
                reachable_paths=reachable,
                incomplete_owners=incomplete_owners,
                completed_owners=completed_owners,
                referenced_run_ids=referenced_run_ids,
                total_referenced_entries=0,
            )

        try:
            checkpoint = json.loads(self.checkpoint_path.read_text())
        except (OSError, json.JSONDecodeError):
            # Checkpoint unreadable or corrupted; assume nothing is safe to delete
            return ReachabilityReport(
                reachable_paths=reachable,
                incomplete_owners=incomplete_owners,
                completed_owners=completed_owners,
                referenced_run_ids=referenced_run_ids,
                total_referenced_entries=0,
            )

        entries = checkpoint.get("entries", {})

        for owner_id, entry in entries.items():
            # Completed entries: check artifacts and curated_paths
            if not entry.get("incomplete"):
                completed_owners.add(owner_id)
                run_id = entry.get("run_id")
                if run_id:
                    referenced_run_ids.add(run_id)

                # Artifacts (manifest/index files)
                for artifact_path_str in entry.get("artifacts", {}):
                    reachable.add(Path(artifact_path_str))

                # Curated documents (consolidated from runs/)
                for curated_path_str in entry.get("curated_paths", {}):
                    reachable.add(Path(curated_path_str))

            # Incomplete entries: check outcomes (which have paths for incomplete fetches)
            else:
                incomplete_owners.add(owner_id)
                run_id = entry.get("run_id")
                if run_id:
                    referenced_run_ids.add(run_id)

                # Incomplete targets have outcomes with paths that must be preserved
                # (they'll be reused on the next run for that target)
                for outcome in entry.get("outcomes", {}).values():
                    outcome_path_str = outcome.get("path")
                    if outcome_path_str:
                        reachable.add(Path(outcome_path_str))

        return ReachabilityReport(
            reachable_paths=reachable,
            incomplete_owners=incomplete_owners,
            completed_owners=completed_owners,
            referenced_run_ids=referenced_run_ids,
            total_referenced_entries=len(entries),
        )

    def scan_disk_runs(self) -> dict[str, Path]:
        """Scan disk for all run folders under discovered/runs/.

        Returns mapping of run_id -> run_path.
        """
        runs = {}
        if not self.runs_dir.exists():
            return runs

        for run_path in sorted(self.runs_dir.iterdir()):
            if run_path.is_dir():
                run_id = run_path.name
                runs[run_id] = run_path

        return runs

    def _build_inode_map(self, paths: list[Path]) -> dict[tuple[int, int], list[Path]]:
        """Build inode map for hardlink detection: (device, inode) -> [paths]."""
        inode_map: dict[tuple[int, int], list[Path]] = defaultdict(list)

        for path in paths:
            if not path.exists():
                continue
            try:
                stat = path.stat()
                key = (stat.st_dev, stat.st_ino)
                inode_map[key].append(path)
            except OSError:
                # Path may have been deleted or become inaccessible
                continue

        return dict(inode_map)

    def _size_with_hardlink_dedup(
        self, paths: list[Path], inode_map: dict[tuple[int, int], list[Path]]
    ) -> int:
        """Calculate total size, counting each hardlink only once.

        For each hardlink group, count the size of only the first path;
        hardlinks to the same inode don't add extra size.
        """
        total_bytes = 0
        seen_inodes = set()

        for path in paths:
            if not path.exists():
                continue

            try:
                stat = path.stat()
                inode_key = (stat.st_dev, stat.st_ino)

                # Count this inode only once
                if inode_key not in seen_inodes:
                    total_bytes += stat.st_size
                    seen_inodes.add(inode_key)
            except OSError:
                continue

        return total_bytes

    def report(self) -> GarbageReport:
        """Generate a garbage collection report identifying orphaned runs.

        Returns a report with:
        - Orphaned runs (not referenced by any checkpoint entry)
        - File and byte counts (with hardlink-aware deduplication)
        - Verification info for user review before deletion
        """
        reachability = self.scan_checkpoint_reachability()
        disk_runs = self.scan_disk_runs()

        # Find orphaned runs: on disk but not referenced
        orphaned_runs = [
            run_path
            for run_id, run_path in sorted(disk_runs.items())
            if run_id not in reachability.referenced_run_ids
        ]

        # Collect all files in orphaned runs
        orphaned_files = []
        for run_path in orphaned_runs:
            for file_path in sorted(run_path.rglob("*")):
                if file_path.is_file():
                    orphaned_files.append(file_path)

        # Build inode map for hardlink detection
        inode_map = self._build_inode_map(orphaned_files)

        # Calculate reclaimable size with hardlink deduplication
        reclaimable_bytes = self._size_with_hardlink_dedup(orphaned_files, inode_map)

        return GarbageReport(
            orphaned_runs=orphaned_runs,
            orphaned_entries=len(orphaned_runs),
            reclaimable_bytes=reclaimable_bytes,
            reclaimable_files=len(orphaned_files),
            reachability=reachability,
            inode_map=inode_map,
        )

    def delete_orphaned_runs(self, report: GarbageReport) -> tuple[int, int]:
        """Delete orphaned run folders. Must re-verify reachability first.

        Returns (runs_deleted, bytes_deleted).
        Raises ValueError if reachability has changed since report generation.
        """
        import shutil

        # Re-verify reachability hasn't changed
        current_reachability = self.scan_checkpoint_reachability()
        if (
            current_reachability.reachable_paths != report.reachability.reachable_paths
            or current_reachability.referenced_run_ids
            != report.reachability.referenced_run_ids
        ):
            raise ValueError(
                "Reachability has changed since report generation. "
                "Re-run scan and review before deleting."
            )

        runs_deleted = 0
        bytes_deleted = 0

        for run_path in report.orphaned_runs:
            try:
                # Calculate actual size before deletion
                for file_path in run_path.rglob("*"):
                    if file_path.is_file():
                        try:
                            bytes_deleted += file_path.stat().st_size
                        except OSError:
                            pass

                shutil.rmtree(run_path, ignore_errors=False)
                runs_deleted += 1
            except (OSError, shutil.Error) as exc:
                raise ValueError(f"Failed to delete {run_path}: {exc}") from exc

        return runs_deleted, bytes_deleted
