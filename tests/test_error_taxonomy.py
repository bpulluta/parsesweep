"""Tests for canonical runtime error taxonomy helpers."""

import pytest
import requests

from psweep.discovery.browser import BrowserDownloadError
from psweep.exceptions import ExtractionError
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
        RuntimeError(
            "context_window_exceeded: estimated request size 353161 prompt chars "
            "exceeds model budget"
        ),
        stage="extract",
        document_path="documents/tariffs/PSCo_Electric_Entire_Tariff.pdf",
        model="gpt-4.1-mini",
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


def test_error_taxonomy_has_one_retry_classifier() -> None:
    import psweep.utils.error_taxonomy as taxonomy
    from psweep.utils import retry

    assert not hasattr(taxonomy, "_is_retryable")
    assert not hasattr(retry, "error_record")
    assert taxonomy.is_transient_error is retry.is_transient_error


def _http_error(status: int) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.exceptions.HTTPError(f"{status} Client Error", response=response)


@pytest.mark.parametrize(("status", "retryable"), [(403, False), (404, False), (503, True)])
def test_build_error_record_labels_http_status(status: int, retryable: bool) -> None:
    error = build_error_record(_http_error(status), stage="discovery.download", provider="http")

    assert error["category"] == "network"
    assert error["code"] == f"http_{status}"
    assert error["retryable"] is retryable


def test_build_error_record_labels_status_carrying_browser_error() -> None:
    error = build_error_record(
        BrowserDownloadError("fetch failed", status_code=410), stage="discovery.download"
    )

    assert (error["category"], error["code"]) == ("network", "http_410")


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.Timeout("slow"),
        TimeoutError(),
    ],
)
def test_build_error_record_labels_connection_failures(exc: BaseException) -> None:
    error = build_error_record(exc, stage="discovery.download", provider="http")

    assert (error["category"], error["code"]) == ("network", "network_error")
    assert error["retryable"] is True


def test_extraction_error_keeps_its_category_when_it_carries_a_status() -> None:
    exc = ExtractionError("provider refused")
    exc.status_code = 429

    assert build_error_record(exc, stage="extract")["category"] == "extraction"
