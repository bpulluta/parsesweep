"""Discovery-specific publication, recovery, and checkpoint evidence.

Stage first, then swap under one corpus lock. Backups outlive
every provenance write; an interrupted publication is rolled back
by the next writer.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import math
import os
import shutil
import socket
import tempfile
import uuid
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from collections.abc import Iterator, Mapping

from psweep.extraction.document_utils import text_cache_paths

if os.name == "nt":
    import msvcrt
else:
    import fcntl

TOKEN_HEX_LENGTH = 32
SHA256_HEX_LENGTH = 64
CHECKPOINT_VERSION = 2
MIN_CHECKPOINT_ARTIFACTS = 2


class PublicationError(ValueError):
    """Publication cannot safely replace the previously published
    corpus.
    """


@dataclass(frozen=True)
class PublicationPaths:
    """Resolved run and corpus publication locations."""

    documents_dir: Path
    manifest_path: Path
    domain_dir: Path
    run_curated_dir: Path
    run_index: Path


def resolve_publication_paths(
    documents_dir: Path,
    manifest_path: Path,
    domain: str,
    *,
    domain_dir: Path | None = None,
    run_curated_dir: Path | None = None,
) -> PublicationPaths:
    """Resolve explicit outputs without inferring arbitrary ancestor
    directories.
    """
    if not isinstance(domain, str) or not domain.strip():
        msg = "Publication requires a nonempty domain"
        raise PublicationError(msg)
    documents = Path(documents_dir).resolve()
    manifest = Path(manifest_path).resolve()
    run = manifest.parent
    root = (
        Path(domain_dir).resolve()
        if domain_dir is not None
        else (run.parent.parent if run.parent.name == "runs" else run)
    )
    curated = (
        Path(run_curated_dir).resolve()
        if run_curated_dir is not None
        else documents.parent / "curated"
    )
    if run_curated_dir is None and curated == root / "curated":
        curated = run / "run-curated"
    index = run / (
        "run-download_index.csv" if run == root else "download_index.csv"
    )
    return PublicationPaths(documents, manifest, root, curated, index)


def atomic_write(
    path: Path, content: str | bytes, *, prefix: str | None = None
) -> None:
    """Replace a UTF-8 or binary file after flushing its same-
    directory staging file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=prefix or f".{path.name}-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(
                content.encode("utf-8")
                if isinstance(content, str)
                else content
            )
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(
    path: Path, payload: object, *, prefix: str | None = None
) -> None:
    """Atomically publish finite JSON through a same-directory file."""
    atomic_write(
        path,
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False),
        prefix=prefix,
    )


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


@contextlib.contextmanager
def discovery_publication(domain_dir: Path) -> Iterator[None]:
    """Reject an active writer; kernel locks release automatically on
    process death.

    Never unlink the lock inode: doing so lets waiting writers
    lock different files. PID/host/token are diagnostic ownership,
    not an unsafe stale timeout.
    """
    root = Path(domain_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".discovery-publication.lock").open("a+b") as handle:
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b" ")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                msg = f"Discovery publication already active in {root}"
                raise PublicationError(msg) from exc
        else:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                msg = f"Discovery publication already active in {root}"
                raise PublicationError(msg) from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "token": uuid.uuid4().hex,
                    }
                ).encode()
            )
            handle.flush()
            os.fsync(handle.fileno())
            _recover(root)
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _journal_path(root: Path) -> Path:
    return root / ".discovery-publication.json"


