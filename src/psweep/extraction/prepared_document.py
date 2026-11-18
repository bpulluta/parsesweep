"""Immutable identity of the source version an extraction consumes.

A path, stat, or open descriptor is not a content snapshot. Extraction copies
the consumed source into a private document-local snapshot while hashing those
same bytes, compares that digest with the identity preparation fingerprinted,
and reads only the snapshot afterwards. The original path stays provenance.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from psweep.exceptions import SourceChangedError

if TYPE_CHECKING:
    from collections.abc import Iterator

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class CapturedSource:
    """A consumed source version: snapshot ``path`` plus the ``original`` it came from."""

    original: Path
    path: Path
    sha256: str
    size: int
    mtime_ns: int


@contextlib.contextmanager
def capture_source(original: Path, *, expected: str | None = None) -> Iterator[CapturedSource]:
    """Snapshot *original* for one consumer and always remove the snapshot.

    The snapshot lives in a new owner-only directory under the system temporary
    directory, outside inventoried inputs and any output reset, and keeps the
    original file name so suffix-dependent readers behave identically. When
    *expected* is given, a digest mismatch raises :class:`SourceChangedError`
    before the snapshot is handed out.
    """
    original = Path(original)
    workdir = Path(tempfile.mkdtemp(prefix="psweep-source-"))
    try:
        digest = hashlib.sha256()
        snapshot = workdir / original.name
        with original.open("rb") as handle, snapshot.open("wb") as copy:
            stat = os.fstat(handle.fileno())
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
                copy.write(chunk)
        captured = CapturedSource(
            original=original,
            path=snapshot,
            sha256=digest.hexdigest(),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )
        if expected is not None and captured.sha256 != expected:
            message = (
                f"{original.name} changed after extraction was prepared; "
                "rerun extraction so the record describes the bytes consumed"
            )
            raise SourceChangedError(message, document=original.name)
        yield captured
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
