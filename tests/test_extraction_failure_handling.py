"""Regression tests for hard extraction failure propagation."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from psweep.extraction.document_extractor import DocumentExtractor
from psweep.extraction.llm_client import LLMClient
from psweep.utils.exceptions import ExtractionError


def test_document_extractor_propagates_llm_failure() -> None:
    extractor = DocumentExtractor(api_key="test-key", model="gpt-4o-mini")

    with patch.object(extractor.client, "extract", side_effect=ExtractionError("context_length_exceeded")):
        with pytest.raises(ExtractionError, match="context_length_exceeded"):
            extractor.extract("sample text", {"type": "object", "properties": {}})


def test_llm_client_raises_on_empty_response() -> None:
    client = LLMClient(api_key="test-key", model="gpt-4o-mini", provider="openai")
    response = SimpleNamespace(choices=[])

    with patch("psweep.extraction.llm_client.completion", return_value=response):
        with pytest.raises(ExtractionError, match="Empty response"):
            client.extract("sample text", {"type": "object", "properties": {}})


def test_llm_client_raises_on_provider_exception() -> None:
    client = LLMClient(api_key="test-key", model="gpt-4o-mini", provider="openai")

    with patch("psweep.extraction.llm_client.completion", side_effect=RuntimeError("context_length_exceeded")):
        with pytest.raises(ExtractionError, match="context_length_exceeded"):
            client.extract("sample text", {"type": "object", "properties": {}})


def test_llm_client_preflight_rejects_oversized_request_before_provider_call() -> None:
    # Context window is now config-driven (no hardcoded deployment names): the
    # guard fires only for models present in the supplied context_windows map.
    client = LLMClient(
        api_key="test-key",
        model="my-azure-deployment",
        provider="azure",
        context_windows={"my-azure-deployment": 300000},
    )
    oversized_text = "x" * 1_300_000

    with patch("psweep.extraction.llm_client.completion") as completion_mock:
        with pytest.raises(ExtractionError, match="context_window_exceeded"):
            client.extract(oversized_text, {"type": "object", "properties": {}})

    completion_mock.assert_not_called()


def test_llm_client_no_context_guard_when_window_unset() -> None:
    # A model absent from context_windows is not guarded — the oversized request
    # is allowed through to the provider (mocked) instead of failing preflight.
    client = LLMClient(api_key="test-key", model="some-model", provider="azure")
    oversized_text = "x" * 1_300_000

    with patch("psweep.extraction.llm_client.completion") as completion_mock:
        completion_mock.side_effect = RuntimeError("reached provider")
        with pytest.raises(ExtractionError):
            client.extract(oversized_text, {"type": "object", "properties": {}})

    completion_mock.assert_called_once()