"""Factory for creating state-specific permit extractors."""

import logging
from pathlib import Path
from typing import Dict, Any, Optional

from permit_toolkit.extraction.pdf_utils import extract_text_from_pdf

logger = logging.getLogger(__name__)


class ExtractorFactory:
    """Factory for creating state-specific permit extractors with auto-detection."""
    
    @staticmethod
    def create_extractor(
        state: Optional[str] = None,
        pdf_path: Optional[Path] = None,
        api_key: str = None,
        schema: Dict[str, Any] = None,
        **kwargs
    ):
        """
        Create appropriate extractor for the state.
        
        Args:
            state: Explicit state name ("virginia", "illinois", etc.)
            pdf_path: Auto-detect state from PDF content or path
            api_key: OpenAI API key (required)
            schema: JSON schema for validation (required)
            **kwargs: Additional arguments passed to extractor constructor
            
        Returns:
            State-specific extractor instance
            
        Raises:
            ValueError: If state cannot be determined or is not supported
        """
        if not api_key:
            raise ValueError("api_key is required")
        if not schema:
            raise ValueError("schema is required")
        
        # Auto-detect state if not provided
        if not state and pdf_path:
            state = ExtractorFactory._detect_state(pdf_path)
        
        if not state:
            raise ValueError("Must provide either 'state' or 'pdf_path' for state detection")
        
        state = state.lower()
        
        # Import extractors dynamically to avoid circular imports
        from permit_toolkit.extraction.virginia_extractor import VirginiaPermitExtractor
        
        extractors = {
            "virginia": VirginiaPermitExtractor,
            # Future: "illinois": IllinoisPermitExtractor,
            # Future: "texas": TexasPermitExtractor,
        }
        
        if state not in extractors:
            supported = ', '.join(extractors.keys())
            raise ValueError(
                f"No extractor available for state: {state}. "
                f"Supported states: {supported}"
            )
        
        logger.info(f"Creating {state.title()} permit extractor")
        extractor_class = extractors[state]
        return extractor_class(api_key=api_key, schema=schema, **kwargs)
    
    @staticmethod
    def _detect_state(pdf_path: Path) -> str:
        """
        Auto-detect state from PDF path or content.
        
        Detection strategy:
        1. Check file path for state name
        2. Check first 1500 chars of PDF text for state-specific markers
        
        Args:
            pdf_path: Path to PDF file
            
        Returns:
            State name (lowercase)
            
        Raises:
            ValueError: If state cannot be detected
        """
        # Strategy 1: Check path
        path_str = str(pdf_path)
        
        state_patterns_in_path = {
            "virginia": ["Virginia", "VA"],
            "illinois": ["Illinois", "IL"],
            "texas": ["Texas", "TX"],
        }
        
        for state, patterns in state_patterns_in_path.items():
            if any(pattern in path_str for pattern in patterns):
                logger.info(f"Detected state from path: {state}")
                return state
        
        # Strategy 2: Check content
        try:
            text = extract_text_from_pdf(pdf_path)[:1500]
            
            state_markers_in_content = {
                "virginia": [
                    "COMMONWEALTH of VIRGINIA",
                    "COMMONWEALTH OF VIRGINIA",
                    "Virginia Department of Environmental Quality",
                    "VIRGINIA DEPARTMENT OF ENVIRONMENTAL QUALITY",
                    "Registration No",  # Virginia uses "Registration No"
                ],
                "illinois": [
                    "Illinois Environmental Protection Agency",
                    "ILLINOIS ENVIRONMENTAL PROTECTION AGENCY",
                    "State of Illinois",
                    "STATE OF ILLINOIS",
                ],
                "texas": [
                    "Texas Commission on Environmental Quality",
                    "TEXAS COMMISSION ON ENVIRONMENTAL QUALITY",
                    "TCEQ",
                ],
            }
            
            for state, markers in state_markers_in_content.items():
                if any(marker in text for marker in markers):
                    logger.info(f"Detected state from content: {state}")
                    return state
        
        except Exception as e:
            logger.warning(f"Could not read PDF for state detection: {e}")
        
        raise ValueError(
            f"Cannot detect state from {pdf_path}. "
            "Please specify 'state' explicitly or ensure the file path contains the state name."
        )
    
    @staticmethod
    def get_supported_states():
        """Get list of supported states."""
        return ["virginia"]  # Future: add more as implemented
