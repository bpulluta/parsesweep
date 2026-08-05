"""Shared test helpers.

Small, dependency-free utilities reused across multiple test modules. Kept
minimal on purpose — anything domain-specific belongs in the individual test
files or a fixture.
"""

from __future__ import annotations

import json
from pathlib import Path


def write_json(path: Path, content: dict) -> None:
    """Write ``content`` as indented JSON, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2), encoding="utf-8")
