"""Regression tests for hard extraction failure propagation."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from psweep.extraction.document_extractor import DocumentExtractor
from psweep.extraction.llm_client import LLMClient
from psweep.exceptions import ExtractionError


def test_document_extractor_propagates_llm_failure() -> None:
    extractor = DocumentExtractor(api_key="test-key", model="gpt-4o-mini")

    with (
        patch.object(
            extractor.client,
            "extract",
            side_effect=ExtractionError("context_length_exceeded"),
        ),
        pytest.raises(ExtractionError, match="context_length_exceeded"),
    ):
        extractor.extract("sample text", {"type": "object", "properties": {}})


def test_llm_client_raises_on_empty_response() -> None:
    client = LLMClient(
        api_key="test-key", model="gpt-4o-mini", provider="openai"
    )
    response = SimpleNamespace(choices=[])

    with (
        patch(
            "psweep.extraction.llm_client.completion", return_value=response
        ),
        pytest.raises(ExtractionError, match="Empty response"),
    ):
        client.extract("sample text", {"type": "object", "properties": {}})


def test_llm_client_raises_on_provider_exception() -> None:
    client = LLMClient(
        api_key="test-key", model="gpt-4o-mini", provider="openai"
    )

    with (
        patch(
            "psweep.extraction.llm_client.completion",
            side_effect=RuntimeError("context_length_exceeded"),
        ),
        pytest.raises(ExtractionError, match="context_length_exceeded"),
    ):
        client.extract("sample text", {"type": "object", "properties": {}})


def test_context_guard_rejects_before_provider_call():
    """Oversized prompts fail without contacting the provider."""
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
            client.extract(
                oversized_text, {"type": "object", "properties": {}}
            )

    completion_mock.assert_not_called()


def test_llm_client_no_context_guard_when_window_unset() -> None:
    # A model absent from context_windows is not guarded — the oversized request
    # is allowed through to the provider (mocked) instead of failing preflight.
    client = LLMClient(
        api_key="test-key", model="some-model", provider="azure"
    )
    oversized_text = "x" * 1_300_000

    with patch("psweep.extraction.llm_client.completion") as completion_mock:
        completion_mock.side_effect = RuntimeError("reached provider")
        with pytest.raises(ExtractionError):
            client.extract(
                oversized_text, {"type": "object", "properties": {}}
            )

    completion_mock.assert_called_once()


def test_retry_without_temperature_preserves_usage():
    """A parameter rejection retries once and counts both attempts."""
    client = LLMClient(
        api_key="test-key",
        model="claude-opus-4-8",
        provider="openai",
        base_url="https://proxy.example.com/v1",
    )

    class _Msg:
        content = '{"requirements":[]}'

    class _Choice:
        message = _Msg()

    response = SimpleNamespace(
        choices=[_Choice()],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )

    with patch("psweep.extraction.llm_client.completion") as completion_mock:
        completion_mock.side_effect = [
            RuntimeError(
                "invalid_request_error: `temperature` is deprecated "
                "for this model."
            ),
            response,
        ]
        with patch(
            "psweep.extraction.llm_client.completion_cost", return_value=0.0
        ):
            out = client.extract(
                "sample text", {"type": "object", "properties": {}}
            )

    assert out["data"] == {"requirements": []}
    assert completion_mock.call_count == 2
    first_kwargs = completion_mock.call_args_list[0].kwargs
    second_kwargs = completion_mock.call_args_list[1].kwargs
    assert "temperature" in first_kwargs
    assert "temperature" not in second_kwargs
    assert client.usage["calls"] == 2


@pytest.mark.parametrize("content", [None, "not json"])
def test_failed_response_retains_billed_usage(content):
    """A rejected paid reply is still accounted for."""
    client = LLMClient(api_key="fixture-key", provider="openai")
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=13, completion_tokens=7),
    )
    with (
        patch(
            "psweep.extraction.llm_client.completion", return_value=response
        ),
        patch(
            "psweep.extraction.llm_client.completion_cost", return_value=0.025
        ),
        pytest.raises(ExtractionError) as caught,
    ):
        client.extract("text", {"type": "object"})
    assert caught.value.usage["cost"] == 0.025
    assert caught.value.usage["input_tokens"] == 13
    assert caught.value.usage["output_tokens"] == 7
    assert client.usage["cost"] == 0.025


def test_schema_rejection_preserves_provider_usage():
    """Post-call schema failure retains returned billing evidence."""
    extractor = DocumentExtractor(api_key="fixture-key")
    with (
        patch.object(
            extractor.client,
            "extract",
            return_value={
                "data": {},
                "cost": 0.012,
                "input_tokens": 20,
                "output_tokens": 4,
            },
        ),
        pytest.raises(Exception) as caught,
    ):
        extractor.extract("text", {"type": "object", "required": ["items"]})
    assert caught.value.usage == {
        "cost": 0.012,
        "input_tokens": 20,
        "output_tokens": 4,
    }
