"""Test configuration for optional dependencies."""

import sys
from unittest.mock import MagicMock

# Mock serpapi module before any imports from streamline_extract
# This allows tests to run even when serpapi is not installed
if "serpapi" not in sys.modules:
    serpapi_mock = MagicMock()
    sys.modules["serpapi"] = serpapi_mock
