"""Extraction package - LLM-based PDF extraction."""

from permit_toolkit.extraction.extractor import PermitExtractor
from permit_toolkit.extraction.extractor_langextract import PermitExtractorLangExtract
from permit_toolkit.extraction.pdf_utils import (
    extract_text_from_pdf,
    load_schema,
    validate_extraction,
)

__all__ = [
    "PermitExtractor",  # OpenAI-based extractor (legacy/backup)
    "PermitExtractorLangExtract",  # LangExtract-based extractor (recommended)
    "extract_text_from_pdf",
    "load_schema",
    "validate_extraction",
]
