"""PDF extraction utilities using LLMs."""

import logging
from pathlib import Path
from typing import Optional

try:
    import pymupdf4llm  # PyMuPDF4LLM - optimized for LLMs with table preservation

    PYMUPDF4LLM_AVAILABLE = True
except ImportError:
    PYMUPDF4LLM_AVAILABLE = False
    pymupdf4llm = None

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


def _validate_extraction_quality(text: str, pdf_path: Path) -> bool:
    """
    Validate extraction quality to detect truncation or corruption.

    Heuristics:

    1. Minimum character threshold (multi-page documents should have substantial text)
    2. Content-to-page ratio check (detect image-heavy PDFs with poor OCR)

    Parameters
    ----------
    text : str
        Extracted text
    pdf_path : Path
        Path to PDF for metadata

    Returns
    -------
    bool
        True if extraction quality is acceptable, False otherwise
    """
    # Check 1: Minimum length threshold
    # Most multi-page documents have at least 1000 chars/page
    # 3000 chars is conservative minimum for reasonable content
    if len(text) < 3000:
        logger.debug(
            f"Extraction too short: {len(text)} chars (expected >3000)"
        )
        return False

    # Check 2: Page count vs content ratio
    # If PDF has many pages but very little text, OCR likely failed
    try:
        if PYMUPDF_AVAILABLE:
            with pymupdf.open(str(pdf_path)) as doc:
                page_count = len(doc)
                chars_per_page = (
                    len(text) / page_count if page_count > 0 else 0
                )
                # Warn if average is less than 100 chars/page (likely images without OCR)
                if page_count > 5 and chars_per_page < 100:
                    logger.debug(
                        f"Low content density: {chars_per_page:.0f} chars/page for {page_count} pages"
                    )
                    return False
    except Exception:
        pass  # Skip check if can't open PDF

    return True


