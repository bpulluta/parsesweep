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
    client = LLMClient(api_key="test-key", model="gpt-4o-mini", provider="openai")
    response = SimpleNamespace(choices=[])

    with (
        patch("psweep.extraction.llm_client.completion", return_value=response),
        pytest.raises(ExtractionError, match="Empty response"),
    ):
        client.extract("sample text", {"type": "object", "properties": {}})


def test_llm_client_raises_on_provider_exception() -> None:
    client = LLMClient(api_key="test-key", model="gpt-4o-mini", provider="openai")

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

    with (
        patch("psweep.extraction.llm_client.completion") as completion_mock,
        pytest.raises(ExtractionError, match="context_window_exceeded"),
    ):
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
            RuntimeError("invalid_request_error: `temperature` is deprecated for this model."),
            response,
        ]
        with patch("psweep.extraction.llm_client.completion_cost", return_value=0.0):
            out = client.extract("sample text", {"type": "object", "properties": {}})

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
        patch("psweep.extraction.llm_client.completion", return_value=response),
        patch("psweep.extraction.llm_client.completion_cost", return_value=0.025),
        pytest.raises(ExtractionError) as caught,
    ):
        client.extract("text", {"type": "object"})
    assert caught.value.usage["cost"] == 0.025
    assert caught.value.usage["input_tokens"] == 13
    assert caught.value.usage["output_tokens"] == 7
    assert client.usage["cost"] == 0.025


def test_schema_rejection_preserves_provider_usage():
    """Post-call schema failure retains returned billing evidence for every generation."""
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
        ) as mocked,
        pytest.raises(Exception) as caught,
    ):
        extractor.extract("text", {"type": "object", "required": ["items"]})
    # The invalid answer earns one feedback generation; both are billed and visible.
    assert mocked.call_count == 2
    assert caught.value.usage == {
        "cost": 0.024,
        "input_tokens": 40,
        "output_tokens": 8,
    }


# --- Step 8: response outcomes and one bounded schema-feedback generation -----

import json  # noqa: E402

from psweep.exceptions import (  # noqa: E402
    AttemptBudgetExceeded,
    ResponseOutcomeError,
    SchemaValidationError,
)
from psweep.extraction.llm_client import RETRY_ATTEMPTS  # noqa: E402
from psweep.extraction.request_policy import AttemptAllowance  # noqa: E402
from psweep.utils.error_taxonomy import build_error_record  # noqa: E402

ITEMS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
}
SECRET = "SECRET-DOCUMENT-VALUE"  # noqa: S105 - sentinel, not a credential


def _answer(content, finish="stop", refusal=None, usage=(100, 10)):
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=message)],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
    )


def _run(replies, extractor=None, schema=ITEMS_SCHEMA, text="DOCUMENT BODY"):
    extractor = extractor or DocumentExtractor(api_key="k", model="gpt-4o-mini")
    with (
        patch("psweep.extraction.llm_client.completion", side_effect=replies) as sent,
        patch("psweep.extraction.llm_client.completion_cost", return_value=0.5),
        patch("psweep.extraction.llm_client.time.sleep"),
    ):
        try:
            return extractor.extract(text, schema), sent, None
        except Exception as exc:  # noqa: BLE001 - the tests inspect the outcome
            return None, sent, exc


def test_invalid_answer_gets_exactly_one_feedback_generation_on_the_same_contract():
    bad = _answer(json.dumps({"items": [1], "extra": SECRET}))
    good = _answer('{"items": ["a"]}')
    result, sent, error = _run([bad, good])
    assert error is None and result.data == {"items": ["a"]}
    first, second = (call.kwargs for call in sent.call_args_list)
    # Original contract and evidence are untouched; feedback only appends turns.
    assert second["messages"][:2] == first["messages"]
    assert second["response_format"] == first["response_format"]
    assert second.get("max_tokens") == first.get("max_tokens")
    assert [m["role"] for m in second["messages"][2:]] == ["assistant", "user"]
    feedback = second["messages"][-1]["content"]
    assert "additionalProperties" in feedback and "items" in feedback
    assert SECRET not in feedback  # diagnostics name paths and rules, not values
    # Both generations are billed and visible.
    assert result.cost == 1.0 and result.input_tokens == 200 and result.output_tokens == 20


def test_two_invalid_generations_fail_with_both_costs_and_no_third_call():
    _, sent, error = _run([_answer('{"items": [1]}') for _ in range(3)])
    assert isinstance(error, SchemaValidationError) and sent.call_count == 2
    assert error.usage == {"cost": 1.0, "input_tokens": 200, "output_tokens": 20}


