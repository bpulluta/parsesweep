"""
Air Quality Permit Toolkit

A production-ready toolkit for extracting structured data from air quality permits
using state-of-the-art LLMs. Supports multi-state permit formats, intelligent 
deduplication, and automated data consolidation.

Features:
- Hybrid LLM extraction (OpenAI + optional LangExtract)
- Cross-state compatibility (Virginia, Illinois, extensible)
- Smart deduplication and error handling
- Modern CLI with progress tracking
- CSV/Excel export for analysis
"""

__version__ = "0.1.0"
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
