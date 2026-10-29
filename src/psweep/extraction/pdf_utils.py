"""PDF text extraction with native parsers and scoped OCR fallback."""

import contextlib
import logging
import os
import re
from pathlib import Path
from typing import Optional

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

logger = logging.getLogger(__name__)
_MIN_NATIVE_CHARS = 500


def _validate_page_range(page_range: tuple) -> None:
    """Reject malformed or reversed bounds before opening a parser."""
    message = "page_range must contain ordered integer page bounds"
    if not isinstance(page_range, (tuple, list)):
        raise TypeError(message)
    try:
        start, end = page_range
    except ValueError as exc:
        raise ValueError(message) from exc
    if type(start) is not int or type(end) is not int or start > end:
        raise ValueError(message)


def _selected_pages(page_count: int, page_range: Optional[tuple]) -> range:
    """Return one bounded selection for every PDF parser."""
    if page_range is None:
        return range(page_count)
    _validate_page_range(page_range)
    start, end = max(1, page_range[0]), min(page_count, page_range[1])
    if start > end:
        message = f"page_range does not intersect the {page_count}-page PDF"
        raise ValueError(message)
    return range(start - 1, end)


def validate_pdf_scope(pdf_path: Path, page_range: tuple) -> tuple[int, int]:
    """Check actual PDF bounds without parsing text or starting OCR."""
    if PYMUPDF_AVAILABLE:
        with pymupdf.open(str(pdf_path)) as document:
            pages = _selected_pages(len(document), page_range)
    elif PYPDF_AVAILABLE:
        with Path(pdf_path).open("rb") as handle:
            pages = _selected_pages(len(PdfReader(handle).pages), page_range)
    else:
        message = "No PDF parser is available to validate selected pages"
        raise RuntimeError(message)
    return pages.start + 1, pages.stop


def _scope_meta(
    page_count: int | None,
    pages: list[int],
    used_ocr: bool = False,
) -> dict:
    """Describe actual parser coverage, including skipped OCR pages."""
    return {
        "used_ocr": used_ocr,
        "page_count": page_count,
        "page_range": (pages[0], pages[-1]) if pages else None,
        "extracted_pages": pages,
    }


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
    page_range: Optional[tuple] = None,
    return_meta: bool = False,
):
    """Extract raw OCR text from the selected pages, best-effort."""
    text, meta = "", _scope_meta(None, [])
    if not PYMUPDF_AVAILABLE:
        logger.warning("PyMuPDF not available for OCR extraction")
        return (text, meta) if return_meta else text
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception:
        logger.exception("Error opening %s for OCR", pdf_path)
        return (text, meta) if return_meta else text

    with doc:
        pages = _selected_pages(len(doc), page_range)
        extracted, parts = [], []
        logger.info("Performing OCR on %s selected pages", len(pages))
        with _quiet_tesseract():
            for page_num in pages:
                try:
                    page = doc[page_num]
                    textpage = page.get_textpage_ocr()
                    if textpage:
                        parts.append(
                            (page.get_text(textpage=textpage) or "") + "\n",
                        )
                        extracted.append(page_num + 1)
                except AttributeError:  # noqa: PERF203 - isolate per-page OCR failures
                    logger.warning("PyMuPDF OCR requires Tesseract")
                    break
                except (OSError, RuntimeError, ValueError) as exc:
                    # Keep healthy pages when one scan fails OCR.
                    logger.debug("OCR skipped page %s: %s", page_num + 1, exc)
        text = "".join(parts)
        meta = {
            **_scope_meta(len(doc), extracted, used_ocr=bool(text.strip())),
            "selected_pages": [page + 1 for page in pages],
        }
    return (text, meta) if return_meta else text


def _extract_native(
    pdf_path: Path,
    page_range: Optional[tuple],
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
            return "", _scope_meta(None, [])
        selected = _selected_pages(len(pages), page_range)
        try:
            text = "".join(
                (
                    (
                        pages[n].extract_text()
                        if use_pypdf
                        else pages[n].get_text()
                    )
                    or ""
                )
                + "\n"
                for n in selected
            )
        except Exception:
            logger.exception(
                "Error extracting native PDF text from %s", pdf_path
            )
            return "", _scope_meta(len(pages), [])
        return text, _scope_meta(len(pages), [n + 1 for n in selected])


def extract_text_from_pdf(
    pdf_path: Path,
    page_range: Optional[tuple] = None,
    return_meta: bool = False,
    ocr_corrections: Optional[list] = None,
):
    """Extract selected pages with native, pypdf, then OCR fallbacks.

    Bounds are 1-indexed and inclusive. Overlapping bounds are clamped;
    invalid scopes raise ValueError. Every parser honors the scope.
    ``return_meta`` always returns a pair, including on failure.
    Metadata describes actual ``page_range``, total ``page_count``, and
    ``extracted_pages`` (OCR can skip failed pages). OCR also returns
    ``raw_ocr_text`` for caching before applying current corrections.
    """
    pdf_path = Path(pdf_path)
    if page_range is not None:
        _validate_page_range(page_range)
    text, meta = "", _scope_meta(None, [])
    if PYMUPDF_AVAILABLE:
        text, meta = _extract_native(pdf_path, page_range)
    if not text.strip() and PYPDF_AVAILABLE and PdfReader is not None:
        fallback_text, fallback_meta = _extract_native(
            pdf_path,
            page_range,
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
            page_range,
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


def _cleanup_ocr_errors(text: str, rules: Optional[list] = None) -> str:
    """Apply ordered configured corrections to OCR text only."""
    for rule in rules or []:
        flags = re.IGNORECASE if rule.get("ignore_case") else 0
        text = re.sub(rule["pattern"], rule["replacement"], text, flags=flags)
    return text
