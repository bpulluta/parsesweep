"""LLMClient.complete: the one request path for every LLM call."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import litellm
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
        choices=[
            SimpleNamespace(
                finish_reason=finish, message=SimpleNamespace(content=content)
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=usage[0], completion_tokens=usage[1]
        ),
    )


def _proxy(model="gemini-3.5-flash"):
    return LLMClient(api_key="k", model=model, base_url=BASE_URL)


def test_complete_routes_through_the_proxy_with_request_params():
    client = _proxy()
    with (
        patch(
            "psweep.extraction.llm_client.completion", return_value=_reply()
        ) as sent,
        patch(
            "psweep.extraction.llm_client.completion_cost", return_value=0.002
        ),
    ):
        reply = client.complete(
            MESSAGES, temperature=0, reasoning_effort="none"
        )
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
        patch(
            "psweep.extraction.llm_client.completion_cost", return_value=0.0
        ),
    ):
        out = client.extract("text", SCHEMA)
    assert out["data"] == {"items": []}
    assert calls == [
        {"temperature": 0, "response_format": {"type": "json_object"}}
    ]


def test_proxy_extraction_is_priced_as_the_configured_model():
    """A proxy route is unmapped; the configured model's price applies."""
    client = _proxy()
    response = _proxy_response()
    expected = sum(
        litellm.cost_per_token(
            model="gemini-3.5-flash", usage_object=response.usage
        )
    )
    with patch(
        "psweep.extraction.llm_client.completion", return_value=response
    ):
        out = client.extract("text", SCHEMA)
    assert expected > 0
    assert out["cost"] == pytest.approx(expected)
    assert client.usage["cost"] == pytest.approx(expected)
    assert client.usage["unknown_cost_calls"] == 0


def test_direct_routes_never_borrow_another_price():
    client = LLMClient(
        api_key="k", model="gemini-3.5-flash", provider="openai"
    )

    def per_token(**_):
        raise AssertionError("direct routes are priced by litellm only")

    with (
        patch(
            "psweep.extraction.llm_client.completion", return_value=_reply()
        ),
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
        patch(
            "psweep.extraction.llm_client.completion", return_value=response
        ),
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
    failure = RuntimeError(
        "Connection error. internal-host.example secret-token"
    )
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
        patch(
            "psweep.extraction.llm_client.completion", return_value=response
        ),
        patch(
            "psweep.extraction.llm_client.completion_cost", return_value=0.01
        ),
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
    monkeypatch.setattr(
        "psweep.extraction.llm_client.time.sleep", lambda _: None
    )

    class RateLimitError(RuntimeError):
        status_code = 429

    client = _proxy()
    with (
        patch(
            "psweep.extraction.llm_client.completion",
            side_effect=[RateLimitError("slow down"), _reply()],
        ) as sent,
        patch(
            "psweep.extraction.llm_client.completion_cost", return_value=0.0
        ),
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
def test_only_transient_provider_failures_are_retried(
    monkeypatch, failure, sent_calls
):
    monkeypatch.setattr(
        "psweep.extraction.llm_client.time.sleep", lambda _: None
    )
    client = _proxy()
    with (
        patch(
            "psweep.extraction.llm_client.completion",
            side_effect=[failure, _reply()],
        ) as sent,
        patch(
            "psweep.extraction.llm_client.completion_cost", return_value=0.0
        ),
    ):
        if sent_calls == 1:
            with pytest.raises(ExtractionError):
                client.complete(MESSAGES)
        else:
            assert client.complete(MESSAGES).content == "ok"
    assert sent.call_count == sent_calls
