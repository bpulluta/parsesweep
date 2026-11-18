"""PDF text extraction with native parsers and scoped OCR fallback."""

import contextlib
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import pymupdf

    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False
    pymupdf = None

try:
    from pypdf import PdfReader

    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False
    PdfReader = None

from psweep.utils.file_io import staged_file
from psweep.utils.page_range import (
    PageRanges,
    format_page_ranges,
    normalize_page_ranges,
    ranges_from_pages,
)

logger = logging.getLogger(__name__)
_MIN_NATIVE_CHARS = 500  # a mostly-raster page with less text than this needs OCR
_RASTER_PAGE_FRACTION = 0.5  # image coverage that makes a page raster-dominated
_VISUAL_FRACTION = 0.05  # below this, images are decoration (logos), not content
NATIVE, OCR, BLANK, UNREADABLE = "native", "ocr", "blank", "unreadable"
REPRESENTED = frozenset({NATIVE, OCR, BLANK})
# Names the raw-OCR page cache format and rendering; bump it when OCR output changes.
OCR_PAGE_VERSION = "ocr-page-v1"
RENDER_DPI = 144  # legible small print without oversized images


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ocr_digest(page_digests: dict[int, str]) -> str | None:
    """Identity of a set of OCR pages: one digest over ``page:text digest`` lines."""
    if not page_digests:
        return None
    lines = (f"{page}:{digest}" for page, digest in sorted(page_digests.items()))
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


class OcrPageCache:
    """Raw OCR text per original page, keyed by source digest, page and engine.

    One file per page, replaced atomically, so a page-range run, the locator
    index and a whole-document run share OCR work. Entries hold raw text only;
    configured corrections are applied by the reader. Any entry that is
    unreadable, from another source or engine version, or whose text no longer
    matches its digest is a miss.
    """

    def __init__(self, directory: Path, source_sha256: str):
        self.directory = Path(directory)
        self.source_sha256 = source_sha256

    def _path(self, page: int) -> Path:
        return self.directory / f"{page}.json"

    def _engine(self) -> str:
        return f"{OCR_PAGE_VERSION}:pymupdf-{getattr(pymupdf, 'VersionBind', '')}"

    def _read(self, path: Path, page: int) -> tuple[str, str] | None:
        """Return ``(text, text digest)`` for a valid entry, else ``None``."""
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            text = entry["text"]
            valid = (
                entry["engine"] == self._engine()
                and entry["source_sha256"] == self.source_sha256
                and entry["page"] == page
                and isinstance(text, str)
            )
            digest = _sha(text)
            if valid and entry["text_sha256"] == digest:
                return text, digest
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return None

    def get(self, page: int) -> str | None:
        """Return the cached raw OCR text of *page*, or ``None`` on a miss."""
        hit = self._read(self._path(page), page)
        return hit[0] if hit else None

    def put(self, page: int, text: str) -> None:
        """Atomically publish *page*; a failed write leaves any prior entry intact."""
        entry = {
            "engine": self._engine(),
            "source_sha256": self.source_sha256,
            "page": page,
            "text_sha256": _sha(text),
            "text": text,
        }
        destination = self._path(page)
        try:
            with staged_file(destination, json.dumps(entry).encode("utf-8")) as staged:
                staged.replace(destination)
        except OSError:
            logger.debug("OCR page cache write failed for page %s", page, exc_info=True)

    def digest(self) -> str | None:
        """Identity of every valid cached page, or ``None`` when nothing is cached."""
        found = {}
        for path in self.directory.glob("*.json") if self.directory.is_dir() else ():
            if path.stem.isdecimal() and (hit := self._read(path, int(path.stem))):
                found[int(path.stem)] = hit[1]
        return ocr_digest(found)


def _checked_ranges(page_ranges) -> PageRanges:
    """Reject malformed or empty selections before opening a parser."""
    ranges = normalize_page_ranges(page_ranges)
    if not ranges:
        message = "Empty page selection"
        raise ValueError(message)
    return ranges


