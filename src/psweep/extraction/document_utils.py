"""Universal document text extraction for multiple file formats."""

from html.parser import HTMLParser
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Directory name (a sibling of each document) holding cached extracted text so
# expensive work — OCR on scanned PDFs in particular — is done once and reused
# by every downstream stage (review, extraction) instead of being redone.
TEXT_CACHE_DIRNAME = ".text"


def _text_cache_paths(file_path: Path) -> tuple[Path, Path]:
    """Return (text_path, meta_path) for a document's cached extraction."""
    cache_dir = file_path.parent / TEXT_CACHE_DIRNAME
    return (
        cache_dir / f"{file_path.stem}.txt",
        cache_dir / f"{file_path.stem}.meta.json",
    )


def read_text_cache(file_path: Path) -> Optional[str]:
    """Return cached full-document text if present and still valid, else None.

    Validity is keyed on the source file's size and mtime, so a re-downloaded or
    edited document is re-extracted rather than served stale.
    """
    txt_path, meta_path = _text_cache_paths(file_path)
    if not txt_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        stat = file_path.stat()
        if int(meta.get("source_size", -1)) != stat.st_size:
            return None
        if int(meta.get("source_mtime_ns", -1)) != stat.st_mtime_ns:
            return None
        return txt_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 - a bad cache must never break extraction
        return None


def write_text_cache(file_path: Path, text: str, method: str = "ocr") -> None:
    """Persist expensively-extracted ``text`` next to ``file_path``.

    Best-effort. Only the OCR path is cached (native-text documents are cheap to
    re-extract and are left uncached so a later table-aware extraction is never
    served a flattened copy); ``method`` is recorded for transparency.
    """
    txt_path, meta_path = _text_cache_paths(file_path)
    try:
        stat = file_path.stat()
        txt_path.parent.mkdir(parents=True, exist_ok=True)
        txt_path.write_text(text, encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {
                    "source_size": stat.st_size,
                    "source_mtime_ns": stat.st_mtime_ns,
                    "chars": len(text),
                    "method": method,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - caching is an optimization, never fatal
        pass

# Supported file extensions
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".txt",
    ".xlsx",
    ".csv",
    ".doc",
    ".html",
    ".htm",
}


class _HTMLTextExtractor(HTMLParser):
    """Convert HTML content into readable plain text.

    Handles tables by preserving column structure with tab separators,
    and filters out navigation elements (nav) that add noise without
    useful content. Footer and header are preserved as block elements
    since they can contain legal citations, dates, or ordinance metadata.
    """

    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "ul",
    }
    # Tags whose content is skipped entirely.
    _SKIP_TAGS = {"script", "style", "noscript", "nav"}

    # Table cell tags get tab-separated within a row instead of newlines,
    # preserving column association (e.g., "AG\t500\tfeet").
    _TABLE_CELL_TAGS = {"td", "th"}
    # Table row tag — ends with a newline (row boundary).
    _TABLE_ROW_TAG = "tr"

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_table_cell = False

    def handle_starttag(self, tag: str, attrs) -> None:
        normalized = tag.lower()
        if normalized in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if normalized in self._TABLE_CELL_TAGS:
            # Tab separator between cells (column delimiter)
            if self._in_table_cell:
                self._chunks.append("\t")
            self._in_table_cell = True
        elif normalized == self._TABLE_ROW_TAG:
            # Newline at row start
            self._chunks.append("\n")
            self._in_table_cell = False
        elif normalized in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if normalized in self._TABLE_CELL_TAGS:
            pass  # No newline after cell — tab separator handles it
        elif normalized == self._TABLE_ROW_TAG:
            self._chunks.append("\n")
            self._in_table_cell = False
        elif normalized in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        text = data.strip()
        if text:
            self._chunks.append(text)

    def get_text(self) -> str:
        lines: list[str] = []
        current_parts: list[str] = []
        for chunk in self._chunks:
            if chunk == "\n":
                if current_parts:
                    lines.append(" ".join(current_parts))
                    current_parts = []
                elif lines and lines[-1] != "":
                    lines.append("")
                continue
            if chunk == "\t":
                # Preserve tab as column separator
                if current_parts:
                    current_parts.append("\t")
                continue
            current_parts.append(chunk)

        if current_parts:
            lines.append(" ".join(current_parts))

        collapsed: list[str] = []
        for line in lines:
            # Preserve tabs within lines (table columns)
            parts = line.split("\t")
            cleaned = "\t".join(" ".join(p.split()) for p in parts)
            if cleaned.strip():
                collapsed.append(cleaned)
            elif collapsed and collapsed[-1] != "":
                collapsed.append("")
        return "\n".join(collapsed).strip()


