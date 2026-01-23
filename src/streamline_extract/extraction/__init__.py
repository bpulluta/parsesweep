"""Extraction module for document data extraction."""

from .pdf_utils import extract_text_from_pdf
from .document_utils import (
    extract_text_from_document,
    is_supported_document,
    SUPPORTED_EXTENSIONS
)
from .document_extractor import DocumentExtractor, ExtractionResult
import json
from pathlib import Path


def load_schema(schema_path: Path) -> dict:
    """Load JSON schema from file."""
    with open(schema_path, 'r') as f:
        return json.load(f)


__all__ = [
    "DocumentExtractor",
    "ExtractionResult",
    "extract_text_from_pdf",
    "extract_text_from_document",
    "is_supported_document",
    "SUPPORTED_EXTENSIONS",
    "load_schema",
]
