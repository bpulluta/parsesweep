"""Request admission: pure policy plus the central guard in LLMClient.complete."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from psweep.extraction.llm_client import LLMClient
from psweep.extraction.request_policy import (
    RequestNotAdmittedError,
    RouteLimits,
    assess_request,
    estimate_request_tokens,
    estimate_text_tokens,
    route_limits,
)

BASE_URL = "https://proxy.example.invalid/v1"


def _msgs(text):
    return [{"role": "user", "content": text}]


def test_estimate_is_conservative_for_ascii_and_unicode():
    assert estimate_text_tokens("a" * 300) >= 75  # ~4 chars/token real-world
    assert estimate_text_tokens("日" * 100) >= 100  # ~1 token/char worst case


def test_schema_overhead_counts_toward_the_request():
    schema = {"type": "json_schema", "json_schema": {"schema": {"x": "y" * 3000}}}
    assert estimate_request_tokens(_msgs("hi"), schema) > estimate_request_tokens(_msgs("hi")) + 900


def test_schema_alone_can_exceed_the_input_limit():
    schema = {"type": "json_schema", "json_schema": {"schema": {"x": "y" * 3000}}}
    with pytest.raises(RequestNotAdmittedError, match="context_window_exceeded"):
        assess_request(_msgs("hi"), schema, RouteLimits(input_tokens=500))


def test_equality_boundary_is_admitted_and_one_over_is_not():
    messages = _msgs("a" * 30)
    need = estimate_request_tokens(messages)
    assert assess_request(messages, None, RouteLimits(input_tokens=need)) == need
    with pytest.raises(RequestNotAdmittedError):
        assess_request(messages, None, RouteLimits(input_tokens=need - 1))


def test_unknown_limits_do_not_pretend_to_be_large_or_small():
    assert route_limits("not-a-real-model-xyz") == RouteLimits()
    assert assess_request(_msgs("a" * 10_000_000), None, RouteLimits()) > 0


def test_configured_window_wins_over_catalog():
    assert route_limits("gpt-4o", configured_windows={"gpt-4o": 1234}).input_tokens == 1234


def test_catalog_supplies_input_and_output_limits_offline():
    limits = route_limits("gpt-4o")
    assert limits.input_tokens and limits.output_tokens


def test_output_cap_cannot_exceed_route_output_limit():
    with pytest.raises(RequestNotAdmittedError, match="output cap"):
        assess_request(_msgs("hi"), None, RouteLimits(output_tokens=100), output_cap=101)


def test_typed_content_has_no_small_default_estimate():
    blocks = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]
    with pytest.raises(RequestNotAdmittedError, match="no known token estimate"):
        estimate_request_tokens(blocks)


def _client(**kwargs):
    return LLMClient(api_key="k", model="gpt-4o", base_url=BASE_URL, **kwargs)


def _reply():
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}"))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


def test_oversized_request_never_reaches_the_provider_or_the_meter():
    client = _client(context_windows={"gpt-4o": 1000})
    with (
        patch.object(client, "_complete") as send,
        pytest.raises(RequestNotAdmittedError, match="context_window_exceeded"),
    ):
        client.complete(_msgs("x" * 100_000))
    send.assert_not_called()
    assert client.usage_snapshot["calls"] == 0


def test_admitted_extraction_request_is_capped_at_the_route_output_limit():
    client = _client()
    with patch.object(client, "_complete", return_value=_reply()) as send:
        client.complete(_msgs("hi"), admit=True)
    assert send.call_args.args[0]["max_tokens"] == route_limits("gpt-4o").output_tokens


def test_admitted_request_on_a_route_with_no_known_output_limit_is_sent_uncapped():
    client = LLMClient(api_key="k", model="my-proxy-alias", base_url=BASE_URL)
    with patch.object(client, "_complete", return_value=_reply()) as send:
        client.complete(_msgs("hi"), admit=True)
    assert "max_tokens" not in send.call_args.args[0]


def test_non_extraction_callers_get_no_output_cap_or_catalog_guard():
    client = _client()
    with patch.object(client, "_complete", return_value=_reply()) as send:
        client.complete(_msgs("x" * 2_000_000))  # beyond gpt-4o's catalog window
    assert "max_tokens" not in send.call_args.args[0]


def test_catalog_guard_applies_to_admitted_requests():
    client = _client()
    with (
        patch.object(client, "_complete") as send,
        pytest.raises(RequestNotAdmittedError),
    ):
        client.complete(_msgs("x" * 2_000_000), admit=True)
    send.assert_not_called()
