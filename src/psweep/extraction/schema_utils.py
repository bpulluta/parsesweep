"""Schema loading utilities."""

import json
from pathlib import Path
from typing import Dict, Any


def load_schema(schema_path: Path) -> Dict[str, Any]:
    """
    Load JSON schema from file.

    Args:
        schema_path: Path to JSON schema file

    Returns:
        Parsed JSON schema as dictionary

    Raises:
        FileNotFoundError: If schema file doesn't exist
        json.JSONDecodeError: If schema file is not valid JSON
    """
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)
