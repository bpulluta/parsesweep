"""Schema loading utilities."""

from pathlib import Path
from typing import Any, Dict, Union

from ..config.file_loader import load_json_file


def load_schema(schema_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Load JSON schema from file.

    Args:
        schema_path: Path to JSON schema file

    Returns:
        Parsed JSON schema as dictionary

    Raises:
        ConfigurationError: If the file is missing or not valid JSON
    """
    return load_json_file(schema_path)
