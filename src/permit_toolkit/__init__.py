"""
Air Quality Permit Toolkit

A comprehensive toolkit for extracting structured data from air quality permits
across the United States. Supports web scraping, PDF extraction using LLMs,
and data consolidation into analysis-ready datasets.
"""

__version__ = "0.1.0"
__author__ = "Your Name"

from pathlib import Path

# Package root directory
PACKAGE_ROOT = Path(__file__).parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]

__all__ = [
    "__version__",
    "__author__",
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
]
