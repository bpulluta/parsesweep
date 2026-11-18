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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from psweep.exceptions import SourceChangedError
from psweep.extraction.pdf_utils import RENDER_DPI

if TYPE_CHECKING:
    from collections.abc import Iterator

_CHUNK = 1024 * 1024
_UNCHECKED = object()
CONSUMPTION_VERSION = "captured-input-v1"


def effective_identity(metadata: dict) -> dict:
    """Compact identity of a verified text product, including representation."""
    return {key: metadata.get(key) for key in ("method", "text_sha256", "page_count")}


def visual_pages(mode: str, parser_scope: dict) -> list[int]:
    """Original page numbers to attach as images under ``extraction.input_mode``.

    ``text`` attaches none. ``auto`` attaches only pages no parser could turn
    into text, so clean documents stay text-only. ``vision`` attaches every
    meaningful (non-blank) selected page. Only PDF parsing reports pages, so
    other document types never get visuals.
    """
    if mode == "text":
        return []
    attach = {"unreadable"} if mode == "auto" else {"native", "ocr", "unreadable"}
    return [entry["page"] for entry in parser_scope.get("pages") or () if entry["status"] in attach]


def input_provenance(mode: str, parser_scope: dict, visual: list[int]) -> dict:
    """Bounded record of how the input was read: mode, per-page evidence, image use.

    Holds metadata only (never rendered bytes). ``pages`` are the selected PDF
    pages' representations; a document without a PDF parse has none.
    """
    provenance = {
        "input_mode": mode,
        "pages": [dict(entry) for entry in parser_scope.get("pages") or ()],
        "visual_pages": list(visual),
    }
    if visual:
        provenance["render_dpi"] = RENDER_DPI
    return provenance


@dataclass(frozen=True)
class CapturedText:
    """Verified raw text product frozen for one source lifetime."""

    text: str
    method: str
    sha256: str
    page_count: int | None

    @property
    def identity(self) -> dict:
        """Return compact method, content and representation proof."""
        return {
            "method": self.method,
            "text_sha256": self.sha256,
            "page_count": self.page_count,
        }


@dataclass(frozen=True)
class CapturedSource:
    """A consumed source version: snapshot ``path`` plus the ``original`` it came from."""

    original: Path
    path: Path
    sha256: str
    size: int
    mtime_ns: int
    cached_text: CapturedText | None = None
    ocr_pages: dict[int, str] = field(default_factory=dict)  # page -> raw OCR text digest

    @property
    def name(self) -> str:
        """Keep original document names in logs and worker context."""
        return self.original.name

    @property
    def identity(self) -> dict:
        """Bind retained scopes to this source and effective evidence.

        Raw OCR is derived deterministically from the source and the OCR
        engine, so it is proven by the record's per-page ``ocr_sha256`` and
        :attr:`ocr_pages`, not part of a scope's identity.
        """
        return {
            "source_sha256": self.sha256,
            "effective_text": self.cached_text.identity if self.cached_text else None,
        }


@contextlib.contextmanager
def capture_source(
    original: Path,
    *,
    expected: str | None = None,
    expected_effective=_UNCHECKED,
    transcriber=None,
) -> Iterator[CapturedSource]:
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
        # Read metadata and bytes once; never reopen the live pair after capture.
        from .document_utils import ocr_page_cache, read_text_cache
        from .transcription import accepts_transcript, is_audio

        cached = read_text_cache(original, return_meta=True, source_sha256=captured.sha256)
        if cached and is_audio(original):
            method = cached[1].get("method") or ""
            if not accepts_transcript(method, transcriber):
                cached = None
        product = None
        if cached:
            text, metadata = cached
            identity = effective_identity(metadata)
            product = CapturedText(
                text,
                identity["method"],
                identity["text_sha256"],
                identity["page_count"],
            )
        actual = product.identity if product else None
        # A derived transcript may safely appear during this run. An authoritative
        # browser/API product cannot appear or disappear under a prepared identity.
        derived = product and is_audio(original)
        mismatch = (
            expected_effective is not _UNCHECKED
            and actual != expected_effective
            and not (expected_effective is None and derived)
        )
        if mismatch:
            raise SourceChangedError(
                f"{original.name} effective text changed after extraction was prepared; "
                "rerun extraction",
                document=original.name,
            )
        captured = CapturedSource(
            original,
            snapshot,
            captured.sha256,
            snapshot.stat().st_size,
            captured.mtime_ns,
            product,
            # Raw OCR pages cached so far, each by its own text digest.
            ocr_page_cache(original, captured.sha256).entries()
            if original.suffix.lower() == ".pdf"
            else {},
        )
        yield captured
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
