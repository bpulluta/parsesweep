"""Canonical extraction-record writer for the shared workflow."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from collections.abc import Sequence

from psweep.exceptions import SchemaValidationError
from psweep.utils.error_taxonomy import normalize_error_records
from psweep.utils.file_io import artifact_sha256, atomic_json
from psweep.utils.item_matcher import get_nested_value
from psweep.extraction.schema_utils import (
    record_validator as load_record_validator,
)

if TYPE_CHECKING:
    from psweep.utils.schema_metadata import SchemaMetadata


def _resolve_lineage_artifact_id(
    *,
    runtime_artifact: dict[str, Any] | None,
    run_id: str | None,
    default_fragment: str,
) -> str:
    artifact_id = (runtime_artifact or {}).get("artifact_id")
    if isinstance(artifact_id, str) and artifact_id.strip():
        return artifact_id
    lineage_artifact = ((runtime_artifact or {}).get("lineage") or {}).get(
        "artifact_id"
    )
    if isinstance(lineage_artifact, str) and lineage_artifact.strip():
        return lineage_artifact
    run_fragment = (run_id or default_fragment).replace("run://", "")
    return f"artifact://runtime/{run_fragment}"


def build_lineage(
    *,
    runtime_artifact: dict[str, Any] | None,
    run_id: str | None,
    model: str,
    provider: str | None,
    schema_id: str | None,
    default_run_fragment: str,
    extracted_at: str,
) -> dict[str, Any]:
    """Build canonical lineage metadata for extraction records."""
    return {
        "artifact_id": _resolve_lineage_artifact_id(
            runtime_artifact=runtime_artifact,
            run_id=run_id,
            default_fragment=default_run_fragment,
        ),
        "profile_id": ((runtime_artifact or {}).get("lineage") or {}).get(
            "profile_id"
        )
        or "default",
        "run_id": run_id or f"run://{default_run_fragment}",
        "model": model,
        "provider": provider or "unknown",
        "schema_id": schema_id,
        "extracted_at": extracted_at,
    }


def _extract_identifier(
    data: dict[str, Any],
    identifier_fields: Sequence[str],
) -> str:
    """Join declared scalar identifiers, retaining zero and false."""
    parts = []
    for field in identifier_fields:
        value = get_nested_value(data, field)
        if isinstance(value, (str, int, float)) and str(value).strip():
            parts.append(str(value).strip())
    return "-".join(parts)


def record_extraction_scope(
    result: Any,
    *,
    page_range: tuple[int, int] | None = None,
    section_filtered: bool = False,
    parser_scope: dict | None = None,
) -> None:
    """Retain selected-input limitations in the canonical result."""
    coverage = dict(getattr(result, "coverage", None) or {})
    warnings = list(getattr(result, "validation_notes", None) or [])
    if page_range is not None:
        coverage["selected_page_range"] = list(page_range)
        warnings.append(
            "Extraction limited to selected pages "
            f"{page_range[0]}-{page_range[1]}; "
            "not a full-document coverage guarantee."
        )
    if section_filtered:
        coverage["section_filtered"] = True
        warnings.append(
            "Extraction limited to selected sections; "
            "not a full-document coverage guarantee."
        )
    scope = parser_scope or {}
    if scope.get("page_count") is not None:
        coverage["page_count"] = scope["page_count"]
        extracted = scope.get("extracted_pages", [])
        coverage["extracted_pages"] = extracted
        selected = scope.get("selected_pages")
        if selected is None:
            selected = (
                list(range(page_range[0], page_range[1] + 1))
                if page_range
                else list(range(1, scope["page_count"] + 1))
            )
        coverage["parser_partial"] = set(selected) != set(extracted)
        if coverage["parser_partial"]:
            warnings.append(
                "PDF parser coverage is partial: missing selected pages; "
                "not all requested source pages were extracted."
            )
    result.coverage = coverage
    result.validation_notes = warnings


def build_extraction_record(
    *,
    doc_path: Path,
    result: Any,
    model: str,
    provider: str | None,
    runtime_artifact: dict[str, Any] | None = None,
    run_id: str | None = None,
    schema_id: str | None = None,
    schema_metadata: SchemaMetadata,
    input_fingerprint: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Build a source-identified record using domain schema metadata."""
    item_array_key = schema_metadata.get_main_data_array()
    items = get_nested_value(result.data, item_array_key)
    if not isinstance(items, list):
        message = (
            f"Schema-declared main data array '{item_array_key}' "
            "must be an array."
        )
        raise SchemaValidationError(message)
    num_items = len(items)
    identifier = _extract_identifier(
        result.data,
        schema_metadata.get_identifier_fields(),
    )
    if input_fingerprint is not None and (
        not isinstance(input_fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", input_fingerprint)
    ):
        message = (
            "Extraction input_fingerprint must be a lowercase SHA-256 digest."
        )
        raise ValueError(message)
    source_identity = hashlib.sha256(
        doc_path.resolve().as_posix().encode("utf-8"),
    ).hexdigest()
    source_sha256 = artifact_sha256(doc_path)
    extracted_at = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    lineage = build_lineage(
        runtime_artifact=runtime_artifact,
        run_id=run_id,
        model=model,
        provider=provider,
        schema_id=schema_id,
        default_run_fragment=uuid4().hex,
        extracted_at=extracted_at,
    )
    if input_fingerprint is not None:
        lineage["input_fingerprint"] = input_fingerprint

    run_fragment = lineage["run_id"].replace("run://", "")
    record = {
        "record_id": f"record://{run_fragment}/{source_identity}",
        "item_count": num_items,
        "contract_version": "1.0.0",
        "document": {
            "source_document_id": identifier or doc_path.stem,
            "source_path": doc_path.as_posix(),
            "source_filename": doc_path.name,
            "source_sha256": source_sha256,
        },
        "lineage": lineage,
        "payload": result.data,
        "quality": {
            "overall_confidence": getattr(result, "completeness_score", None),
            "warnings": list(getattr(result, "validation_notes", []) or []),
            "errors": normalize_error_records(
                getattr(result, "processing_errors", None)
                or getattr(result, "errors", None)
            ),
        },
        "processing_metrics": {
            "duration_seconds": getattr(result, "processing_time", 0.0),
            "cost_usd": getattr(result, "cost", 0.0),
            "input_tokens": getattr(result, "input_tokens", None),
            "output_tokens": getattr(result, "output_tokens", None),
        },
    }
    coverage = getattr(result, "coverage", None)
    if coverage:
        record["processing_metrics"]["coverage"] = coverage
    return record, num_items


def write_extraction_record(  # noqa: PLR0913 - canonical record context + validator
    output_path: Path,
    *,
    doc_path: Path,
    result: Any,
    model: str,
    provider: str | None,
    runtime_artifact: dict[str, Any] | None = None,
    run_id: str | None = None,
    schema_id: str | None = None,
    schema_metadata: SchemaMetadata,
    input_fingerprint: str | None = None,
    record_validator: Any = None,
) -> int:
    """Publish a record atomically and return its item count."""
    record, num_items = build_extraction_record(
        doc_path=doc_path,
        result=result,
        model=model,
        provider=provider,
        runtime_artifact=runtime_artifact,
        run_id=run_id,
        schema_id=schema_id,
        schema_metadata=schema_metadata,
        input_fingerprint=input_fingerprint,
    )
    if record_validator is None:
        record_validator = load_record_validator()
    record_validator.validate(record)
    atomic_json(output_path, record)
    return num_items
