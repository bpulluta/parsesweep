"""PDF extraction utilities using LLMs."""

import logging
from pathlib import Path
from typing import Dict, Any
import json

from pypdf import PdfReader

# Suppress pypdf warnings about PDF structure issues (common in scanned documents)
logging.getLogger('pypdf').setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


def extract_text_from_pdf(pdf_path: Path) -> str:
    """
    Extract text from a PDF file.
    
    Args:
        pdf_path: Path to the PDF file
        
    Returns:
        Extracted text as a string
    """
    text = ""
    try:
        with open(pdf_path, 'rb') as f:
            pdf_reader = PdfReader(f)
            for page in pdf_reader.pages:
                text += page.extract_text() + "\n"
    except Exception as e:
        logger.error(f"Error extracting text from {pdf_path}: {e}")
        return ""
    
    return text


def truncate_text(text: str, max_chars: int = 50000) -> str:
    """
    Intelligently truncate text if too long.
    
    Keeps the beginning (70%) and end (30%) of the document
    to preserve both header information and summary sections.
    
    Args:
        text: Text to truncate
        max_chars: Maximum characters to keep (~12,500 tokens)
        
    Returns:
        Truncated text
    """
    if len(text) <= max_chars:
        return text
    
    keep_first = int(max_chars * 0.7)
    keep_last = max_chars - keep_first
    
    truncated = (
        text[:keep_first] + 
        "\n\n[...middle section truncated...]\n\n" + 
        text[-keep_last:]
    )
    
    logger.info(f"Document truncated from {len(text)} to {max_chars} characters")
    return truncated


def load_schema(schema_path: Path) -> Dict[str, Any]:
    """
    Load JSON schema from file.
    
    Args:
        schema_path: Path to the schema file
        
    Returns:
        Schema dictionary
    """
    with open(schema_path, 'r') as f:
        return json.load(f)


def create_empty_result() -> Dict[str, Any]:
    """
    Create an empty extraction result matching the schema.
    
    Returns:
        Empty result dictionary
    """
    return {
        "permitDetails": {
            "permitNumber": None,
            "permitIssuanceDate": None,
            "permitExpirationDate": None,
            "facilityName": None,
            "facilityAddress": None,
            "facilityCounty": None
        },
        "generatorSets": [],
        "complianceRequirements": [],
        "recordKeepingRequirements": [],
        "notifications": []
    }


def validate_extraction(result: Dict[str, Any]) -> list[str]:
    """
    Validate extraction results and return warnings.
    
    Args:
        result: Extraction result dictionary
        
    Returns:
        List of validation warnings
    """
    warnings = []
    
    permit_details = result.get('permitDetails', {})
    if not permit_details.get('permitNumber'):
        warnings.append("Missing permit number")
    
    generator_sets = result.get('generatorSets', [])
    if not generator_sets:
        warnings.append("No generator sets extracted")
    else:
        for i, gen in enumerate(generator_sets):
            if not gen.get('ratedCapacityKW'):
                warnings.append(f"Generator {i+1} missing capacity (kW)")
            if not gen.get('fuelType'):
                warnings.append(f"Generator {i+1} missing fuel type")
    
    return warnings
