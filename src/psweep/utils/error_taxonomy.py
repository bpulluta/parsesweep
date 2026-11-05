"""Structured error taxonomy helpers for deterministic runtime reporting."""

from __future__ import annotations

from typing import Any
from collections.abc import Iterable

from psweep.exceptions import (
    CompilationError,
    ExtractionError,
    SchemaMetadataError,
    SchemaValidationError,
)
from psweep.utils.retry import CONNECTION_ERRORS, http_status, is_transient_error


def build_error_record(
    exc: BaseException,
    *,
    stage: str,
    document_path: str | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Convert a runtime exception into the canonical error payload."""
    message = str(exc)
    lowered_message = message.lower()
    category = "internal"
    code = "unexpected_processing_error"

    if (
        "context_window_exceeded" in lowered_message
        or "context_length_exceeded" in lowered_message
        or "maximum context length" in lowered_message
    ):
        category = "context_budget"
        code = "context_window_exceeded"
    elif isinstance(exc, FileNotFoundError):
        category = "document_io"
        code = "document_not_found"
    elif isinstance(exc, SchemaMetadataError):
        category = "schema_contract"
        code = "schema_metadata_invalid"
    elif isinstance(exc, SchemaValidationError):
        category = "schema_contract"
        code = "schema_validation_failed"
    elif isinstance(exc, CompilationError):
        category = "compilation"
        code = "compilation_failed"
    elif isinstance(exc, ExtractionError):
        category = "extraction"
        code = "extraction_failed"
    elif (status := http_status(exc)) is not None:
        category = "network"
        code = f"http_{status}"
    elif isinstance(exc, ValueError):
        category = "input_validation"
        code = "invalid_input"
    elif isinstance(exc, CONNECTION_ERRORS):
        category = "network"
        code = "network_error"
    elif isinstance(exc, RuntimeError):
        if (
            "decode" in lowered_message
            or "extract from" in lowered_message
            or "ocr" in lowered_message
        ):
            category = "document_extraction"
            code = "document_extraction_failed"
        else:
            category = "runtime"
            code = "runtime_failure"

    return {
        "stage": stage,
        "category": category,
        "code": code,
        "message": message,
        "retryable": is_transient_error(exc),
        "source": {
            "document_path": document_path,
            "model": model,
            "provider": provider,
        },
    }


def normalize_error_records(
    errors: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return a deterministic list of canonical error records."""
    normalized: list[dict[str, Any]] = []
    for error in errors or []:
        if not isinstance(error, dict):
            continue
        source = error.get("source") if isinstance(error.get("source"), dict) else {}
        record = {
            "stage": error.get("stage") or "unknown",
            "category": error.get("category") or "internal",
            "code": error.get("code") or "unexpected_processing_error",
            "message": error.get("message") or "Unknown error",
            "retryable": bool(error.get("retryable", False)),
            "source": {
                "document_path": source.get("document_path"),
                "model": source.get("model"),
                "provider": source.get("provider"),
            },
        }
        # Discovery errors are attributed to their target (or ``_shared``).
        if error.get("target_label"):
            record["target_label"] = error["target_label"]
        normalized.append(record)

    return sorted(
        normalized,
        key=lambda item: (
            item["source"].get("document_path") or "",
            item["source"].get("model") or "",
            item["category"],
            item["code"],
            item["message"],
        ),
    )


def summarize_error_records(
    errors: Iterable[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Aggregate deterministic error totals for manifests and metadata."""
    records = normalize_error_records(errors)
    by_category: dict[str, int] = {}
    by_code: dict[str, int] = {}

    for error in records:
        by_category[error["category"]] = by_category.get(error["category"], 0) + 1
        by_code[error["code"]] = by_code.get(error["code"], 0) + 1

    return {
        "total_errors": len(records),
        "by_category": dict(sorted(by_category.items())),
        "by_code": dict(sorted(by_code.items())),
        "records": records,
    }
