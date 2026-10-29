"""Schema loading utilities."""

import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Dict, Union

from ..config.file_loader import load_json_file


def load_schema(schema_path: Union[str, Path]) -> Dict[str, Any]:
    """
    Load JSON schema from file.

    Parameters
    ----------
    schema_path : Union[str, Path]
        Path to JSON schema file

    Returns
    -------
    Dict[str, Any]
        Parsed JSON schema as dictionary

    Raises
    ------
    ConfigurationError
        If the file is missing or not valid JSON
    """
    schema = load_json_file(schema_path)
    build_schema_validator(schema)
    return schema


def build_schema_validator(schema: Dict[str, Any]):
    """Validate a schema definition and return its format-aware validator."""
    from jsonschema import FormatChecker, SchemaError
    from jsonschema.validators import validator_for
    from ..exceptions import SchemaValidationError

    validator_class = validator_for(schema)
    try:
        validator_class.check_schema(schema)
    except SchemaError as exc:
        message = f"Invalid extraction schema: {exc.message}"
        raise SchemaValidationError(message) from exc
    return validator_class(schema, format_checker=FormatChecker())


@lru_cache(maxsize=1)
def record_validator():
    """Load the canonical record-envelope validator once per process."""
    path = files("psweep.extraction").joinpath(
        "data/extraction_record.schema.json"
    )
    return build_schema_validator(
        json.loads(path.read_text(encoding="utf-8")),
    )