def _selected_pages(page_count: int, page_ranges: PageRanges | None) -> list[int]:
    """Return the 1-indexed pages every PDF parser reads, clamped to the PDF."""
    if page_ranges is None:
        return list(range(1, page_count + 1))
    ranges = _checked_ranges(page_ranges)
    beyond = tuple(rng for rng in ranges if rng[0] > page_count)
    if beyond:
        message = f"Pages {format_page_ranges(beyond)} are beyond the {page_count}-page PDF"
        raise ValueError(message)
    return [page for start, end in ranges for page in range(start, min(end, page_count) + 1)]


def validate_pdf_scope(pdf_path: Path, page_ranges: PageRanges) -> PageRanges:
    """Check selected pages against the PDF without parsing text or starting OCR."""
    if PYMUPDF_AVAILABLE:
        with pymupdf.open(str(pdf_path)) as document:
            pages = _selected_pages(len(document), page_ranges)
    elif PYPDF_AVAILABLE:
        with Path(pdf_path).open("rb") as handle:
            pages = _selected_pages(len(PdfReader(handle).pages), page_ranges)
    else:
        message = "No PDF parser is available to validate selected pages"
        raise RuntimeError(message)
    return ranges_from_pages(pages)


@dataclass(frozen=True)
class PageRepresentation:
    """How one original PDF page is represented as evidence.

    ``native``: the embedded text layer is the evidence. ``ocr``: local OCR
    supplied the text because the page is mostly raster. ``blank``: nothing to
    read. ``unreadable``: the page has visual content no parser could turn into
    text (including a raster page whose OCR failed), so its text, if any, is
    known to be incomplete. Later steps route ``unreadable`` pages to visuals.
    """

    page: int
    status: str
    chars: int
    image_fraction: float

    def as_dict(self) -> dict:
        """Return a JSON-safe record with the original 1-indexed page number."""
        return {
            "page": self.page,
            "status": self.status,
            "chars": self.chars,
            "image_fraction": round(self.image_fraction, 3),
        }


def _scope_meta(
    page_count: int | None,
    selected: list[int],
    pages: list[PageRepresentation],
    ocr_pages: dict[int, str] | None = None,
) -> dict:
    """Describe the selected pages and which of them are actually represented."""
    extracted = [rep.page for rep in pages if rep.status in REPRESENTED]
    return {
        "used_ocr": any(rep.status == OCR for rep in pages),
        "page_count": page_count,
        "page_ranges": ranges_from_pages(selected) or None,
        "selected_pages": selected,
        "extracted_pages": extracted,
        "pages": [rep.as_dict() for rep in pages],
        "ocr_sha256": ocr_digest(ocr_pages or {}),
    }


def _join_pages(parts: list[tuple[int, str, bool]], rules: list | None = None) -> str:
    """Join page texts, marking omitted pages; configured corrections touch OCR pages only."""
    if not any(text.strip() for _, text, _ in parts):
        return ""
    chunks: list[str] = []
    previous = None
    for page, text, is_ocr in parts:
        if previous is not None and page > previous + 1:
            gap = format_page_ranges(((previous + 1, page - 1),))
            chunks.append(f"[Omitted pages: {gap}]\n")
        chunks.append((_cleanup_ocr_errors(text, rules) if is_ocr else text) + "\n")
        previous = page
    return "".join(chunks)


@contextlib.contextmanager
def _quiet_tesseract():
    """Restore stderr and close handles after OCR or failure."""
    with Path(os.devnull).open("w", encoding="utf-8") as null_device:
        old_stderr = os.dup(2)
        try:
            os.dup2(null_device.fileno(), 2)
            yield
        finally:
            try:
                os.dup2(old_stderr, 2)
            finally:
                os.close(old_stderr)


def _ocr_page(page) -> str:
    """OCR raster areas of one page, merged with its native text layer."""
    with _quiet_tesseract():
        textpage = page.get_textpage_ocr()
        return (page.get_text(textpage=textpage) if textpage else "") or ""


