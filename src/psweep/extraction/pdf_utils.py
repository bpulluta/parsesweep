"""PDF extraction utilities using LLMs."""

import logging
import re
from pathlib import Path
from typing import Optional

try:
    import pymupdf  # PyMuPDF - fallback

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


def _extract_with_ocr(pdf_path: Path) -> str:
    """
    Extract text from image-based PDF using PyMuPDF's built-in OCR (Tesseract).

    PyMuPDF 1.23+ has built-in OCR support via get_textpage_ocr().
    This is used as a fallback when normal text extraction yields insufficient text.

    Parameters
    ----------
    pdf_path : Path
        Path to the PDF file

    Returns
    -------
    str
        Extracted text via OCR
    """
    if not PYMUPDF_AVAILABLE:
        logger.warning("PyMuPDF not available for OCR extraction")
        return ""

    try:
        import os

        text = ""
        with pymupdf.open(str(pdf_path)) as doc:
            page_count = len(doc)
            logger.info(
                f"🔍 Performing OCR on {page_count} pages (image-based PDF detected)..."
            )

            # Completely suppress Tesseract stderr warnings
            # Redirect both Python stderr and system-level stderr
            null_device = open(os.devnull, "w")
            old_stderr = os.dup(2)
            os.dup2(null_device.fileno(), 2)

            try:
                for page_num in range(page_count):
                    page = doc[page_num]
                    # Try OCR with PyMuPDF (uses Tesseract if available)
                    try:
                        # get_textpage_ocr requires tesseract to be installed
                        tp = page.get_textpage_ocr()
                        if tp:
                            page_text = page.get_text(textpage=tp)
                            text += page_text + "\n"
                            logger.debug(
                                f"  OCR page {page_num + 1}/{page_count}: {len(page_text)} chars"
                            )
                    except AttributeError:
                        # Older PyMuPDF versions or Tesseract not available
                        logger.warning(
                            f"OCR not available - PyMuPDF {pymupdf.__version__} may need Tesseract installation"
                        )
                        break
                    except Exception as e:
                        # Skip pages that fail OCR (common with poor quality scans)
                        logger.debug(
                            f"OCR skipped for page {page_num + 1}: {str(e)[:100]}"
                        )
                        continue
            finally:
                # Restore system stderr
                os.dup2(old_stderr, 2)
                os.close(old_stderr)
                null_device.close()

        if text:
            logger.info(
                f"✓ OCR extraction completed: {len(text):,} characters from {page_count} pages"
            )
        else:
            logger.warning(
                "OCR extraction yielded no text - PDF may have poor quality scans"
            )

        return text

    except Exception as e:
        logger.error(f"Error during OCR extraction from {pdf_path}: {e}")
        return ""


