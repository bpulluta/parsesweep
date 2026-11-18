"""LLMClient.complete: the one request path for every LLM call."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import litellm
import openai
import pytest

from psweep.exceptions import ExtractionError
from psweep.extraction.llm_client import Completion, LLMClient

BASE_URL = "https://proxy.example.invalid/v1"
SCHEMA = {"type": "object", "properties": {}}
MESSAGES = [{"role": "user", "content": "hi"}]


def _proxy_response(model="gemini-3.5-flash", content='{"items": []}'):
    """A reply as litellm builds it for an ``openai/`` proxy route."""
    response = litellm.ModelResponse(
        model=model,
        choices=[
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        usage=litellm.Usage(prompt_tokens=1500, completion_tokens=170),
    )
    response._hidden_params = {"custom_llm_provider": "openai", "model": model}
    return response


def _reply(content="ok", finish="stop", usage=(10, 5)):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
    )


def _proxy(model="gemini-3.5-flash"):
    return LLMClient(api_key="k", model=model, base_url=BASE_URL)


def test_usage_snapshot_cannot_mutate_client_counters():
    client = _proxy()
    snapshot = client.usage_snapshot
    snapshot["calls"] = 999
    client._count(calls=1, input_tokens=10)
    assert snapshot["calls"] == 999
    assert client.usage_snapshot["calls"] == 1
    assert client.usage_snapshot["input_tokens"] == 10


def test_complete_routes_through_the_proxy_with_request_params():
    client = _proxy()
    with (
        patch("psweep.extraction.llm_client.completion", return_value=_reply()) as sent,
        patch("psweep.extraction.llm_client.completion_cost", return_value=0.002),
    ):
        reply = client.complete(MESSAGES, temperature=0, reasoning_effort="none")
    params = sent.call_args.kwargs
    assert params["model"] == "openai/gemini-3.5-flash"
    assert params["base_url"] == BASE_URL
    assert params["api_key"] == "k"
    assert params["messages"] == MESSAGES
    assert params["temperature"] == 0
    assert params["reasoning_effort"] == "none"
    assert params["timeout"] == client.timeout
    assert "custom_llm_provider" not in params
    assert reply == Completion(
        content="ok",
        finish_reason="stop",
        usage={"cost": 0.002, "input_tokens": 10, "output_tokens": 5},
    )
    assert client.usage == {
        "calls": 1,
        "cost": 0.002,
        "input_tokens": 10,
        "output_tokens": 5,
        "unknown_cost_calls": 0,
    }


def test_extract_sends_its_request_through_complete(monkeypatch):
    client = _proxy("gpt-4o-mini")
    calls = []
    original = LLMClient.complete

    def spy(self, messages, **params):
        calls.append(params)
        return original(self, messages, **params)

    monkeypatch.setattr(LLMClient, "complete", spy)
    with (
        patch(
            "psweep.extraction.llm_client.completion",
            return_value=_reply('{"items": []}'),
        ),
        patch("psweep.extraction.llm_client.completion_cost", return_value=0.0),
    ):
        out = client.extract("text", SCHEMA)
    assert out["data"] == {"items": []}
    assert calls == [{"temperature": 0, "response_format": {"type": "json_object"}}]


def test_proxy_extraction_is_priced_as_the_configured_model():
    """A proxy route is unmapped; the configured model's price applies."""
    client = _proxy()
    response = _proxy_response()
    expected = sum(litellm.cost_per_token(model="gemini-3.5-flash", usage_object=response.usage))
    with patch("psweep.extraction.llm_client.completion", return_value=response):
        out = client.extract("text", SCHEMA)
    assert expected > 0
    assert out["cost"] == pytest.approx(expected)
    assert client.usage["cost"] == pytest.approx(expected)
    assert client.usage["unknown_cost_calls"] == 0


def test_direct_routes_never_borrow_another_price():
    client = LLMClient(api_key="k", model="gemini-3.5-flash", provider="openai")

    def per_token(**_):
        raise AssertionError("direct routes are priced by litellm only")

    with (
        patch("psweep.extraction.llm_client.completion", return_value=_reply()),
        patch(
            "psweep.extraction.llm_client.completion_cost",
            side_effect=ValueError("unmapped"),
        ),
        patch("psweep.extraction.llm_client.cost_per_token", per_token),
    ):
        reply = client.complete(MESSAGES)
    assert reply.usage["cost"] is None
    assert client.usage["unknown_cost_calls"] == 1


@pytest.mark.parametrize(
    ("model", "usage"),
    [
        (
            "my-private-deployment",
            SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        ),
        ("gemini-3.5-flash", None),
    ],
)
def test_unpriced_or_unmetered_proxy_replies_are_unknown_cost(model, usage):
    client = _proxy(model)
    response = _reply()
    response.usage = usage
    with (
        patch("psweep.extraction.llm_client.completion", return_value=response),
        patch(
            "psweep.extraction.llm_client.completion_cost",
            side_effect=ValueError("unmapped"),
        ),
    ):
        reply = client.complete(MESSAGES)
    assert reply.usage["cost"] is None
    assert client.usage["cost"] == 0
    assert client.usage["unknown_cost_calls"] == 1