def _image_fraction(page) -> float:
    """Share of the page area covered by embedded raster images (rotation-safe)."""
    area = page.rect.get_area()
    covered = sum(pymupdf.Rect(info["bbox"]).get_area() for info in page.get_image_info())
    return min(1.0, covered / area) if area > 0 else 0.0


def _represent_page(page, number: int, state: dict) -> tuple[str, PageRepresentation, bool]:
    """Decide one page's evidence from that page alone, never from document totals."""
    native = (page.get_text() or "").strip()
    chars, fraction = len(native), _image_fraction(page)
    visual = fraction >= _VISUAL_FRACTION
    if chars == 0 and not visual:
        visual = bool(page.get_drawings())
    raster = fraction >= _RASTER_PAGE_FRACTION and chars < _MIN_NATIVE_CHARS
    if not (raster or (chars == 0 and visual)):
        status = NATIVE if chars else BLANK
        return native, PageRepresentation(number, status, chars, fraction), False
    cache = state.get("cache")
    recognized = cache.get(number) if cache else None
    if recognized is None:
        try:
            recognized = _ocr_page(page)
        except (AttributeError, OSError, RuntimeError, ValueError) as exc:
            if not state.get("ocr_failed"):
                logger.warning("OCR unavailable or failed for a raster page: %s", exc)
                state["ocr_failed"] = True
        else:
            if cache:
                cache.put(number, recognized)
    if recognized is None:
        recognized = ""
    else:
        state.setdefault("ocr", {})[number] = _sha(recognized)
    ocr_chars = len(recognized.strip())
    if ocr_chars > chars:
        return recognized, PageRepresentation(number, OCR, ocr_chars, fraction), True
    return native, PageRepresentation(number, UNREADABLE, chars, fraction), False


def _read_pymupdf(
    pdf_path: Path,
    page_ranges: PageRanges | None,
    rules: list | None,
    cache: OcrPageCache | None,
) -> tuple[str, dict]:
    """Read selected pages with PyMuPDF, assessing and OCR-ing each page on its own."""
    try:
        document = pymupdf.open(str(pdf_path))
    except Exception:
        logger.exception("Error opening PDF %s", pdf_path)
        return "", _scope_meta(None, [], [])
    with document:
        selected = _selected_pages(len(document), page_ranges)
        parts, reps, state = [], [], {"cache": cache}
        try:
            for number in selected:
                text, rep, is_ocr = _represent_page(document[number - 1], number, state)
                parts.append((number, text, is_ocr))
                reps.append(rep)
        except Exception:
            logger.exception("Error extracting native PDF text from %s", pdf_path)
            return "", _scope_meta(len(document), selected, [])
        text = _join_pages(parts, rules)
        return text, _scope_meta(len(document), selected, reps, state.get("ocr"))


def _read_pypdf(pdf_path: Path, page_ranges: PageRanges | None) -> tuple[str, dict]:
    """Fallback text layer only; without visual evidence an empty page is unreadable."""
    with contextlib.ExitStack() as stack:
        try:
            pages = PdfReader(stack.enter_context(pdf_path.open("rb"))).pages
        except Exception:
            logger.exception("Error opening PDF %s", pdf_path)
            return "", _scope_meta(None, [], [])
        selected = _selected_pages(len(pages), page_ranges)
        try:
            texts = [(n, (pages[n - 1].extract_text() or "").strip()) for n in selected]
        except Exception:
            logger.exception("Error extracting native PDF text from %s", pdf_path)
            return "", _scope_meta(len(pages), selected, [])
    reps = [
        PageRepresentation(n, NATIVE if text else UNREADABLE, len(text), 0.0) for n, text in texts
    ]
    text = _join_pages([(n, t, False) for n, t in texts])
    return text, _scope_meta(len(pages), selected, reps)


