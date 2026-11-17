"""Pure review-policy checks and target binding shared by every runtime entry point."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from .targets import TARGET_INPUT_FIELDS, render_target_template


def validate_review_policy(
    context_fields: list[str] | None, coverage: dict[str, str] | None
) -> None:
    """Check metadata hints and coverage labels without provider setup or target mutation."""
    context_fields = [] if context_fields is None else context_fields
    if (
        not isinstance(context_fields, list)
        or any(not isinstance(field, str) or not field for field in context_fields)
        or len(set(context_fields)) != len(context_fields)
    ):
        raise ValueError("context_fields must be distinct metadata field names")
    if set(context_fields) & TARGET_INPUT_FIELDS:
        raise ValueError("context_fields cannot include search input fields")
    if coverage is not None and (
        not isinstance(coverage, dict)
        or any(
            not isinstance(label, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", label)
            or not isinstance(description, str)
            or not description.strip()
            for label, description in coverage.items()
        )
    ):
        raise ValueError("coverage must map simple labels to nonempty descriptions")


def render_review_scope(
    description: str, coverage: dict[str, str] | None, target: Mapping[str, Any]
) -> tuple[str, dict[str, str]]:
    """Render domain-authored review requirements against one untrusted target row."""
    return render_target_template(description, target), {
        label: render_target_template(template, target)
        for label, template in (coverage or {}).items()
    }


def iter_review_scopes(
    description: str,
    context_fields: list[str] | None,
    coverage: dict[str, str] | None,
    targets: Iterable[Mapping[str, Any]],
    *,
    additional_templates: Iterable[str] = (),
) -> Iterator[tuple[str, dict[str, str]]]:
    """Validate all bindings before callers act on them, without materializing target rows."""
    validate_review_policy(context_fields, coverage)
    known_fields = set()
    additional_templates = tuple(additional_templates)
    for index, target in enumerate(targets):
        known_fields.update(target)
        try:
            scope = render_review_scope(description, coverage, target)
            for template in additional_templates:
                render_target_template(template, target)
        except ValueError as exc:
            raise ValueError(f"discovery.targets[{index}]: {exc}") from exc
        yield scope
    unknown = set(context_fields or []) - known_fields
    if unknown:
        raise ValueError(f"Unknown review context_fields: {sorted(unknown)}")