def extract_text_from_pdf(
    pdf_path: Path,
    prefer_markdown: bool = False,
    page_range: Optional[tuple] = None,
    return_meta: bool = False,
):
    """
    Extract text from a PDF file with adaptive method selection.

    Strategy:

    1. Try PyMuPDF4LLM for markdown/table structure (if prefer_markdown=True)
    2. Validate extraction quality (content length, key terms)
    3. Fall back to PyMuPDF if quality check fails
    4. Ultimate fallback to pypdf

    This ensures optimal extraction method is used based on PDF characteristics.

    Parameters
    ----------
    pdf_path : Path
        Path to the PDF file
    prefer_markdown : bool
        If True, try PyMuPDF4LLM first for table preservation
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

    # Strategy 1: Try PyMuPDF4LLM first if markdown preferred (for table-heavy docs)
    if prefer_markdown and PYMUPDF4LLM_AVAILABLE:
        try:
            text = pymupdf4llm.to_markdown(str(pdf_path))
            logger.debug(f"Extracted using PyMuPDF4LLM from {pdf_path.name}")

            # Quality check: PyMuPDF4LLM has known truncation issues
            # If extracted text is suspiciously short, fall back to PyMuPDF
            if not _validate_extraction_quality(text, pdf_path):
                logger.warning(
                    f"PyMuPDF4LLM extraction quality low for {pdf_path.name}, falling back to PyMuPDF"
                )
                text = ""
        except Exception as e:
            logger.error(
                f"Error extracting text with PyMuPDF4LLM from {pdf_path}: {e}"
            )
            text = ""

    # Strategy 2: Use PyMuPDF as primary/fallback - most reliable
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

    # Strategy 3: Final fallback to pypdf (when PyMuPDF is unavailable or yielded
    # no text). PyMuPDF4LLM was already attempted in Strategy 1 when requested, so
    # re-invoking it here would be redundant.
    if not text and PYPDF_AVAILABLE:
        if PdfReader is None:
            logger.error(
                "No PDF extraction library available. Install PyMuPDF4LLM, PyMuPDF or pypdf."
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

    # Strategy 4: OCR fallback for image-based PDFs
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
        text = _cleanup_ocr_errors(text)

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


def _cleanup_ocr_errors(text: str) -> str:
    """
    Fix common OCR errors using scalable rule-based approach.

    Strategy:

    1. General pattern rules (O→C, I→l, ^→/, CamelCase splitting)
    2. Context-aware fixes (units, symbols, abbreviations)
    3. Small domain dictionary for exceptions

    This scales to new states/documents without hardcoding every variant.

    Parameters
    ----------
    text : str
        Raw extracted text

    Returns
    -------
    str
        Text with common OCR errors corrected
    """
    import re

    # ========================================================================
    # RULE 1: Capital O → C at word start (common OCR error in scanned docs)
    # ========================================================================
    # Pattern: Oarbon → Carbon, Oompounds → Compounds, Oaterpillar → Caterpillar
    # Scalable: Works for any capitalized word starting with O followed by vowel
    text = re.sub(r"\bO([aeiou][a-z]+)", r"C\1", text)

    # ========================================================================
    # RULE 2: Split compound words (CamelCase → Separated Words)
    # ========================================================================
    # Pattern: SulfurDioxide → Sulfur Dioxide, CarbonMonoxide → Carbon Monoxide
    # Scalable: Works for any CamelCase technical terms (2+ capitalized words)
    text = re.sub(
        r"\b([A-Z][a-z]+)([A-Z][a-z]+(?:[A-Z][a-z]+)*)\b", r"\1 \2", text
    )

    # ========================================================================
    # RULE 3: Symbol substitutions (OCR misreads special characters)
    # ========================================================================
    # ^ → / in units (tons^yr → tons/yr)
    text = re.sub(r"(lbs|tons)\^(hr|yr)", r"\1/\2", text)
    # ^ → 2 in chemical formulas (NO^ → NO2, SO^ → SO2)
    text = re.sub(r"([A-Z]{1,2})\^", r"\g<1>2", text)

    # ========================================================================
    # RULE 4: Letter/Number confusion
    # ========================================================================
    # I → l in common units (Ibs → lbs)
    text = re.sub(r"\bIbs\b", "lbs", text)
    # O → 0 in codes/designations (PM-IO → PM-10)
    text = re.sub(r"PM-I([O0])", "PM-10", text)
    text = re.sub(r"PM-([O0])", "PM-0", text)

    # ========================================================================
    # RULE 5: Letter confusion in common abbreviations
    # ========================================================================
    # b → h in time units (lbs/br → lbs/hr)
    text = re.sub(r"(lbs|tons)/br\b", r"\1/hr", text)

    # ========================================================================
    # RULE 6: Spacing fixes (remove run-together words)
    # ========================================================================
    spacing_rules = [
        (
            r"Caterpillardiesel",
            "Caterpillar diesel",
        ),  # Specific manufacturer+type
        (r"hoursperyear", "hours per year"),
        (r"peryear", "per year"),
        (r"perhour", "per hour"),
        (r"dieselpowered", "diesel powered"),
        (r"diesel-powered", "diesel powered"),
        (r"gaspowered", "gas powered"),
        (r"gas-powered", "gas powered"),
        (r"Emissionsfrom", "Emissions from"),
        (r"Emissionsto", "Emissions to"),
    ]

    for pattern, replacement in spacing_rules:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    # ========================================================================
    # RULE 7: Domain dictionary (common text correction patterns)
    # ========================================================================
    # Keep small dictionary for edge cases that don't fit general rules
    domain_terms = {
        # Chemical compound fragments (double letters, special cases)
        r"Oioxide": "Dioxide",  # Oioxide not caught by Rule 1 (double-i)
        r"Oompounds": "Compounds",  # Backup for Organic Oompounds pattern
        # Special formatting for chemical formulas
        r"\(asNO2\)": "(as NO2)",  # Add space: (asNO2)→(as NO2)
        r"\(asSO2\)": "(as SO2)",  # Add space: (asSO2)→(as SO2)
    }

    for pattern, replacement in domain_terms.items():
        text = re.sub(pattern, replacement, text)

    # ========================================================================
    # RULE 8: Numeric spacing (add spaces between numbers and units)
    # ========================================================================
    # Add spaces around numeric values with units (4.20lbs/hr → 4.20 lbs/hr)
    text = re.sub(
        r"(\d+\.?\d*)([a-z]+/[a-z]+)", r"\1 \2", text, flags=re.IGNORECASE
    )

    return text


def truncate_text(text: str, max_chars: int = 50000) -> str:
    """
    Intelligently truncate text if too long.

    Keeps the beginning (70%) and end (30%) of the document
    to preserve both header information and summary sections.

    Parameters
    ----------
    text : str
        Text to truncate
    max_chars : int
        Maximum characters to keep (~12,500 tokens)

    Returns
    -------
    str
        Truncated text
    """
    if len(text) <= max_chars:
        return text

    keep_first = int(max_chars * 0.7)
    keep_last = max_chars - keep_first

    truncated = (
        text[:keep_first]
        + "\n\n[...middle section truncated...]\n\n"
        + text[-keep_last:]
    )

    logger.info(
        f"Document truncated from {len(text)} to {max_chars} characters"
    )
    return truncated