def extract_text_from_pdf(
    pdf_path: Path,
    page_ranges: PageRanges | None = None,
    return_meta: bool = False,
    ocr_corrections: list | None = None,
    ocr_cache: OcrPageCache | None = None,
):
    """Extract selected pages, deciding native text versus OCR per page.

    ``page_ranges`` holds 1-indexed inclusive ``(start, end)`` pairs; ``None``
    reads the whole PDF. Ranges running past the last page are clamped; invalid
    selections raise ValueError. Every parser honors the selection and marks
    pages left out between them. ``return_meta`` always returns a pair, also on
    failure. Metadata carries the clamped ``page_ranges`` and ``selected_pages``,
    total ``page_count``, a ``pages`` list of :class:`PageRepresentation` records
    keyed by original page number, and ``extracted_pages``: the pages whose
    content is represented (native, OCR or blank), never merely iterated.
    ``ocr_cache`` supplies and receives raw OCR per page; ``ocr_sha256`` is the
    digest of the OCR pages this read used. Configured corrections touch only
    OCR pages and are applied here, never stored.
    """
    pdf_path = Path(pdf_path)
    if page_ranges is not None:
        page_ranges = _checked_ranges(page_ranges)
    text, meta = "", _scope_meta(None, [], [])
    if PYMUPDF_AVAILABLE:
        text, meta = _read_pymupdf(pdf_path, page_ranges, ocr_corrections, ocr_cache)
    if not meta["extracted_pages"] and PYPDF_AVAILABLE and PdfReader is not None:
        fallback_text, fallback_meta = _read_pypdf(pdf_path, page_ranges)
        if len(fallback_meta["extracted_pages"]) > len(meta["extracted_pages"]):
            text, meta = fallback_text, fallback_meta
    return (text, meta) if return_meta else text


def extract_pages_text(
    pdf_path: Path, *, ocr: bool = False, ocr_cache: OcrPageCache | None = None
) -> list[str]:
    """Return text per page (index 0 = page 1), best-effort.

    Native text only by default (cheap discovery screening). ``ocr=True`` also
    OCRs raster pages, for the page locator's index, sharing ``ocr_cache``
    with extraction. Text is raw: corrections apply only when extraction reads.
    """
    if not PYMUPDF_AVAILABLE:
        logger.warning("PyMuPDF not available; cannot read per-page text")
        return []
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            if not ocr:
                return [page.get_text() or "" for page in doc]
            state: dict = {"cache": ocr_cache}
            return [_represent_page(page, number, state)[0] for number, page in enumerate(doc, 1)]
    except Exception:
        logger.exception("Error reading per-page text from %s", pdf_path)
        return []


@dataclass(frozen=True)
class PageImage:
    """One rendered PDF page for visual extraction (original 1-indexed number)."""

    page: int
    png: bytes
    width: int
    height: int


def render_pages(pdf_path: Path, pages: list[int]) -> list[PageImage]:
    """Render only the requested pages, in page order, at full page size.

    Pages are never cropped, resized to fit a budget, or dropped: a page that
    cannot be sent is the request policy's decision, not this renderer's.
    """
    if not PYMUPDF_AVAILABLE:
        message = "PyMuPDF is required to render PDF pages"
        raise RuntimeError(message)
    wanted = sorted(set(pages))
    with pymupdf.open(str(pdf_path)) as document:
        beyond = [page for page in wanted if page < 1 or page > document.page_count]
        if beyond:
            message = f"Pages {beyond} are beyond the {document.page_count}-page PDF"
            raise ValueError(message)
        rendered = []
        for number in wanted:
            pixmap = document[number - 1].get_pixmap(dpi=RENDER_DPI)
            rendered.append(PageImage(number, pixmap.tobytes("png"), pixmap.width, pixmap.height))
    return rendered


def _cleanup_ocr_errors(text: str, rules: list | None = None) -> str:
    """Apply ordered configured corrections to OCR text only."""
    for rule in rules or []:
        flags = re.IGNORECASE if rule.get("ignore_case") else 0
        text = re.sub(rule["pattern"], rule["replacement"], text, flags=flags)
    return text
