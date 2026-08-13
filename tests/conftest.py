"""Test configuration for optional dependencies."""

import os
import sys
from unittest.mock import MagicMock

# Force color OFF before psweep imports build any Rich console. CLI tests assert
# on plain-text error substrings (e.g. "Missing option '--config'"); when Rich
# emits ANSI codes those substrings get split and the assertions fail. Some
# environments (notably GitHub Actions) export FORCE_COLOR, so pin deterministic
# no-color output here rather than depending on TTY detection.
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"
os.environ.pop("FORCE_COLOR", None)

# Mock serpapi module before any imports from psweep
# This allows tests to run even when serpapi is not installed
if "serpapi" not in sys.modules:
    serpapi_mock = MagicMock()
    sys.modules["serpapi"] = serpapi_mock
