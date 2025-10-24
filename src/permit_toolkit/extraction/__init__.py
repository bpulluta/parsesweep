"""Extraction module for permit data extraction."""

from permit_toolkit.extraction.base_extractor import BasePermitExtractor
from permit_toolkit.extraction.virginia_extractor import VirginiaPermitExtractor
from permit_toolkit.extraction.extractor_factory import ExtractorFactory
from permit_toolkit.extraction.pdf_utils import extract_text_from_pdf, validate_extraction
import json
from pathlib import Path

def load_schema(schema_path: Path) -> dict:
    """Load JSON schema from file."""
    with open(schema_path, 'r') as f:
        return json.load(f)

__all__ = [
    "BasePermitExtractor",  # Base class for hybrid extraction
    "VirginiaPermitExtractor",  # Virginia-specific hybrid extractor
    "ExtractorFactory",  # Factory for creating state-specific extractors
    "extract_text_from_pdf",
    "validate_extraction",
    "load_schema",
]