def _plan_digest(entries: list[dict]) -> str:
    immutable = [
        {
            key: entry.get(key)
            for key in ("destination", "staged", "backup", "had_old")
        }
        for entry in entries
    ]
    return hashlib.sha256(
        json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_journal(root: Path, data: object) -> tuple[Path, list[dict]]:
    """Validate the entire recovery plan before changing even one
    destination.
    """
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data.get("version") != 1
        or not isinstance(data.get("state"), str)
        or data.get("state") not in {"prepared", "committed"}
    ):
        msg = "Corrupt discovery publication journal"
        raise PublicationError(msg)
    token = data.get("token")
    if (
        not isinstance(token, str)
        or len(token) != TOKEN_HEX_LENGTH
        or any(c not in "0123456789abcdef" for c in token)
    ):
        msg = "Corrupt discovery publication journal token"
        raise PublicationError(msg)
    staging = root / f".discovery-stage-{token}"
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        msg = "Corrupt discovery publication journal entries"
        raise PublicationError(msg)
    if staging.is_symlink() or (
        not staging.is_dir()
        and data["state"] == "prepared"
        and any(
            entry.get("state") != "restored"
            for entry in entries
            if isinstance(entry, dict)
        )
    ):
        msg = "Missing discovery publication staging directory"
        raise PublicationError(msg)
    _validate_entries(
        root, staging, entries, prepared=data["state"] == "prepared"
    )
    if data.get("plan_sha256") != _plan_digest(entries):
        msg = "Corrupt discovery publication plan checksum"
        raise PublicationError(msg)
    return staging, entries


def _entry_destination(
    root: Path, staging: Path, entry: dict, index: int
) -> Path:
    if (
        not isinstance(entry, dict)
        or type(entry.get("had_old")) is not bool
        or not isinstance(entry.get("state"), str)
        or entry.get("state") not in {"planned", "restoring", "restored"}
    ):
        message = "Corrupt discovery publication journal entry"
        raise PublicationError(message)
    raw = entry.get("destination")
    if (
        not isinstance(raw, str)
        or not Path(raw).is_absolute()
        or Path(raw) != Path(os.path.normpath(raw))
    ):
        message = "Corrupt discovery publication destination"
        raise PublicationError(message)
    dest = Path(raw)
    if (
        dest in {root, staging}
        or dest in root.parents
        or staging in dest.parents
        or any(part.startswith(".discovery-") for part in dest.parts)
    ):
        message = "Unsafe discovery publication destination"
        raise PublicationError(message)
    if entry.get("backup") != str(staging / f"backup-{index}") or (
        entry.get("staged") is not None
        and entry.get("staged") != str(staging / f"new-{index}")
    ):
        message = "Corrupt discovery publication staging paths"
        raise PublicationError(message)
    if (
        any(parent.is_symlink() for parent in (dest, *dest.parents))
        or Path(entry["backup"]).is_symlink()
    ):
        message = "Symlink discovery publication destination or backup"
        raise PublicationError(message)
    return dest


def _validate_backup(entry: dict, destination: Path) -> None:
    if not entry["had_old"]:
        return
    backup = Path(entry["backup"])
    staged = Path(entry["staged"]) if entry["staged"] else None
    if (
        entry["state"] == "planned"
        and not _exists(backup)
        and (
            not _exists(destination)
            or (staged is not None and not _exists(staged))
        )
    ):
        message = "Missing discovery publication backup; refusing recovery"
        raise PublicationError(message)
    if (
        entry["state"] in {"restoring", "restored"}
        and not _exists(backup)
        and not _exists(destination)
    ):
        message = "Missing restored discovery publication destination"
        raise PublicationError(message)


def _validate_entries(
    root: Path, staging: Path, entries: list[dict], *, prepared: bool
) -> None:
    destinations: set[Path] = set()
    ancestors: set[Path] = set()
    for index, entry in enumerate(entries):
        dest = _entry_destination(root, staging, entry, index)
        if dest in ancestors or _under(dest, destinations):
            message = "Overlapping discovery publication destinations"
            raise PublicationError(message)
        destinations.add(dest)
        ancestors.update(dest.parents)
        if prepared:
            _validate_backup(entry, dest)