@pytest.mark.parametrize(
    ("content", "first_code"),
    [(None, "response_empty"), ("sorry, no json", "response_not_json")],
)
def test_empty_and_non_json_answers_are_repaired_once(content, first_code):
    result, sent, error = _run([_answer(content), _answer('{"items": []}')])
    assert error is None and sent.call_count == 2 and result.cost == 1.0
    roles = [m["role"] for m in sent.call_args_list[1].kwargs["messages"]]
    assert roles[-1] == "user" and "assistant" not in roles  # nothing usable to echo
    _, _, error = _run([_answer(content), _answer(content)])
    assert isinstance(error, ResponseOutcomeError) and error.code == first_code
    assert error.usage["cost"] == 1.0


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (_answer('{"items": []}', finish="length"), "response_truncated"),
        (_answer('{"items": [1]}', finish="length"), "response_truncated"),
        (_answer(None, refusal="no"), "response_refused"),
        (_answer('{"items": [1]}', finish="content_filter"), "response_refused"),
    ],
)
def test_truncation_and_refusal_are_never_regenerated(reply, code):
    _, sent, error = _run([reply, _answer('{"items": ["a"]}')])
    assert sent.call_count == 1
    assert isinstance(error, ResponseOutcomeError) and error.code == code
    assert error.usage["cost"] == 0.5


def test_valid_abstention_and_unverifiable_quotes_are_not_regenerated():
    result, sent, error = _run([_answer('{"items": []}')])
    assert error is None and sent.call_count == 1 and result.data == {"items": []}
    # Authenticity is judged later against evidence; this layer never repairs it.
    result, sent, error = _run([_answer('{"items": ["fabricated quote"]}')])
    assert error is None and sent.call_count == 1


def test_transport_retries_share_the_per_document_allowance():
    class BusyError(RuntimeError):
        status_code = 503

    invalid = _answer('{"items": [1]}')
    # Three attempts consumed by transport retries leave one for the first answer
    # and none for a feedback generation.
    _, sent, error = _run(
        [BusyError("x"), BusyError("x"), BusyError("x"), invalid, _answer('{"items": []}')]
    )
    assert isinstance(error, SchemaValidationError)
    assert sent.call_count == RETRY_ATTEMPTS == 4
    # One transport retry leaves exactly the repair, which then cannot retry freely.
    _, sent, error = _run(
        [BusyError("x"), invalid, BusyError("x"), BusyError("x"), BusyError("x"), BusyError("x")]
    )
    assert isinstance(error, AttemptBudgetExceeded) and sent.call_count == RETRY_ATTEMPTS
    assert error.usage["cost"] == 0.5  # the billed first answer is not lost


def test_temperature_fallback_draws_from_the_same_allowance():
    class RejectedError(RuntimeError):
        pass

    rejected = RejectedError("invalid_request_error: `temperature` is deprecated")
    _, sent, error = _run([rejected, _answer('{"items": [1]}'), _answer('{"items": []}')])
    assert error is None and sent.call_count == 3


def test_allowance_is_document_local_not_stored_on_the_shared_client():
    extractor = DocumentExtractor(api_key="k", model="gpt-4o-mini")
    before = dict(vars(extractor.client))
    bad, good = _answer('{"items": [1]}'), _answer('{"items": []}')
    for _ in range(2):  # each document gets its own repair
        _, sent, error = _run([bad, good], extractor=extractor)
        assert error is None and sent.call_count == 2
    assert set(vars(extractor.client)) == set(before)


def test_attempt_allowance_reserves_until_exhausted():
    allowance = AttemptAllowance(2)
    assert allowance.remaining == 2
    allowance.reserve()
    allowance.reserve()
    assert allowance.remaining == 0
    with pytest.raises(AttemptBudgetExceeded):
        allowance.reserve()
    assert allowance.remaining == 0


def test_response_outcomes_have_explicit_error_records():
    record = build_error_record(
        ResponseOutcomeError("x", code="response_truncated"), stage="extract"
    )
    assert (record["category"], record["code"]) == ("model_response", "response_truncated")
    assert record["retryable"] is False


def test_schema_diagnostics_are_bounded_and_never_echo_values():
    from psweep.extraction.schema_utils import build_schema_validator, schema_diagnostics

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["a", "b"],
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "array", "items": {"type": "integer"}},
        },
    }
    data = {"a": 5, "b": [SECRET] * 20, "z" * 90: SECRET}
    lines = schema_diagnostics(build_schema_validator(schema).iter_errors(data))
    assert 1 < len(lines) <= 8 and all(len(line) <= 120 for line in lines)
    assert "a: type (expected string)" in lines
    assert not any(SECRET in line for line in lines)
    missing = schema_diagnostics(build_schema_validator(schema).iter_errors({"a": "x"}))
    assert missing == ["answer: required b"]
