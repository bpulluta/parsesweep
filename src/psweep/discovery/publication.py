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
import uuid
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path, PurePosixPath
from collections.abc import Iterator, Mapping

from psweep.extraction.document_utils import text_cache_paths
from psweep.utils.file_io import atomic_write, atomic_json, publication_lock, PublicationLockError

from .retry import is_retryable

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


DocumentOwner = tuple[str, str]


def owner_key(target_id: object, source_url: object) -> str | None:
    """Checkpoint key of a target or shared source; owns its documents."""
    if target_id:
        return str(target_id)
    if source_url:
        return f"source-{hashlib.md5(str(source_url).encode()).hexdigest()[:12]}"
    return None


def document_owner(row: Mapping[str, object]) -> DocumentOwner:
    """Owner of a published document: its target or shared source, and URL."""
    return (
        owner_key(row.get("target_id"), row.get("source_url")) or "",
        str(row.get("url") or ""),
    )


def _owner_name(path: str, owner: DocumentOwner, *, suffixed: bool) -> str:
    """The plain path an owner asked for, or its owner-hash variant."""
    tag = "-" + hashlib.sha256("\0".join(owner).encode()).hexdigest()[:8]
    name = PurePosixPath(path)
    stem = name.stem.removesuffix(tag)
    return name.with_name(f"{stem}{tag if suffixed else ''}{name.suffix}").as_posix()


def assign_document_names(
    records: list[dict[str, object]], index: Path, replaced: set[Path]
) -> None:
    """Give every downloaded record one stable, readable consolidated path.

    Call under the publication lock so ``index`` is current. Each record's
    ``relative_path`` holds the plain name it asks for. An owner keeps the
    path it holds in ``index`` while it asks for the same plain name;
    otherwise it gets the plain name unless another owner holds it, then a
    short owner hash (publication withdraws the owner's old path). Curated
    records claim first, so failed or unselected downloads never push a
    published document to a suffixed name. Index rows in ``replaced``
    partitions (``--fresh``) are ignored.
    """
    held: dict[str, DocumentOwner] = {}
    owned: dict[DocumentOwner, str] = {}
    for row in _csv_read(index)[1] if index.exists() else ():
        path = row["relative_path"]
        if _under(Path(path).parent, replaced):
            continue
        owner = document_owner(row)
        held[path] = owner
        owned[owner] = path
    review_ran = _review_ran(records)
    named = sorted(
        (record for record in records if record.get("relative_path")),
        key=lambda record: not record_is_curated(record, review_ran=review_ran),
    )
    for record in named:
        owner = document_owner(record)
        plain = _owner_name(str(record["relative_path"]), owner, suffixed=False)
        path = owned.get(owner)
        if path is None or _owner_name(path, owner, suffixed=False) != plain:
            free = held.get(plain, owner) == owner
            path = plain if free else _owner_name(plain, owner, suffixed=True)
            held[path] = owner
            owned[owner] = path
        record["relative_path"] = path


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
    try:
        with publication_lock(root / ".discovery-publication.lock"):
            _recover(root)
            yield
    except PublicationLockError as exc:
        raise PublicationError(str(exc)) from exc


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
    _prune_empty_parents(
        (root / "curated").absolute(),
        [Path(e["destination"]) for e in entries if not e["staged"]],
    )


def _prune_empty_parents(base: Path, removed: list[Path]) -> None:
    for path in removed:
        for parent in path.parents:
            if base not in parent.parents:
                break
            try:
                parent.rmdir()
            except OSError:
                break


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


def _review_ran(records: list[dict[str, object]]) -> bool:
    return any(record.get("review_selected") is not None for record in records)


def _withdrawn_owners(records: list[dict[str, object]]) -> set[DocumentOwner]:
    """Owners whose document this run finally did not select."""
    review_ran = _review_ran(records)
    return {
        document_owner(record)
        for record in records
        if not is_retryable(record)
        and not record_is_curated(record, review_ran=review_ran)
    }