def test_provider_failures_are_sanitized():
    client = _proxy()
    failure = RuntimeError("Connection error. internal-host.example secret-token")
    with (
        patch("psweep.extraction.llm_client.completion", side_effect=failure),
        pytest.raises(ExtractionError) as caught,
    ):
        client.complete(MESSAGES)
    message = str(caught.value)
    assert "connection error" in message
    assert "internal-host" not in message
    assert "secret-token" not in message
    assert caught.value.__suppress_context__
    assert client.usage["calls"] == 1
    assert client.usage["unknown_cost_calls"] == 1


def test_unparseable_proxy_body_is_an_unknown_cost_failure():
    client = _proxy()
    with (
        patch(
            "psweep.extraction.llm_client.completion",
            side_effect=json.JSONDecodeError("bad", "", 0),
        ),
        pytest.raises(ExtractionError, match="non-JSON response body"),
    ):
        client.complete(MESSAGES)
    assert client.usage["unknown_cost_calls"] == 1


def test_a_reply_without_choices_fails_with_its_usage():
    client = _proxy()
    response = _reply()
    response.choices = []
    with (
        patch("psweep.extraction.llm_client.completion", return_value=response),
        patch("psweep.extraction.llm_client.completion_cost", return_value=0.01),
        pytest.raises(ExtractionError, match="Empty response") as caught,
    ):
        client.complete(MESSAGES)
    assert caught.value.usage == {
        "cost": 0.01,
        "input_tokens": 10,
        "output_tokens": 5,
    }
    assert client.usage["cost"] == 0.01


def test_rate_limits_are_retried_and_counted(monkeypatch):
    monkeypatch.setattr("psweep.extraction.llm_client.time.sleep", lambda _: None)

    class RateLimitError(RuntimeError):
        status_code = 429

    client = _proxy()
    with (
        patch(
            "psweep.extraction.llm_client.completion",
            side_effect=[RateLimitError("slow down"), _reply()],
        ) as sent,
        patch("psweep.extraction.llm_client.completion_cost", return_value=0.0),
    ):
        reply = client.complete(MESSAGES)
    assert reply.content == "ok"
    assert sent.call_count == 2
    assert client.usage["calls"] == 2
    assert client.usage["unknown_cost_calls"] == 1


@pytest.mark.parametrize(
    ("failure", "sent_calls"),
    [
        (litellm.ServiceUnavailableError("busy", "openai", "m"), 2),
        (litellm.APIConnectionError("reset", "openai", "m"), 2),
        (litellm.BadRequestError("bad", "m", "openai"), 1),
    ],
)
def test_only_transient_provider_failures_are_retried(monkeypatch, failure, sent_calls):
    monkeypatch.setattr("psweep.extraction.llm_client.time.sleep", lambda _: None)
    client = _proxy()
    with (
        patch(
            "psweep.extraction.llm_client.completion",
            side_effect=[failure, _reply()],
        ) as sent,
        patch("psweep.extraction.llm_client.completion_cost", return_value=0.0),
    ):
        if sent_calls == 1:
            with pytest.raises(ExtractionError):
                client.complete(MESSAGES)
        else:
            assert client.complete(MESSAGES).content == "ok"
    assert sent.call_count == sent_calls


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (
            litellm.Timeout("sk-123 too slow", "m", "openai"),
            "did not finish within the 90s timeout",
        ),
        (
            litellm.ServiceUnavailableError("busy https://internal.example", "openai", "m"),
            "request failed: provider failure (ServiceUnavailableError)",
        ),
    ],
)
def test_retries_are_logged_without_provider_text(monkeypatch, caplog, failure, reason):
    """A slow or failing call stays visible while it retries; provider text never does."""
    monkeypatch.setattr("psweep.extraction.llm_client.time.sleep", lambda _: None)
    client = LLMClient(api_key="k", model="gemini-3.5-flash", base_url=BASE_URL, timeout=90)
    with (
        patch(
            "psweep.extraction.llm_client.completion",
            side_effect=[failure, failure, _reply()],
        ),
        patch("psweep.extraction.llm_client.completion_cost", return_value=0.0),
        caplog.at_level(logging.DEBUG, logger="psweep"),
    ):
        assert client.complete(MESSAGES).content == "ok"
    logged = [r for r in caplog.records if r.name == "psweep.extraction.llm_client"]
    retry = f"gemini-3.5-flash {reason}; retrying in"
    assert [r.getMessage() for r in logged if r.levelno == logging.WARNING] == [
        f"{retry} 2s (attempt 2/4)",
        f"{retry} 4s (attempt 3/4)",
    ]
    debug = [r.getMessage() for r in logged if r.levelno == logging.DEBUG]
    assert "Sending gemini-3.5-flash request (2 prompt chars, 90s timeout)" in debug
    answered = [m for m in debug if m.startswith("gemini-3.5-flash answered in ")]
    assert len(answered) == 1 and answered[0].endswith("s (10 prompt, 5 output tokens)")
    assert not any("sk-123" in r.getMessage() or "://" in r.getMessage() for r in logged)


