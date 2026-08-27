"""Canonical extraction-record writer shared by CLI and pipeline APIs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

from ..utils.error_taxonomy import normalize_error_records


def _resolve_lineage_artifact_id(
    *,
    runtime_artifact: Optional[Dict[str, Any]],
    run_id: Optional[str],
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
    runtime_artifact: Optional[Dict[str, Any]],
    run_id: Optional[str],
    model: str,
    provider: str | None,
    schema_id: Optional[str],
    default_run_fragment: str,
    extracted_at: str,
) -> Dict[str, Any]:
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


def _extract_item_count(data: Dict[str, Any]) -> tuple[str | None, int]:
    main_array_key = None
    max_items = 0
    for key, value in data.items():
        if isinstance(value, list) and value and len(value) > max_items:
            max_items = len(value)
            main_array_key = key
    return main_array_key, max_items


def _extract_identifier(
    data: Dict[str, Any],
    identifier_fields: Optional[Sequence[str]] = None,
) -> str:
    identifier = "N/A"
    id_field_names = [
        field.lower() for field in (identifier_fields or ["id", "identifier", "number", "name"])
    ]

    for key, value in data.items():
        if isinstance(value, dict):
            for id_field in id_field_names:
                if id_field in value:
                    id_val = value[id_field]
                    if isinstance(id_val, dict):
                        parts = [str(v) for v in id_val.values() if v]
                        identifier = "-".join(parts) if parts else "N/A"
                    elif id_val:
                        identifier = str(id_val)
                    break
        elif (
            isinstance(value, (str, int))
            and value
            and key.lower() in id_field_names
        ):
            identifier = str(value)

    return identifier


def build_extraction_record(
    *,
    doc_path: Path,
    result: Any,
    model: str,
    provider: str | None,
    runtime_artifact: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    schema_id: Optional[str] = None,
    identifier_fields: Optional[Sequence[str]] = None,
) -> tuple[Dict[str, Any], int]:
    """Build the canonical ParseSweep extraction-record payload."""
    item_array_key, num_items = _extract_item_count(result.data)
    identifier = _extract_identifier(result.data, identifier_fields)
    extracted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    lineage = build_lineage(
        runtime_artifact=runtime_artifact,
        run_id=run_id,
        model=model,
        provider=provider,
        schema_id=schema_id,
        default_run_fragment=doc_path.stem,
        extracted_at=extracted_at,
    )

    record = {
        "record_id": f"record://{lineage['run_id'].replace('run://', '')}/{doc_path.stem}",
        "contract_version": "1.0.0",
        "document": {
            "source_document_id": identifier if identifier != "N/A" else doc_path.stem,
            "source_path": doc_path.as_posix(),
            "source_filename": doc_path.name,
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
    if item_array_key:
        record["item_count"] = num_items
    return record, num_items


def write_extraction_record(
    output_path: Path,
    *,
    doc_path: Path,
    result: Any,
    model: str,
    provider: str | None,
    runtime_artifact: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    schema_id: Optional[str] = None,
    identifier_fields: Optional[Sequence[str]] = None,
) -> int:
    """Write a canonical extraction record and return the extracted item count."""
    record, num_items = build_extraction_record(
        doc_path=doc_path,
        result=result,
        model=model,
        provider=provider,
        runtime_artifact=runtime_artifact,
        run_id=run_id,
        schema_id=schema_id,
        identifier_fields=identifier_fields,
    )
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
    return num_items