def extract_text_from_pdf(
    pdf_path: Path,
    page_range: Optional[tuple] = None,
    return_meta: bool = False,
    ocr_corrections: Optional[list] = None,
):
    """
    Extract text from a PDF file with adaptive method selection.

    Strategy:

    1. Use PyMuPDF as the primary extractor - most reliable
    2. Fall back to pypdf when PyMuPDF is unavailable or yields no text
    3. OCR fallback (Tesseract via PyMuPDF) for image-based PDFs

    This ensures optimal extraction method is used based on PDF characteristics.

    Parameters
    ----------
    pdf_path : Path
        Path to the PDF file
    page_range : Optional[tuple]
        Optional tuple (start_page, end_page) to extract only specific pages (1-indexed)
    return_meta : bool
        If True, return ``(text, {"used_ocr": bool})`` so callers
        can decide whether the (expensive) OCR path ran — used to cache only
        OCR results and re-extract cheap native PDFs fresh.

    Returns
    -------
    str or tuple
        Extracted text string, or ``(text, meta)`` when ``return_meta`` is True.
    """
    text = ""
    used_ocr = False
    pdf_path = Path(pdf_path)

    # Strategy 1: Use PyMuPDF as primary extractor - most reliable
    if not text and PYMUPDF_AVAILABLE:
        try:
            with pymupdf.open(str(pdf_path)) as doc:
                page_count = len(doc)

                # Determine page range
                if page_range:
                    start_page, end_page = page_range
                    # Convert to 0-indexed and validate
                    start_page = max(
                        0, start_page - 1
                    )  # Convert 1-indexed to 0-indexed
                    end_page = min(
                        page_count, end_page
                    )  # Ensure within bounds
                    pages_to_extract = range(start_page, end_page)
                    logger.debug(
                        f"Extracting pages {start_page + 1}-{end_page} from {pdf_path.name}"
                    )
                else:
                    pages_to_extract = range(page_count)
                    logger.debug(
                        f"Extracting all {page_count} pages from {pdf_path.name}"
                    )

                for page_num in pages_to_extract:
                    text += doc[page_num].get_text() + "\n"

                logger.debug(
                    f"Extracted {len(pages_to_extract)} pages using PyMuPDF from {pdf_path.name}"
                )
        except Exception as e:
            logger.error(
                f"Error extracting text with PyMuPDF from {pdf_path}: {e}"
            )

    # Strategy 2: Final fallback to pypdf (when PyMuPDF is unavailable or yielded
    # no text).
    if not text and PYPDF_AVAILABLE:
        if PdfReader is None:
            logger.error(
                "No PDF extraction library available. Install PyMuPDF or pypdf."
            )
            return ""

        try:
            with open(pdf_path, "rb") as f:
                pdf_reader = PdfReader(f)
                for page_num in range(len(pdf_reader.pages)):
                    text += pdf_reader.pages[page_num].extract_text() + "\n"
            logger.debug(
                f"Extracted {len(pdf_reader.pages)} pages using pypdf from {pdf_path.name}"
            )
        except Exception as e:
            logger.error(
                f"Error extracting text with pypdf from {pdf_path}: {e}"
            )
            return ""

    # Strategy 3: OCR fallback for image-based PDFs
    # If text extraction yielded very little content, try OCR
    if (
        len(text.strip()) < 500
    ):  # Less than 500 chars indicates image-based PDF
        logger.warning(
            f"Very little text extracted from {pdf_path.name} ({len(text)} chars), attempting OCR..."
        )
        ocr_text = _extract_with_ocr(pdf_path)
        if ocr_text and len(ocr_text) > len(text):
            logger.info(
                f"✓ OCR extraction successful for {pdf_path.name}, using OCR text"
            )
            text = ocr_text
            used_ocr = True
        elif not ocr_text:
            logger.warning(
                f"⚠️ OCR extraction failed for {pdf_path.name} - PDF may be image-based without searchable text"
            )

    # Apply OCR-artifact corrections only to text that actually came from OCR.
    # These heuristics (e.g. capital-O -> C at a word start) are meant for
    # scan/OCR noise and would corrupt clean digital extractions — turning
    # "Oil" into "Cil" or "Oakmont" into "Cakmont" — so they must not run on
    # normally-extracted text.
    if used_ocr:
        text = _cleanup_ocr_errors(text, ocr_corrections)

    if return_meta:
        return text, {"used_ocr": used_ocr}
    return text


def extract_pages_text(pdf_path: Path) -> list[str]:
    """Return the plain text of each page as a list (index 0 = page 1).

    Used by page targeting to score/locate the pages that hold a target section
    without loading the whole document into the extraction context. Best-effort:
    returns an empty list if PyMuPDF is unavailable or the file can't be opened.
    """
    if not PYMUPDF_AVAILABLE:
        logger.warning("PyMuPDF not available; cannot read per-page text")
        return []
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            return [page.get_text() or "" for page in doc]
    except Exception as e:  # noqa: BLE001 - caller falls back to full extraction
        logger.error(f"Error reading per-page text from {pdf_path}: {e}")
        return []


def _cleanup_ocr_errors(text: str, rules: Optional[list] = None) -> str:
    """Apply configured OCR-artifact correction rules to OCR-derived text.

    ``rules`` is a list of ``{"pattern", "replacement", "ignore_case"?}``
    objects supplied via ``extraction.ocr_corrections`` in the run config and
    applied in order. When no rules are configured this is a no-op: OCR
    corrections are domain-specific (e.g. scanned air-emissions permits) and are
    opted into per domain rather than baked into this universal extractor.

    Parameters
    ----------
    text : str
        Raw OCR-extracted text.
    rules : list, optional
        Ordered correction rules; see ``extraction.ocr_corrections``.

    Returns
    -------
    str
        Text with the configured corrections applied (unchanged if none).
    """
    if not rules:
        return text

    for rule in rules:
        pattern = rule["pattern"]
        replacement = rule["replacement"]
        flags = re.IGNORECASE if rule.get("ignore_case") else 0
        text = re.sub(pattern, replacement, text, flags=flags)

    return text
