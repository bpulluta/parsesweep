"""Content sampling for downloaded discovery files.

Extracts text from PDF/DOCX/DOC/XLSX/CSV/TXT and validates keyword
presence for post-download document classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ContentSamplingResult:
    """Result of content sampling validation."""

    success: bool
    text_extracted: str | None
    keywords_found: list[str]
    keywords_missing: list[str]
    confidence_score: float
    sample_length: int
    reasons: list[str]
    error: str | None = None


class ContentSampler:
    """Samples and validates content from downloaded files for keyword presence.

    Supports:
    - PDF: uses pdftotext or PyMuPDF
    - DOCX/DOC: uses python-docx
    - XLSX: uses openpyxl
    - CSV: direct extraction
    - TXT: direct read
    """

    MAX_SAMPLE_CHARS = 10000  # Sample first N chars
    MIN_EXTRACTION_LENGTH = 50  # Require at least 50 chars of meaningful text

    _SAMPLE_PAGES = 5  # Pages to sample for classification/review

    @staticmethod
    def _ocr_pdf_fallback(file_path: str) -> str:
        """OCR an image-based PDF via the extraction pipeline's proven path.

        Scanned PDFs yield no embedded text, so a plain extract returns nothing
        and the document is silently dropped from review. Delegate to
        ``extract_text_from_document`` (plain extraction + Tesseract OCR
        fallback) which also writes the full text to the shared ``.text/`` cache,
        so this expensive OCR is done once and reused by the extraction stage.
        """
        try:
            from pathlib import Path

            from ..extraction.document_utils import extract_text_from_document

            return extract_text_from_document(Path(file_path)) or ""
        except Exception:  # noqa: BLE001 - OCR unavailable, caller handles empty
            return ""

    @classmethod
    def _extract_text_from_pdf(cls, file_path: str) -> str:
        """Extract text from PDF file, with OCR fallback for scanned PDFs."""
        # Prefer the shared full-text cache: if the extraction stage (or a prior
        # review) already extracted/OCR'd this file, reuse it instead of redoing.
        try:
            from pathlib import Path

            from ..extraction.document_utils import read_text_cache

            cached = read_text_cache(Path(file_path))
            if cached is not None:
                return cached
        except Exception:  # noqa: BLE001 - cache is optional
            pass

        max_pages = cls._SAMPLE_PAGES
        text = ""
        try:
            import pdftotext

            with open(file_path, "rb") as f:
                pdf = pdftotext.PDF(f)
                text = "\n".join(pdf[: min(max_pages, len(pdf))])
        except Exception as e:
            # Fallback to PyMuPDF if pdftotext fails
            try:
                import fitz

                doc = fitz.open(file_path)
                text = "".join(
                    doc[page_num].get_text() + "\n"
                    for page_num in range(min(max_pages, len(doc)))
                )
            except Exception as fallback_e:
                raise ValueError(
                    f"Failed to extract PDF text: {e}, fallback error: {fallback_e}"
                )

        # Image-based (scanned) PDF: no embedded text → OCR fallback.
        if len(text.strip()) < cls.MIN_EXTRACTION_LENGTH:
            ocr_text = cls._ocr_pdf_fallback(file_path)
            if len(ocr_text.strip()) > len(text.strip()):
                return ocr_text
        return text

    @staticmethod
    def _extract_text_from_docx(file_path: str) -> str:
        """Extract text from DOCX file."""
        try:
            from docx import Document

            doc = Document(file_path)
            text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
            return text
        except Exception as e:
            raise ValueError(f"Failed to extract DOCX text: {e}")

    @staticmethod
    def _extract_text_from_doc(file_path: str) -> str:
        """Extract text from DOC file (legacy Word).

        Note: python-docx doesn't support legacy .doc files.
        This requires python-docx[oxml] or external converter.
        """
        try:
            # Try using LibreOffice converter or fallback message
            import subprocess

            result = subprocess.run(
                [
                    "libreoffice",
                    "--headless",
                    "--convert-to",
                    "docx",
                    file_path,
                ],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                # Converted to DOCX, extract from that
                docx_path = file_path.replace(".doc", ".docx")
                if Path(docx_path).exists():
                    return ContentSampler._extract_text_from_docx(docx_path)

            raise ValueError("Could not convert DOC to DOCX")
        except Exception as e:
            raise ValueError(f"Failed to extract DOC text: {e}")

    @staticmethod
    def _extract_text_from_xlsx(file_path: str) -> str:
        """Extract text from XLSX file."""
        try:
            from openpyxl import load_workbook

            wb = load_workbook(file_path, data_only=True)
            text_parts = []

            for sheet_name in wb.sheetnames[:3]:  # First 3 sheets
                ws = wb[sheet_name]
                text_parts.append(f"Sheet: {sheet_name}")
                for row in ws.iter_rows(values_only=True):
                    # Convert row values to strings, skipping None
                    cells = [
                        str(cell) if cell is not None else "" for cell in row
                    ]
                    text_parts.append(" ".join(cells))

            return "\n".join(text_parts)
        except Exception as e:
            raise ValueError(f"Failed to extract XLSX text: {e}")

    @staticmethod
    def _extract_text_from_csv(file_path: str) -> str:
        """Extract text from CSV file."""
        try:
            import csv

            text_parts = []
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.reader(f)
                for row in reader:
                    text_parts.append(" ".join(row))
            return "\n".join(text_parts)
        except Exception as e:
            raise ValueError(f"Failed to extract CSV text: {e}")

    @staticmethod
    def _extract_text_from_html_or_txt(file_path: str) -> str:
        """Extract text from HTML/HTM/TXT using the shared extraction pipeline.

        For HTML files, this uses the document_utils extractor which includes
        JS-shell detection (returns empty for SPA framework pages).
        For TXT files, reads directly.
        """
        from pathlib import Path

        path = Path(file_path)
        if path.suffix.lower() in (".html", ".htm"):
            try:
                from ..extraction.document_utils import (
                    extract_text_from_document,
                )

                return extract_text_from_document(path) or ""
            except Exception as e:
                raise ValueError(f"Failed to extract HTML text: {e}")
        else:
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read()
            except Exception as e:
                raise ValueError(f"Failed to extract text: {e}")

    @classmethod
    def extract_text(cls, file_path: str) -> str:
        """Extract text from file based on extension."""
        file_path_lower = file_path.lower()

        if file_path_lower.endswith(".pdf"):
            return cls._extract_text_from_pdf(file_path)
        elif file_path_lower.endswith(".docx"):
            return cls._extract_text_from_docx(file_path)
        elif file_path_lower.endswith(".doc"):
            return cls._extract_text_from_doc(file_path)
        elif file_path_lower.endswith(".xlsx"):
            return cls._extract_text_from_xlsx(file_path)
        elif file_path_lower.endswith(".csv"):
            return cls._extract_text_from_csv(file_path)
        elif file_path_lower.endswith((".txt", ".html", ".htm")):
            return cls._extract_text_from_html_or_txt(file_path)
        else:
            raise ValueError(
                f"Unsupported file type for content sampling: {file_path}"
            )

    @classmethod
    def extract_text_from_string(cls, html_content: str) -> str:
        """Extract text from an HTML string (no file needed)."""
        from ..extraction.document_utils import _HTMLTextExtractor
        parser = _HTMLTextExtractor()
        parser.feed(html_content)
        return parser.get_text()

    @classmethod
    def validate_content(
        cls,
        file_path: str,
        required_keywords: list[str] | None = None,
        nice_to_have_keywords: list[str] | None = None,
        min_required_matches: int = 1,
    ) -> ContentSamplingResult:
        """Validate content of a file by checking for keywords.

        Args:
            file_path: Path to the file to validate
            required_keywords: List of keywords that must be present
            nice_to_have_keywords: List of keywords that improve score
            min_required_matches: Minimum required keyword matches to pass

        Returns:
            ContentSamplingResult with validation details
        """
        reasons: list[str] = []
        error = None
        text_extracted = None
        keywords_found: list[str] = []
        keywords_missing: list[str] = []
        confidence_score = 0.0
        sample_length = 0

        # Default to lower-case keywords
        required_keywords = [kw.lower() for kw in (required_keywords or [])]
        nice_to_have_keywords = [
            kw.lower() for kw in (nice_to_have_keywords or [])
        ]

        try:
            # Extract text
            text_extracted = cls.extract_text(file_path)
            text_lower = text_extracted.lower()
            sample_length = len(text_extracted)

            reasons.append(
                f"Extracted {sample_length} characters from {Path(file_path).name}"
            )

            # Check if extraction yielded meaningful content
            if sample_length < cls.MIN_EXTRACTION_LENGTH:
                reasons.append(
                    f"Extracted text too short ({sample_length} < {cls.MIN_EXTRACTION_LENGTH})"
                )
                return ContentSamplingResult(
                    success=False,
                    text_extracted=None,
                    keywords_found=keywords_found,
                    keywords_missing=keywords_found,
                    confidence_score=0.0,
                    sample_length=sample_length,
                    reasons=reasons,
                    error="Insufficient content extracted",
                )

            # Limit sample for analysis
            text_sample = text_lower[: cls.MAX_SAMPLE_CHARS]

            # Check required keywords
            for kw in required_keywords:
                if kw in text_sample:
                    keywords_found.append(kw)
                else:
                    keywords_missing.append(kw)

            # Check nice-to-have keywords
            nice_keywords_found = []
            for kw in nice_to_have_keywords:
                if kw in text_sample:
                    nice_keywords_found.append(kw)

            # Compute confidence score
            required_matches = len(keywords_found)
            nice_matches = len(nice_keywords_found)

            if required_keywords:
                required_ratio = required_matches / len(required_keywords)
            else:
                required_ratio = 1.0  # Pass if no required keywords specified

            if nice_to_have_keywords:
                nice_ratio = nice_matches / len(nice_to_have_keywords)
            else:
                nice_ratio = 1.0

            # Compute overall score: 70% required, 30% nice-to-have
            confidence_score = (required_ratio * 0.7) + (nice_ratio * 0.3)

            # Determine pass/fail
            # If no required keywords, automatically pass (success=True)
            # Otherwise, success requires achieving min_required_matches
            if not required_keywords:
                success = True
            else:
                success = required_matches >= min_required_matches

            if success:
                if required_keywords:
                    reasons.append(
                        f"Found {required_matches}/{len(required_keywords)} required keywords"
                    )
                else:
                    reasons.append(
                        "No required keywords specified (validation passed)"
                    )
                if nice_to_have_keywords:
                    reasons.append(
                        f"Found {nice_matches}/{len(nice_to_have_keywords)} nice-to-have keywords"
                    )
            else:
                reasons.append(
                    f"Only found {required_matches}/{len(required_keywords)} required keywords"
                )
                if keywords_missing:
                    reasons.append(
                        f"Missing keywords: {', '.join(keywords_missing)}"
                    )

            return ContentSamplingResult(
                success=success,
                text_extracted=text_extracted[: cls.MAX_SAMPLE_CHARS],
                keywords_found=keywords_found,
                keywords_missing=keywords_missing,
                confidence_score=confidence_score,
                sample_length=sample_length,
                reasons=reasons,
                error=None,
            )

        except Exception as e:
            error = str(e)
            reasons.append(f"Error during content sampling: {error}")
            return ContentSamplingResult(
                success=False,
                text_extracted=None,
                keywords_found=keywords_found,
                keywords_missing=required_keywords or [],
                confidence_score=0.0,
                sample_length=sample_length,
                reasons=reasons,
                error=error,
            )