def _recover(root: Path) -> None:
    journal = _journal_path(root)
    if not journal.exists():
        return
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = "Cannot read discovery publication journal; refusing overwrite"
        raise PublicationError(msg) from exc
    staging, entries = _validate_journal(root, data)
    if data["state"] != "committed":
        for entry in reversed(entries):
            if entry["state"] == "restored":
                continue
            dest, backup = Path(entry["destination"]), Path(entry["backup"])
            entry["state"] = "restoring"
            atomic_json(journal, data)
            if _exists(backup):
                _remove(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                backup.replace(dest)
            elif not entry["had_old"]:
                _remove(dest)
            entry["state"] = "restored"
            atomic_json(journal, data)
    if staging.exists():
        shutil.rmtree(staging)
    journal.unlink()


def _publish(
    root: Path, staging: Path, swaps: list[tuple[Path, Path | None]]
) -> None:
    """Install only this discovery publication's known directory/file
    swaps.
    """
    if not swaps:
        shutil.rmtree(staging)
        return
    entries = []
    for i, (destination, staged) in enumerate(swaps):
        canonical = staging / f"new-{i}"
        if staged is not None and staged != canonical:
            staged.replace(canonical)
        entries.append(
            {
                "destination": str(destination.absolute()),
                "staged": str(canonical) if staged is not None else None,
                "backup": str(staging / f"backup-{i}"),
                "had_old": _exists(destination),
                "state": "planned",
            }
        )
    data = {
        "version": 1,
        "token": staging.name.removeprefix(".discovery-stage-"),
        "state": "prepared",
        "entries": entries,
        "plan_sha256": _plan_digest(entries),
    }
    _validate_journal(root, data)
    journal = _journal_path(root)
    atomic_json(journal, data)
    try:
        for entry in entries:
            dest = Path(entry["destination"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            if entry["had_old"]:
                dest.replace(Path(entry["backup"]))
            if entry["staged"]:
                Path(entry["staged"]).replace(dest)
        data["state"] = "committed"
        atomic_json(journal, data)
    except BaseException:
        _recover(root)
        raise
    _recover(root)


def _new_stage(root: Path) -> Path:
    path = root / f".discovery-stage-{uuid.uuid4().hex}"
    path.mkdir()
    return path


def _relative(value: object, *, partition: bool = False) -> Path:
    text = str(value or "")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts or "\\" in text:
        msg = f"Unsafe selected relative path: {text!r}"
        raise PublicationError(msg)
    if not partition and (
        path == Path() or any(part.startswith(".") for part in path.parts)
    ):
        msg = f"Selected document is a sidecar path: {text!r}"
        raise PublicationError(msg)
    return path


def record_is_curated(
    record: Mapping[str, object], *, review_ran: bool
) -> bool:
    """Resolve the automated default with an explicit human override."""
    decision = str(record.get("human_decision") or "").strip().lower()
    if decision in {"keep", "reject"}:
        return decision == "keep"
    if record.get("review_failed"):
        return False
    return (
        bool(record.get("review_selected"))
        if review_ran
        else record.get("status") == "downloaded"
    )


def _link(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.hardlink_to(source)
    except (OSError, AttributeError):
        shutil.copy2(source, destination)


def _stage_curated(
    documents_dir: Path, records: list[dict[str, object]], destination: Path
) -> int:
    destination.mkdir()
    review_ran = any(
        "review_selected" in record and record["review_selected"] is not None
        for record in records
    )
    seen: set[Path] = set()
    count = 0
    for record in records:
        if not record_is_curated(record, review_ran=review_ran):
            continue
        rel = _relative(record.get("relative_path"))
        raw_source = record.get("path")
        if not raw_source:
            msg = f"Selected document has no source: {rel}"
            raise PublicationError(msg)
        source = Path(str(raw_source)).resolve()
        expected = (documents_dir / rel).resolve()
        if (
            source != expected
            or not source.is_relative_to(documents_dir.resolve())
            or not source.is_file()
        ):
            msg = (
                "Selected document source missing or outside documents "
                f"directory: {source}"
            )
            raise PublicationError(msg)
        if rel in seen:
            msg = f"Duplicate selected path: {rel}"
            raise PublicationError(msg)
        seen.add(rel)
        dest = destination / rel
        _link(source, dest)
        for cache, curated_cache in zip(
            text_cache_paths(source), text_cache_paths(dest)
        ):
            if cache.is_file():
                _link(cache, curated_cache)
        sidecar = source.parent / ".review" / f"{source.name}.json"
        if sidecar.is_file():
            _link(sidecar, dest.parent / ".review" / sidecar.name)
        count += 1
    return count


def materialize_curated(
    *,
    documents_dir: Path,
    download_records: list[dict[str, object]],
    curated_dir: Path | None = None,
) -> tuple[Path, int, int]:
    """Stage selected documents before replacing run-local curation."""
    curated = (
        Path(curated_dir).resolve()
        if curated_dir is not None
        else Path(documents_dir).resolve().parent / "curated"
    )
    with discovery_publication(curated.parent):
        staging = _new_stage(curated.parent)
        try:
            staged = staging / "curated"
            count = _stage_curated(
                Path(documents_dir), download_records, staged
            )
            _publish(curated.parent, staging, [(curated, staged)])
        finally:
            if staging.exists() and not _journal_path(curated.parent).exists():
                shutil.rmtree(staging)
    return curated, count, 0


def _csv_read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields_ = list(reader.fieldnames or ())
        if (
            not fields_
            or len(fields_) != len(set(fields_))
            or "relative_path" not in fields_
        ):
            msg = f"Invalid discovery index: {path}"
            raise PublicationError(msg)
        rows = list(reader)
        if any(
            None in row or any(value is None for value in row.values())
            for row in rows
        ):
            msg = f"Malformed discovery index: {path}"
            raise PublicationError(msg)
    return fields_, rows


def csv_content(fieldnames: list[str], rows: list[dict]) -> str:
    """Serialize discovery provenance rows as a complete CSV."""
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(
        handle, fieldnames=fieldnames, extrasaction="ignore"
    )
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue()


def _under(partition: Path, roots: set[Path]) -> bool:
    return any(parent in roots for parent in (partition, *partition.parents))


def _minimal(partitions: set[Path]) -> set[Path]:
    return {
        part
        for part in partitions
        if not any(parent in partitions for parent in part.parents)
    }


def _merged_index(
    *,
    run_index: Path,
    domain_index: Path,
    promoted_paths: set[str],
    replaced_partitions: set[Path],
    run_content: str | None = None,
) -> str:
    fieldnames, existing = (
        _csv_read(domain_index) if domain_index.exists() else ([], [])
    )
    if run_content is None:
        incoming_fields, incoming = _csv_read(run_index)
    else:
        reader = csv.DictReader(io.StringIO(run_content))
        incoming_fields, incoming = list(reader.fieldnames or ()), list(reader)
    fieldnames = list(dict.fromkeys([*fieldnames, *incoming_fields, "path"]))
    rows: dict[str, dict] = {}
    for row in existing:
        key = row.get("relative_path", "")
        rel = _relative(key)
        if not _under(rel.parent, replaced_partitions):
            if key in rows:
                msg = f"Duplicate consolidated index path: {key}"
                raise PublicationError(msg)
            rows[key] = row
    found: set[str] = set()
    for row in incoming:
        key = row.get("relative_path", "")
        if key not in promoted_paths:
            continue
        _relative(key)
        if key in found:
            msg = f"Duplicate run index path: {key}"
            raise PublicationError(msg)
        found.add(key)
        rows[key] = {
            **row,
            "path": (domain_index.parent / "curated" / key).as_posix(),
        }
    if found != promoted_paths:
        missing = sorted(promoted_paths - found)
        msg = f"Missing provenance for curated paths: {missing}"
        raise PublicationError(msg)
    return csv_content(fieldnames, list(rows.values()))


def _promotion_files(source: Path, is_blocked) -> tuple[list, set, set]:
    files: list[tuple[Path, Path, Path]] = []
    partitions: set[Path] = set()
    promoted: set[str] = set()
    for src in source.rglob("*"):
        if not src.is_file():
            continue
        if src.is_symlink():
            message = f"Symlink in run curation: {src}"
            raise PublicationError(message)
        rel = src.relative_to(source)
        partition = rel.parent
        while partition != Path() and partition.name.startswith("."):
            partition = partition.parent
        if is_blocked(partition):
            continue
        partitions.add(partition)
        files.append((src, rel, partition))
        if not any(part.startswith(".") for part in rel.parts):
            promoted.add(rel.as_posix())
    return files, partitions, promoted


def _stage_promotion(
    *,
    source: Path,
    root: Path,
    staging: Path,
    attempted_partitions,
    blocked_partitions,
    run_index: Path | None,
    run_content: str | None = None,
) -> list[tuple[Path, Path | None]]:
    if source.resolve() == (root / "curated").resolve():
        msg = "Run curation and consolidated curation must have distinct paths"
        raise PublicationError(msg)
    if not source.is_dir():
        msg = f"Run curated directory missing: {source}"
        raise PublicationError(msg)
    blocked = {
        _relative(part, partition=True) for part in (blocked_partitions or ())
    }
    blocked_ancestors = {parent for part in blocked for parent in part.parents}

    def is_blocked(part):
        return part in blocked_ancestors or _under(part, blocked)

    files, partitions, promoted = _promotion_files(source, is_blocked)
    attempted = {
        _relative(part, partition=True)
        for part in (attempted_partitions or ())
    }
    replacements = _minimal(
        partitions | {part for part in attempted if not is_blocked(part)}
    )
    staged_root = staging / "promoted"
    staged_root.mkdir()
    for src, rel, _ in files:
        _link(src, staged_root / rel)
    swaps: list[tuple[Path, Path | None]] = []
    # Detach nested staged partitions before they are renamed by
    # publication.
    for i, part in enumerate(sorted(replacements)):
        src = staged_root / part
        stage_part = staging / f"partition-{i}"
        if src.exists():
            src.replace(stage_part)
            swaps.append((root / "curated" / part, stage_part))
        else:
            swaps.append((root / "curated" / part, None))
    if run_index is not None:
        content = _merged_index(
            run_index=run_index,
            domain_index=root / "download_index.csv",
            promoted_paths=promoted,
            replaced_partitions=replacements,
            run_content=run_content,
        )
        staged_index = staging / "index"
        atomic_write(staged_index, content)
        swaps.append((root / "download_index.csv", staged_index))
    return swaps


def promote_curated(
    *,
    run_curated_dir: Path,
    domain_dir: Path,
    attempted_partitions=None,
    blocked_partitions=None,
    run_index: Path | None = None,
) -> None:
    """Publish complete partitions and their consolidated provenance."""
    root = Path(domain_dir).resolve()
    source = Path(run_curated_dir).resolve()
    index = (
        Path(run_index).resolve()
        if run_index is not None
        else source.parent / "download_index.csv"
    )
    if run_index is None and not index.exists():
        index = None
    with discovery_publication(root):
        staging = _new_stage(root)
        try:
            swaps = _stage_promotion(
                source=source,
                root=root,
                staging=staging,
                attempted_partitions=attempted_partitions,
                blocked_partitions=blocked_partitions,
                run_index=index,
            )
            _publish(root, staging, swaps)
        finally:
            if staging.exists() and not _journal_path(root).exists():
                shutil.rmtree(staging)


def merge_download_index(
    *,
    run_index: Path,
    domain_index: Path,
    promoted_paths: set[str],
    replaced_partitions: set[Path],
) -> None:
    """Atomically publish the index for known promoted documents."""
    with discovery_publication(domain_index.parent):
        atomic_write(
            domain_index,
            _merged_index(
                run_index=run_index,
                domain_index=domain_index,
                promoted_paths=promoted_paths,
                replaced_partitions=replaced_partitions,
            ),
        )


def publish_manual_curation(
    *,
    paths: PublicationPaths,
    download_records: list[dict[str, object]],
    run_index: Path,
    run_index_content: str,
    metadata_updates: Mapping[Path, str],
    attempted_partitions=None,
    blocked_partitions=None,
    _lock_held: bool = False,
) -> tuple[Path, int]:
    """Publish local/consolidated human curation and its provenance
    together.
    """
    root = paths.domain_dir
    if paths.run_curated_dir == root / "curated":
        msg = "Run curation and consolidated curation must have distinct paths"
        raise PublicationError(msg)
    with (
        contextlib.nullcontext() if _lock_held else discovery_publication(root)
    ):
        staging = _new_stage(root)
        try:
            local = staging / "local"
            count = _stage_curated(
                paths.documents_dir, download_records, local
            )
            # Updated review sidecars must travel with curation, not
            # stale copies.
            for path, content in metadata_updates.items():
                if path.parent.name == ".review" and path.is_relative_to(
                    paths.documents_dir
                ):
                    rel = path.relative_to(paths.documents_dir)
                    staged_sidecar = local / rel
                    if staged_sidecar.exists():
                        # Never mutate a source hardlink.
                        staged_sidecar.unlink()
                        atomic_write(staged_sidecar, content)
            swaps = _stage_promotion(
                source=local,
                root=root,
                staging=staging,
                attempted_partitions=attempted_partitions,
                blocked_partitions=blocked_partitions,
                run_index=run_index,
                run_content=run_index_content,
            )
            swaps.append((paths.run_curated_dir, local))
            updates = {**metadata_updates, run_index: run_index_content}
            for i, (path, content) in enumerate(updates.items()):
                staged = staging / f"metadata-{i}"
                atomic_write(staged, content)
                swaps.append((Path(path).resolve(), staged))
            _publish(root, staging, swaps)
        finally:
            if staging.exists() and not _journal_path(root).exists():
                shutil.rmtree(staging)
    return paths.run_curated_dir, count


def refresh_latest_pointer(
    run_dir: Path, *, domain_dir: Path | None = None
) -> None:
    """Atomically update the current-run pointer under the corpus
    lock.
    """
    run = Path(run_dir).resolve()
    root = (
        Path(domain_dir).resolve()
        if domain_dir is not None
        else (run.parent.parent if run.parent.name == "runs" else run)
    )
    with discovery_publication(root):
        temporary = root / f".latest-{uuid.uuid4().hex}"
        try:
            try:
                temporary.symlink_to(
                    os.path.relpath(run, root),
                    target_is_directory=True,
                )
            except OSError:
                atomic_write(root / "latest.txt", run.as_posix() + "\n")
            else:
                temporary.replace(root / "latest")
                (root / "latest.txt").unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)


def artifact_sha256(path: Path) -> str:
    """Hash a regular publication artifact without following a
    symlink.
    """
    if Path(path).is_symlink():
        msg = f"Checkpoint artifact is a symlink: {path}"
        raise PublicationError(msg)
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_artifacts_valid(entry: Mapping[str, object]) -> bool:
    """Validate hashed artifact evidence for a completed target."""
    artifacts, curated = entry.get("artifacts"), entry.get("curated_paths")
    if (
        not isinstance(artifacts, dict)
        or len(artifacts) < MIN_CHECKPOINT_ARTIFACTS
        or not isinstance(curated, dict)
    ):
        return False
    for evidence in (artifacts, curated):
        for raw, expected in evidence.items():
            if (
                not isinstance(raw, str)
                or not Path(raw).is_absolute()
                or not isinstance(expected, str)
                or len(expected) != SHA256_HEX_LENGTH
            ):
                return False
            try:
                if artifact_sha256(Path(raw)) != expected:
                    return False
            except (OSError, ValueError):
                return False
    return True


def _checkpoint_data(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"Corrupt checkpoint: {path}"
        raise PublicationError(msg) from exc
    if not isinstance(data, dict):
        msg = f"Corrupt checkpoint: {path}"
        raise PublicationError(msg)
    if data.get("version") == 1 and type(data.get("version")) is int:
        return {}
    if (
        data.get("version") != CHECKPOINT_VERSION
        or type(data.get("version")) is not int
    ):
        msg = f"Invalid checkpoint version: {path}"
        raise PublicationError(msg)
    entries = data.get("entries")
    if not isinstance(entries, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict)
        for key, value in entries.items()
    ):
        msg = f"Corrupt checkpoint entries: {path}"
        raise PublicationError(msg)
    return entries


def load_checkpoint(
    path: Path,
    *,
    fingerprint: str | None = None,
    validate_artifacts: bool = True,
) -> dict[str, dict[str, object]]:
    """Load matching v2 completions whose artifact evidence remains
    valid.
    """
    if fingerprint is not None and (
        not isinstance(fingerprint, str) or not fingerprint
    ):
        msg = "Checkpoint fingerprint must be a nonempty string"
        raise PublicationError(msg)
    try:
        entries = _checkpoint_data(path)
    except PublicationError:
        return {}
    return {
        key: entry
        for key, entry in entries.items()
        if (fingerprint is None or entry.get("fingerprint") == fingerprint)
        and (not validate_artifacts or checkpoint_artifacts_valid(entry))
    }


def save_checkpoint_entries(
    path: Path,
    new_entries: dict[str, dict[str, object]],
    *,
    fingerprint: str | None = None,
) -> None:
    """Atomically merge completions without overwriting corrupt
    state.
    """
    if not isinstance(new_entries, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict)
        for key, value in new_entries.items()
    ):
        msg = "Checkpoint entries must be a mapping of target keys to objects"
        raise PublicationError(msg)
    if fingerprint is not None and (
        not isinstance(fingerprint, str) or not fingerprint
    ):
        msg = "Checkpoint fingerprint must be a nonempty string"
        raise PublicationError(msg)
    with discovery_publication(Path(path).resolve().parent):
        entries = _checkpoint_data(path)
        entries.update(
            {
                key: {
                    **entry,
                    **(
                        {"fingerprint": fingerprint}
                        if fingerprint is not None
                        else {}
                    ),
                }
                for key, entry in new_entries.items()
            }
        )
        atomic_json(
            path, {"version": 2, "entries": entries}, prefix=".checkpoint-"
        )


def effective_discovery_fingerprint(request: object) -> str:
    """Fingerprint effective discovery semantics and configured
    output locations.
    """
    excluded = {
        "targets",
        "progress_callback",
        "reprocess",
        "dry_run",
        "seeker_cache",
        "seeker_cache_ttl_minutes",
    }
    if is_dataclass(request) and not isinstance(request, type):
        values = {
            field.name: getattr(request, field.name)
            for field in fields(request)
            if field.name not in excluded
        }
    elif isinstance(request, Mapping):
        values = {
            key: value for key, value in request.items() if key not in excluded
        }
    else:
        msg = "Fingerprint requires an effective discovery request"
        raise PublicationError(msg)

    def normalize(value):
        if isinstance(value, Path):
            return value.as_posix()
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        if isinstance(value, Mapping):
            if any(not isinstance(key, str) for key in value):
                msg = "Fingerprint configuration keys must be strings"
                raise PublicationError(msg)
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        kind = type(value).__name__
        msg = f"Invalid effective discovery configuration value: {kind}"
        raise PublicationError(msg)

    encoded = json.dumps(
        {"version": 2, "request": normalize(values)},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()
