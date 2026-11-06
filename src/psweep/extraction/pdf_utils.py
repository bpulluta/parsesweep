"""PDF text extraction with native parsers and scoped OCR fallback."""

import contextlib
import logging
import os
import re
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

from psweep.utils.page_range import (
    PageRanges,
    format_page_ranges,
    normalize_page_ranges,
    ranges_from_pages,
)

logger = logging.getLogger(__name__)
_MIN_NATIVE_CHARS = 500


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


def _scope_meta(
    page_count: int | None,
    selected: list[int],
    extracted: list[int],
    used_ocr: bool = False,
) -> dict:
    """Describe the selected pages and those a parser actually read."""
    return {
        "used_ocr": used_ocr,
        "page_count": page_count,
        "page_ranges": ranges_from_pages(selected) or None,
        "selected_pages": selected,
        "extracted_pages": extracted,
    }


def _join_pages(parts: list[tuple[int, str]]) -> str:
    """Join page texts, marking pages left out between them."""
    if not any(text.strip() for _, text in parts):
        return ""
    chunks, previous = [], None
    for page, text in parts:
        if previous is not None and page > previous + 1:
            gap = format_page_ranges(((previous + 1, page - 1),))
            chunks.append(f"[Omitted pages: {gap}]\n")
        chunks.append(text + "\n")
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


def _extract_with_ocr(
    pdf_path: Path,
    page_ranges: PageRanges | None = None,
    return_meta: bool = False,
):
    """Extract raw OCR text from the selected pages, best-effort."""
    text, meta = "", _scope_meta(None, [], [])
    if not PYMUPDF_AVAILABLE:
        logger.warning("PyMuPDF not available for OCR extraction")
        return (text, meta) if return_meta else text
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        logger.exception("Error opening %s for OCR", pdf_path)
        return (text, meta) if return_meta else text

    with doc:
        selected = _selected_pages(len(doc), page_ranges)
        parts = []
        logger.info("Performing OCR on %s selected pages", len(selected))
        with _quiet_tesseract():
            for page_num in selected:
                try:
                    page = doc[page_num - 1]
                    textpage = page.get_textpage_ocr()
                    if textpage:
                        parts.append((page_num, page.get_text(textpage=textpage) or ""))
                except AttributeError:
                    logger.warning("PyMuPDF OCR requires Tesseract")
                    break
                except (OSError, RuntimeError, ValueError) as exc:
                    # Keep healthy pages when one scan fails OCR.
                    logger.debug("OCR skipped page %s: %s", page_num, exc)
        text = _join_pages(parts)
        extracted = [page for page, _ in parts]
        meta = _scope_meta(len(doc), selected, extracted, used_ocr=bool(text))
    return (text, meta) if return_meta else text


def _extract_native(
    pdf_path: Path,
    page_ranges: PageRanges | None,
    *,
    use_pypdf: bool = False,
) -> tuple[str, dict]:
    """Run either parser with identical selection/failure semantics."""
    with contextlib.ExitStack() as stack:
        try:
            if use_pypdf:
                handle = stack.enter_context(pdf_path.open("rb"))
                pages = PdfReader(handle).pages
            else:
                pages = stack.enter_context(pymupdf.open(str(pdf_path)))
        except Exception:
            logger.exception("Error opening PDF %s", pdf_path)
            return "", _scope_meta(None, [], [])
        selected = _selected_pages(len(pages), page_ranges)
        read = (lambda page: page.extract_text()) if use_pypdf else (lambda page: page.get_text())
        try:
            text = _join_pages([(n, read(pages[n - 1]) or "") for n in selected])
        except Exception:
            logger.exception("Error extracting native PDF text from %s", pdf_path)
            return "", _scope_meta(len(pages), selected, [])
        return text, _scope_meta(len(pages), selected, selected)


def extract_text_from_pdf(
    pdf_path: Path,
    page_ranges: PageRanges | None = None,
    return_meta: bool = False,
    ocr_corrections: list | None = None,
):
    """Extract selected pages with native, pypdf, then OCR fallbacks.

    ``page_ranges`` holds 1-indexed inclusive ``(start, end)`` pairs; ``None``
    reads the whole PDF. Ranges running past the last page are clamped; invalid
    selections raise ValueError. Every parser honors the selection and marks
    pages left out between selected ones. ``return_meta`` always returns a pair,
    including on failure. Metadata describes the clamped ``page_ranges`` and
    ``selected_pages``, total ``page_count``, and ``extracted_pages`` (OCR can
    skip failed pages). OCR also returns ``raw_ocr_text`` for caching before
    applying current corrections.
    """
    pdf_path = Path(pdf_path)
    if page_ranges is not None:
        page_ranges = _checked_ranges(page_ranges)
    text, meta = "", _scope_meta(None, [], [])
    if PYMUPDF_AVAILABLE:
        text, meta = _extract_native(pdf_path, page_ranges)
    if not text.strip() and PYPDF_AVAILABLE and PdfReader is not None:
        fallback_text, fallback_meta = _extract_native(
            pdf_path,
            page_ranges,
            use_pypdf=True,
        )
        if fallback_meta["page_count"] is not None:
            text, meta = fallback_text, fallback_meta
    if len(text.strip()) < _MIN_NATIVE_CHARS:
        logger.warning(
            "Very little text extracted from %s; attempting scoped OCR",
            pdf_path.name,
        )
        ocr_text, ocr_meta = _extract_with_ocr(
            pdf_path,
            page_ranges,
            return_meta=True,
        )
        if ocr_text.strip() and len(ocr_text) > len(text):
            text = _cleanup_ocr_errors(ocr_text, ocr_corrections)
            meta = {**ocr_meta, "raw_ocr_text": ocr_text}
    return (text, meta) if return_meta else text


def extract_pages_text(pdf_path: Path) -> list[str]:
    """Return plain text per page (index 0 = page 1), best-effort."""
    if not PYMUPDF_AVAILABLE:
        logger.warning("PyMuPDF not available; cannot read per-page text")
        return []
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            return [page.get_text() or "" for page in doc]
    except Exception:
        logger.exception("Error reading per-page text from %s", pdf_path)
        return []


def _cleanup_ocr_errors(text: str, rules: list | None = None) -> str:
    """Apply ordered configured corrections to OCR text only."""
    for rule in rules or []:
        flags = re.IGNORECASE if rule.get("ignore_case") else 0
        text = re.sub(rule["pattern"], rule["replacement"], text, flags=flags)
    return text