def is_supported_document(file_path: Path) -> bool:
    """
    Check if a file is a supported document format.

    Args:
        file_path: Path to the file

    Returns:
        True if file format is supported
    """
    return file_path.suffix.lower() in SUPPORTED_EXTENSIONS


def extract_text_from_document(
    file_path: Path, page_range: Optional[tuple] = None
) -> str:
    """
    Extract text from any supported document format.

    Supports: PDF, DOCX, TXT, XLSX, CSV, DOC, HTML

    Args:
        file_path: Path to the document
        page_range: Optional tuple (start_page, end_page) for PDF files only (1-indexed)

    Returns:
        Extracted text content

    Raises:
        ValueError: If file format is not supported
        RuntimeError: If extraction fails
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = file_path.suffix.lower()

    # Reuse cached full-document text (OCR is expensive; do it once). Only the
    # whole-document extraction is cached — page-range requests bypass it so a
    # partial slice never poisons the full-text cache other stages depend on.
    if page_range is None:
        cached = read_text_cache(file_path)
        if cached is not None:
            logger.debug(f"Text cache hit for {file_path.name}")
            return cached

    # Only the expensive OCR path is cached. Native-text PDFs and structured
    # formats (DOCX/XLSX/CSV/HTML) are cheap to re-extract, and caching a
    # flattened copy would defeat any richer/table-aware extraction a later
    # stage might want — so they always extract fresh from the source.
    used_ocr = False
    if ext == ".pdf":
        text, meta = _extract_from_pdf(file_path, page_range)
        used_ocr = bool(meta.get("used_ocr"))
    elif ext in {".docx", ".doc"}:
        text = _extract_from_docx(file_path)
    elif ext == ".txt":
        text = _extract_from_txt(file_path)
    elif ext == ".xlsx":
        text = _extract_from_xlsx(file_path)
    elif ext == ".csv":
        text = _extract_from_csv(file_path)
    elif ext in {".html", ".htm"}:
        text = _extract_from_html(file_path)
    else:
        raise ValueError(
            f"Unsupported file format: {ext}. "
            f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    # Cache OCR results (page-range requests never cache — see above).
    if page_range is None and used_ocr and text:
        write_text_cache(file_path, text)
    return text


def _extract_from_pdf(
    pdf_path: Path, page_range: Optional[tuple] = None
) -> tuple[str, dict]:
    """Extract PDF text; return ``(text, {"used_ocr": bool})``."""
    from .pdf_utils import extract_text_from_pdf

    return extract_text_from_pdf(
        pdf_path, page_range=page_range, return_meta=True
    )


def _extract_from_docx(docx_path: Path) -> str:
    """
    Extract text from DOCX file.

    Args:
        docx_path: Path to DOCX file

    Returns:
        Extracted text
    """
    try:
        import docx
    except ImportError:
        raise RuntimeError(
            "python-docx not installed. Install with: pip install python-docx"
        )

    try:
        doc = docx.Document(str(docx_path))
        text_parts = []

        # Extract text from paragraphs
        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text)

        # Extract text from tables
        for table in doc.tables:
            for row in table.rows:
                row_text = []
                for cell in row.cells:
                    if cell.text.strip():
                        row_text.append(cell.text.strip())
                if row_text:
                    text_parts.append(" | ".join(row_text))

        text = "\n".join(text_parts)
        logger.info(
            f"✓ Extracted {len(text):,} characters from DOCX: {docx_path.name}"
        )
        return text

    except Exception as e:
        raise RuntimeError(
            f"Failed to extract from DOCX {docx_path.name}: {e}"
        )


def _extract_from_txt(txt_path: Path) -> str:
    """
    Extract text from TXT file.

    Args:
        txt_path: Path to TXT file

    Returns:
        File contents
    """
    try:
        # Try UTF-8 first, fall back to other encodings
        encodings = ["utf-8", "latin-1", "cp1252"]

        for encoding in encodings:
            try:
                with open(txt_path, "r", encoding=encoding) as f:
                    text = f.read()
                logger.info(
                    f"✓ Extracted {len(text):,} characters from TXT: {txt_path.name}"
                )
                return text
            except UnicodeDecodeError:
                continue

        raise RuntimeError(
            "Could not decode text file with any supported encoding"
        )

    except Exception as e:
        raise RuntimeError(f"Failed to extract from TXT {txt_path.name}: {e}")


def _extract_from_xlsx(xlsx_path: Path) -> str:
    """
    Extract text from Excel XLSX file.

    Args:
        xlsx_path: Path to XLSX file

    Returns:
        Formatted text with sheet names and cell values
    """
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError(
            "openpyxl not installed. Install with: pip install openpyxl"
        )

    try:
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        text_parts = []

        for sheet_name in wb.sheetnames:
            sheet = wb[sheet_name]
            text_parts.append(f"\n### Sheet: {sheet_name} ###\n")

            # Extract all rows
            for row in sheet.iter_rows(values_only=True):
                row_values = [
                    str(cell) if cell is not None else "" for cell in row
                ]
                # Skip completely empty rows
                if any(v.strip() for v in row_values):
                    text_parts.append(" | ".join(row_values))

        text = "\n".join(text_parts)
        logger.info(
            f"✓ Extracted {len(text):,} characters from XLSX: {xlsx_path.name}"
        )
        return text

    except Exception as e:
        raise RuntimeError(
            f"Failed to extract from XLSX {xlsx_path.name}: {e}"
        )


def _extract_from_csv(csv_path: Path) -> str:
    """
    Extract text from CSV file.

    Args:
        csv_path: Path to CSV file

    Returns:
        Formatted text with CSV contents
    """
    try:
        import csv

        text_parts = []

        # Try different encodings
        encodings = ["utf-8", "latin-1", "cp1252"]

        for encoding in encodings:
            try:
                with open(csv_path, "r", encoding=encoding, newline="") as f:
                    reader = csv.reader(f)
                    for row in reader:
                        # Skip empty rows
                        if any(cell.strip() for cell in row):
                            text_parts.append(" | ".join(row))
                break
            except UnicodeDecodeError:
                continue

        if not text_parts:
            raise RuntimeError(
                "Could not decode CSV file with any supported encoding"
            )

        text = "\n".join(text_parts)
        logger.info(
            f"✓ Extracted {len(text):,} characters from CSV: {csv_path.name}"
        )
        return text

    except Exception as e:
        raise RuntimeError(f"Failed to extract from CSV {csv_path.name}: {e}")


def _extract_from_html(html_path: Path) -> str:
    """Extract readable text from HTML/HTM files.

    Detects JavaScript-rendered single-page application (SPA) shells that contain
    no meaningful server-side content (e.g., Angular, React, Vue apps like
    municode.com, ecode360.com) and returns empty text so downstream quality
    checks can flag or skip them.
    """
    try:
        raw_html = html_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            raw_html = html_path.read_text(encoding="latin-1")
        except Exception as e:
            raise RuntimeError(
                f"Failed to decode HTML {html_path.name}: {e}"
            ) from e
    except Exception as e:
        raise RuntimeError(f"Failed to read HTML {html_path.name}: {e}") from e

    # Detect SPA/JS-framework shells before spending effort on parsing.
    # These files contain framework bootstrap code but no server-rendered content.
    if _is_js_rendered_shell(raw_html):
        logger.warning(
            f"⚠ HTML file appears to be a JavaScript-rendered shell "
            f"(no server-side content): {html_path.name}. "
            f"Content requires browser rendering to extract."
        )
        return ""

    try:
        parser = _HTMLTextExtractor()
        parser.feed(raw_html)
        text = parser.get_text()
        logger.info(
            f"✓ Extracted {len(text):,} characters from HTML: {html_path.name}"
        )
        return text
    except Exception as e:
        raise RuntimeError(
            f"Failed to extract from HTML {html_path.name}: {e}"
        ) from e


# Indicators that an HTML file is a JS-rendered SPA shell with no server-side content.
_SPA_FRAMEWORK_INDICATORS = [
    "ng-app=",          # Angular 1.x
    "ng-strict-di=",    # Angular 1.x strict mode
    "__NEXT_DATA__",    # Next.js
    "id=\"__next\"",    # Next.js root
    "id=\"root\"",      # React CRA default
    "id=\"app\"",       # Vue.js default
    "data-reactroot",   # React
    "window.__NUXT__",  # Nuxt.js
]

# Minimum meaningful text threshold for HTML documents. SPA shells typically
# produce < 200 chars of framework boilerplate after tag stripping.
_HTML_MIN_MEANINGFUL_CHARS = 200


def _is_js_rendered_shell(raw_html: str) -> bool:
    """Detect if an HTML document is a JavaScript SPA shell with no content.

    Returns True if the HTML contains SPA framework indicators AND produces
    very little meaningful text when tags are stripped — indicating the real
    content is loaded dynamically by JavaScript.
    """
    html_lower = raw_html.lower()

    # Check for SPA framework indicators
    has_framework = any(
        indicator.lower() in html_lower for indicator in _SPA_FRAMEWORK_INDICATORS
    )
    if not has_framework:
        return False

    # Quick extraction to see if there's meaningful content
    try:
        parser = _HTMLTextExtractor()
        parser.feed(raw_html)
        text = parser.get_text()
    except Exception:
        return False

    # If a framework is detected AND text is very short, it's a shell
    return len(text.strip()) < _HTML_MIN_MEANINGFUL_CHARS
