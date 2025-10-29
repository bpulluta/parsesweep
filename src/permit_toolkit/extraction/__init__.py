"""Extraction module for permit data extraction."""

from .pdf_utils import extract_text_from_pdf
from .permit_extractor import PermitExtractor, ExtractionResult
import json
from pathlib import Path


def load_schema(schema_path: Path) -> dict:
    """Load JSON schema from file."""
    with open(schema_path, 'r') as f:
        return json.load(f)


__all__ = [
    "PermitExtractor",
    "ExtractionResult",
    "extract_text_from_pdf",
    "load_schema",
]