def test_exhausted_timeouts_are_named(monkeypatch):
    monkeypatch.setattr("psweep.extraction.llm_client.time.sleep", lambda _: None)
    timeout = openai.APITimeoutError(request=httpx.Request("POST", BASE_URL))
    with (
        patch("psweep.extraction.llm_client.completion", side_effect=timeout),
        patch("psweep.extraction.llm_client.completion_cost", return_value=0.0),
        pytest.raises(ExtractionError, match=r"failed: timed out \(APITimeoutError\)"),
    ):
        _proxy().complete(MESSAGES)


@pytest.mark.parametrize(
    ("failure", "retryable"),
    [
        (litellm.RateLimitError("secret-key sk-123 slow down", "openai", "m"), True),
        (litellm.ServiceUnavailableError("busy https://internal.example", "openai", "m"), True),
        (litellm.Timeout("took too long", "m", "openai"), True),
        (litellm.AuthenticationError("bad key sk-123", "openai", "m"), False),
    ],
)
def test_extraction_error_records_classify_provider_failures(monkeypatch, failure, retryable):
    """Error records judge the provider failure, not the sanitized message."""
    from psweep.utils.error_taxonomy import build_error_record

    monkeypatch.setattr("psweep.extraction.llm_client.time.sleep", lambda _: None)
    client = _proxy()
    with (
        patch("psweep.extraction.llm_client.completion", side_effect=failure),
        patch("psweep.extraction.llm_client.completion_cost", return_value=0.0),
        pytest.raises(ExtractionError) as caught,
    ):
        client.complete(MESSAGES)

    record = build_error_record(caught.value, stage="extract")
    assert record["retryable"] is retryable
    assert record["category"] == "extraction"
    assert record["message"].startswith("LLM request failed: ")
    assert "sk-123" not in record["message"] and "://" not in record["message"]


# --- Step 8: explicit response outcomes (opt-in, extraction only) -------------


def _extract(client, reply, **kwargs):
    with (
        patch("psweep.extraction.llm_client.completion", return_value=reply) as sent,
        patch("psweep.extraction.llm_client.completion_cost", return_value=0.02),
    ):
        return client.extract("text", {"type": "object", "properties": {}}, **kwargs), sent


@pytest.mark.parametrize(
    ("finish", "refusal", "code"),
    [
        ("length", None, "response_truncated"),
        ("stop", "I cannot help with that", "response_refused"),
        ("content_filter", None, "response_refused"),
    ],
)
def test_strict_extraction_rejects_truncated_and_refused_answers_with_usage(finish, refusal, code):
    from psweep.exceptions import ResponseOutcomeError

    client = _proxy()
    reply = _reply(content='{"items": []}', finish=finish)
    reply.choices[0].message.refusal = refusal
    with pytest.raises(ResponseOutcomeError) as caught:
        _extract(client, reply, strict_outcomes=True)
    assert caught.value.code == code
    assert not caught.value.repairable
    assert caught.value.usage == {"cost": 0.02, "input_tokens": 10, "output_tokens": 5}
    assert "cannot help" not in str(caught.value)  # provider text is never echoed
    assert client.usage["cost"] == 0.02


def test_other_callers_keep_accepting_length_terminated_json():
    """Discovery, reconciliation and judges do not opt in; their semantics are unchanged."""
    out, _ = _extract(_proxy(), _reply(content='{"items": []}', finish="length"))
    assert out["data"] == {"items": []}


@pytest.mark.parametrize(
    ("content", "code"), [(None, "response_empty"), ("not json", "response_not_json")]
)
def test_strict_extraction_classifies_unusable_answers_as_repairable(content, code):
    from psweep.exceptions import ResponseOutcomeError

    with pytest.raises(ResponseOutcomeError) as caught:
        _extract(_proxy(), _reply(content=content), strict_outcomes=True)
    assert (caught.value.code, caught.value.repairable) == (code, True)
    assert caught.value.usage["cost"] == 0.02


@pytest.mark.parametrize("content", [None, "not json"])
def test_non_strict_unusable_answers_stay_plain_extraction_errors(content):
    from psweep.exceptions import ResponseOutcomeError

    with pytest.raises(ExtractionError) as caught:
        _extract(_proxy(), _reply(content=content))
    assert not isinstance(caught.value, ResponseOutcomeError)


def test_follow_up_messages_follow_the_unchanged_original_request():
    follow_up = [{"role": "user", "content": "Fix: items required."}]
    _, sent = _extract(_proxy(), _reply(content="{}"), follow_up=follow_up)
    plain_messages = _extract(_proxy(), _reply(content="{}"))[1].call_args.kwargs["messages"]
    messages = sent.call_args.kwargs["messages"]
    assert messages[:-1] == plain_messages
    assert messages[-1] == follow_up[0]


def test_extraction_prompt_states_schema_authorized_conversion_precedence():
    prompt = LLMClient._build_extraction_prompt("BODY", SCHEMA)
    lowered = prompt.lower()
    assert "unless a schema field description explicitly requires" in lowered
    assert "quote" in lowered and "verbatim" in lowered
    assert "never invent" in lowered
