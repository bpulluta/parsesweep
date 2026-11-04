"""Runtime config error type and strict key checking."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from difflib import get_close_matches
from typing import Any


class RuntimeConfigError(ValueError):
    """Raised when runtime config cannot be loaded or validated."""


def reject_unknown_keys(
    values: Mapping[str, Any],
    allowed: Iterable[str],
    location: str = "",
    config_path: str | None = None,
) -> None:
    """Fail on the first unknown key, suggesting the closest allowed key."""
    allowed = sorted(allowed)
    for key in values:
        if key in allowed:
            continue
        path = f"{location}.{key}" if location else str(key)
        close = get_close_matches(str(key), allowed, n=1)
        hint = (
            f" — did you mean '{close[0]}'?"
            if close
            else f" (allowed: {', '.join(allowed)})"
        )
        prefix = f"{config_path}: " if config_path else ""
        raise RuntimeConfigError(f"{prefix}unknown key '{path}'{hint}")