def _stage_curated(
    documents_dir: Path, records: list[dict[str, object]], destination: Path
) -> int:
    destination.mkdir()
    review_ran = _review_ran(records)
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
        if not source.is_relative_to(documents_dir.resolve()) or not source.is_file():
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
    withdrawn: set[DocumentOwner] = frozenset(),
    completed: set[str] = frozenset(),
    is_blocked=lambda _partition: False,
    run_content: str | None = None,
) -> tuple[str, set[str]]:
    """Merge run provenance; return the index and the withdrawn paths removed.

    A path is only replaced or withdrawn by its owner (``document_owner``),
    and each owner keeps at most one path. Outside blocked partitions an
    unpromoted row is withdrawn when its owner was promoted elsewhere, is in
    ``withdrawn``, or belongs to a ``completed`` target or source.
    """
    fieldnames, existing = (
        _csv_read(domain_index) if domain_index.exists() else ([], [])
    )
    if run_content is None:
        incoming_fields, incoming = _csv_read(run_index)
    else:
        reader = csv.DictReader(io.StringIO(run_content))
        incoming_fields, incoming = list(reader.fieldnames or ()), list(reader)
    fieldnames = list(dict.fromkeys([*fieldnames, *incoming_fields, "path"]))
    promoted: dict[str, dict] = {}
    for row in incoming:
        key = row.get("relative_path", "")
        if key not in promoted_paths:
            continue
        _relative(key)
        if key in promoted:
            msg = f"Duplicate run index path: {key}"
            raise PublicationError(msg)
        promoted[key] = {
            **row,
            "path": (domain_index.parent / "curated" / key).as_posix(),
        }
    if promoted.keys() != promoted_paths:
        missing = sorted(promoted_paths - promoted.keys())
        msg = f"Missing provenance for curated paths: {missing}"
        raise PublicationError(msg)
    promoted_owners = {document_owner(row) for row in promoted.values()}
    rows: dict[str, dict] = {}
    removed: set[str] = set()
    for row in existing:
        key = row.get("relative_path", "")
        rel = _relative(key)
        if _under(rel.parent, replaced_partitions):
            continue
        owner = document_owner(row)
        if (
            key not in promoted
            and not is_blocked(rel.parent)
            and (
                owner in promoted_owners
                or owner in withdrawn
                or owner[0] in completed
            )
        ):
            removed.add(key)
            continue
        if key in rows:
            msg = f"Duplicate consolidated index path: {key}"
            raise PublicationError(msg)
        if key in promoted and owner != document_owner(promoted[key]):
            msg = f"Curated path is owned by another target or source: {key}"
            raise PublicationError(msg)
        rows[key] = row
    rows.update(promoted)
    return csv_content(fieldnames, list(rows.values())), removed


def _promotion_files(source: Path, is_blocked) -> tuple[list, set]:
    files: list[tuple[Path, Path, Path]] = []
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
        files.append((src, rel, partition))
        if not any(part.startswith(".") for part in rel.parts):
            promoted.add(rel.as_posix())
    return files, promoted


def _stage_promotion(
    *,
    source: Path,
    root: Path,
    staging: Path,
    attempted_partitions,
    blocked_partitions,
    run_index: Path | None,
    run_content: str | None = None,
    withdrawn: set[DocumentOwner] = frozenset(),
    completed: set[str] = frozenset(),
) -> list[tuple[Path, Path | None]]:
    """Stage a run's publication into consolidated curation.

    ``attempted_partitions`` (``--fresh`` runs) are replaced wholesale; any
    other selected document is published file by file. Outside blocked
    partitions, the consolidated documents of ``withdrawn`` owners, of
    ``completed`` targets/sources not promoted now, and the old paths of
    renamed owners are removed (see ``_merged_index``), so documents of
    other targets sharing a partition are kept.
    """
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

    files, promoted = _promotion_files(source, is_blocked)
    attempted = {
        _relative(part, partition=True)
        for part in (attempted_partitions or ())
    }
    replacements = _minimal(
        {part for part in attempted if not is_blocked(part)}
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
    for i, (_, rel, partition) in enumerate(files):
        if _under(partition, replacements):
            continue
        stage_file = staging / f"file-{i}"
        (staged_root / rel).replace(stage_file)
        swaps.append((root / "curated" / rel, stage_file))
    if run_index is not None:
        content, removed = _merged_index(
            run_index=run_index,
            domain_index=root / "download_index.csv",
            promoted_paths=promoted,
            replaced_partitions=replacements,
            withdrawn=withdrawn,
            completed=completed,
            is_blocked=is_blocked,
            run_content=run_content,
        )
        for key in sorted(removed):
            document = root / "curated" / key
            for path in (document, *text_cache_paths(document)):
                if _exists(path):
                    swaps.append((path, None))
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
        content, _ = _merged_index(
            run_index=run_index,
            domain_index=domain_index,
            promoted_paths=promoted_paths,
            replaced_partitions=replaced_partitions,
        )
        atomic_write(domain_index, content)


def publish_manual_curation(
    *,
    paths: PublicationPaths,
    download_records: list[dict[str, object]],
    run_index: Path,
    run_index_content: str,
    metadata_updates: Mapping[Path, str],
    attempted_partitions=None,
    blocked_partitions=None,
    completed: set[str] = frozenset(),
    _lock_held: bool = False,
) -> tuple[Path, int]:
    """Publish local/consolidated human curation and its provenance
    together; ``completed`` targets/sources reconcile their documents.
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
            swaps = _stage_promotion(
                source=local,
                root=root,
                staging=staging,
                attempted_partitions=attempted_partitions,
                blocked_partitions=blocked_partitions,
                run_index=run_index,
                run_content=run_index_content,
                withdrawn=_withdrawn_owners(download_records),
                completed=completed,
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

    ``targets`` and ``sources`` are inputs checkpointed per item, so
    adding one does not invalidate completed work.
    """
    excluded = {
        "targets",
        "sources",
        "progress_callback",
        "fresh",
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
