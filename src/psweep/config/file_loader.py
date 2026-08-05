"""Centralized JSON/YAML file loading with consistent error reporting.

Single source of truth for reading config and schema files from disk. Every
caller that loads a ``.json``, ``.yaml``, or ``.yml`` config/schema file should
route through here so missing-file and malformed-content failures surface with
one uniform, actionable message instead of a bare ``FileNotFoundError`` or
``json.JSONDecodeError``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Union

import yaml

from ..exceptions import ConfigurationError

PathLike = Union[str, Path]


def load_json_file(path: PathLike) -> Dict[str, Any]:
    """Load and parse a JSON file, raising ``ConfigurationError`` on failure."""
    file_path = Path(path)
    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigurationError(f"File not found: {file_path}") from exc
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"Invalid JSON in {file_path}: {exc}"
        ) from exc


def load_yaml_file(path: PathLike) -> Dict[str, Any]:
    """Load and parse a YAML file, raising ``ConfigurationError`` on failure."""
    file_path = Path(path)
    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigurationError(f"File not found: {file_path}") from exc
    try:
        return yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"Invalid YAML in {file_path}: {exc}"
        ) from exc


def load_config_file(path: PathLike) -> Dict[str, Any]:
    """Load a config file, dispatching on extension (.json/.yaml/.yml)."""
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return load_yaml_file(file_path)
    if suffix == ".json":
        return load_json_file(file_path)
    raise ConfigurationError(
        f"Unsupported config extension '{suffix}' for {file_path}. "
        "Use .yaml, .yml, or .json"
    )
