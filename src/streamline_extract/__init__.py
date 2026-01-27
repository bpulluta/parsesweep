"""
StreamlineExtract

AI-powered toolkit for extracting structured data from PDF documents using 
state-of-the-art LLMs. Supports customizable schemas, intelligent deduplication, 
and automated data consolidation for any document type.

Features:
- Hybrid LLM extraction (OpenAI + optional LangExtract)
- Customizable schemas for any document type
- Smart deduplication and error handling
- Modern CLI with progress tracking
- CSV/Excel export for analysis
"""

__version__ = "2.0.1"
__author__ = "NREL Team"
__license__ = "MIT"

from pathlib import Path

# Package root directory
PACKAGE_ROOT = Path(__file__).parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]

__all__ = [
    "__version__",
    "__author__",
    "__license__",
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
]
