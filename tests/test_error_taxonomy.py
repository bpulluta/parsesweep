"""Tests for canonical runtime error taxonomy helpers."""

from psweep.utils.error_taxonomy import build_error_record, summarize_error_records


def test_build_error_record_classifies_file_not_found() -> None:
    error = build_error_record(
        FileNotFoundError("missing.pdf"),
        stage="extract",
        document_path="documents/missing.pdf",
        model="gpt-5",
        provider="azure",
    )

    assert error["category"] == "document_io"
    assert error["code"] == "document_not_found"
    assert error["retryable"] is False


def test_build_error_record_marks_retryable_timeout() -> None:
    error = build_error_record(
        RuntimeError("Request timeout while calling provider"),
        stage="validation",
        document_path="doc-1",
        model="gpt-4o",
        provider="openai",
    )

    assert error["retryable"] is True


def test_build_error_record_classifies_context_window_exceeded() -> None:
    error = build_error_record(
        RuntimeError("context_window_exceeded: estimated request size 353161 prompt chars exceeds model budget"),
        stage="extract",
        document_path="documents/tariffs/PSCo_Electric_Entire_Tariff.pdf",
        model="compassop-gpt-4.1-mini",
        provider="azure",
    )

    assert error["category"] == "context_budget"
    assert error["code"] == "context_window_exceeded"


def test_summarize_error_records_aggregates_by_category_and_code() -> None:
    summary = summarize_error_records(
        [
            {
                "stage": "extract",
                "category": "document_processing",
                "code": "document_extraction_failed",
                "message": "OCR issue",
                "retryable": False,
                "source": {"document_path": "a.pdf", "model": "gpt-5", "provider": "azure"},
            },
            {
                "stage": "validation",
                "category": "internal",
                "code": "unexpected_processing_error",
                "message": "Unknown failure",
                "retryable": False,
                "source": {"document_path": "b.pdf", "model": "gpt-4o", "provider": "openai"},
            },
        ]
    )

    assert summary["total_errors"] == 2
    assert summary["by_category"] == {"document_processing": 1, "internal": 1}
    assert summary["by_code"] == {
        "document_extraction_failed": 1,
        "unexpected_processing_error": 1,
    }