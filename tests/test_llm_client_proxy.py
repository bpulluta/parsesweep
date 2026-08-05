"""Tests for LLMClient proxy/base_url routing and response-handling robustness.

Covers the three-part fix for litellm.nlr.gov-style proxy deployments:
  1. openai/ prefix in _format_model_for_litellm forces /chat/completions routing.
  2. response_format is omitted in proxy mode (proxy support varies per model).
  3. Response content is stripped before empty-check and JSON parsing.

These tests are proxy-agnostic and apply to any OpenAI-compatible endpoint:
LiteLLM proxy, OpenRouter, vLLM, Azure AI Foundry, etc.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

from psweep.extraction.llm_client import LLMClient
from psweep.exceptions import ExtractionError


BASE_URL = "https://proxy.example.com/v1"
SCHEMA = {"type": "object", "properties": {}}


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_response(content):
    """Build a minimal litellm-style response object."""
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=content))]
    resp.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    return resp


def _client(model, **kwargs):
    return LLMClient(api_key="test-key", model=model, base_url=BASE_URL, **kwargs)


# ── _format_model_for_litellm in proxy mode ──────────────────────────────────


class TestProxyModelFormatting:
    """LiteLLM must use /chat/completions regardless of model name origin."""

    def test_bare_model_gets_openai_prefix(self):
        # "claude-haiku-4-5" → LiteLLM model "openai/claude-haiku-4-5"
        # LiteLLM strips prefix, proxy receives "claude-haiku-4-5"
        assert _client("claude-haiku-4-5").model == "openai/claude-haiku-4-5"

    def test_bare_openai_model_gets_prefix(self):
        assert _client("gpt-4o-mini").model == "openai/gpt-4o-mini"

    def test_anthropic_prefixed_model_wrapped_for_openrouter(self):
        # OpenRouter-style: "anthropic/claude-3-sonnet" must stay intact at the
        # proxy. We wrap with openai/ so LiteLLM strips it and forwards the rest.
        assert (
            _client("anthropic/claude-3-sonnet").model
            == "openai/anthropic/claude-3-sonnet"
        )

    def test_gemini_prefixed_model_wrapped(self):
        assert (
            _client("gemini/gemini-pro").model == "openai/gemini/gemini-pro"
        )

    def test_already_openai_prefixed_is_idempotent(self):
        # User who already puts "openai/" in their config: no double-wrap.
        assert _client("openai/gpt-4o").model == "openai/gpt-4o"

    def test_no_proxy_bare_model_unchanged(self):
        # Direct-provider mode: bare model names pass through untouched.
        c = LLMClient(api_key="k", model="gpt-4o-mini", provider="openai")
        assert c.model == "gpt-4o-mini"

    def test_no_proxy_anthropic_model_unchanged(self):
        c = LLMClient(api_key="k", model="claude-haiku-4-5", provider="anthropic")
        assert c.model == "claude-haiku-4-5"

    def test_no_proxy_azure_model_gets_azure_prefix(self):
        c = LLMClient(api_key="k", model="my-deploy", provider="azure")
        assert c.model == "azure/my-deploy"

    def test_no_proxy_gemini_model_gets_gemini_prefix(self):
        c = LLMClient(api_key="k", model="gemini-pro", provider="gemini")
        assert c.model == "gemini/gemini-pro"


# ── api_params sent to litellm.completion ────────────────────────────────────


class TestProxyApiParams:
    """Verify the exact kwargs passed to litellm.completion in proxy mode."""

    def _extract_with_capture(self, model, **client_kwargs):
        client = _client(model, **client_kwargs)
        ok_response = _make_response(json.dumps({"items": []}))
        with patch("psweep.extraction.llm_client.completion", return_value=ok_response) as mock_c, \
             patch("psweep.extraction.llm_client.completion_cost", return_value=0.0):
            client.extract("some text", SCHEMA)
        return mock_c.call_args[1] if mock_c.call_args[1] else mock_c.call_args[0][0]

    def test_base_url_forwarded(self):
        params = self._extract_with_capture("gpt-4o-mini")
        assert params.get("base_url") == BASE_URL

    def test_api_key_forwarded(self):
        params = self._extract_with_capture("gpt-4o-mini")
        assert params.get("api_key") == "test-key"

    def test_no_custom_llm_provider_in_params(self):
        # custom_llm_provider is redundant now that the model carries openai/ prefix.
        params = self._extract_with_capture("claude-haiku-4-5")
        assert "custom_llm_provider" not in params

    def test_response_format_sent_in_proxy_mode(self):
        # response_format is a standard OpenAI-compatible param. Always send it:
        # LiteLLM proxies translate it to each backend's native JSON mode, and
        # litellm.drop_params=True silently drops it for backends that don't
        # support it. Without it, Claude-family models prepend non-JSON preamble.
        params = self._extract_with_capture("claude-haiku-4-5")
        assert params.get("response_format") == {"type": "json_object"}

    def test_temperature_still_set_for_non_reasoning_models(self):
        params = self._extract_with_capture("claude-haiku-4-5")
        assert params.get("temperature") == 0

    def test_reasoning_model_omits_temperature(self):
        # Reasoning model detection uses the formatted model name.
        # "openai/gpt-5-turbo" contains "gpt-5" → is_reasoning_model=True.
        params = self._extract_with_capture("gpt-5-turbo")
        assert "temperature" not in params
        assert "response_format" not in params

    def test_no_api_key_in_params_when_not_provided(self):
        client = LLMClient(model="gpt-4o-mini", base_url=BASE_URL)
        ok_response = _make_response(json.dumps({"items": []}))
        with patch("psweep.extraction.llm_client.completion", return_value=ok_response) as mock_c, \
             patch("psweep.extraction.llm_client.completion_cost", return_value=0.0):
            client.extract("text", SCHEMA)
        params = mock_c.call_args[1] if mock_c.call_args[1] else mock_c.call_args[0][0]
        assert "api_key" not in params

    def test_timeout_in_params(self):
        # timeout must always be forwarded to LiteLLM so hung proxies don't block forever.
        params = self._extract_with_capture("gpt-4o-mini")
        assert "timeout" in params
        assert isinstance(params["timeout"], int)

    def test_timeout_constructor_kwarg_overrides_default(self):
        client = LLMClient(model="gpt-4o-mini", base_url=BASE_URL, timeout=30)
        assert client.timeout == 30
        ok_response = _make_response(json.dumps({"items": []}))
        with patch("psweep.extraction.llm_client.completion", return_value=ok_response) as mock_c, \
             patch("psweep.extraction.llm_client.completion_cost", return_value=0.0):
            client.extract("text", SCHEMA)
        params = mock_c.call_args[1] if mock_c.call_args[1] else mock_c.call_args[0][0]
        assert params["timeout"] == 30

    def test_timeout_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("LLM_TIMEOUT", "45")
        client = LLMClient(model="gpt-4o-mini", base_url=BASE_URL)
        assert client.timeout == 45

    def test_default_timeout_applied_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("LLM_TIMEOUT", raising=False)
        client = LLMClient(model="gpt-4o-mini", base_url=BASE_URL)
        assert client.timeout == LLMClient.DEFAULT_TIMEOUT

    def test_direct_provider_sends_response_format(self):
        # Non-proxy mode (no base_url): response_format is sent for non-reasoning models.
        client = LLMClient(api_key="k", model="gpt-4o-mini", provider="openai")
        ok_response = _make_response(json.dumps({"items": []}))
        with patch("psweep.extraction.llm_client.completion", return_value=ok_response) as mock_c, \
             patch("psweep.extraction.llm_client.completion_cost", return_value=0.0):
            client.extract("text", SCHEMA)
        params = mock_c.call_args[1] if mock_c.call_args[1] else mock_c.call_args[0][0]
        assert params.get("response_format") == {"type": "json_object"}




# ── response content robustness ───────────────────────────────────────────────


class TestMarkdownFenceStripping:
    """_clean_json_content must strip fences that Claude/proxy adds."""

    def _clean(self, raw):
        return LLMClient._clean_json_content(raw)

    def test_plain_json_unchanged(self):
        assert self._clean('{"ok":true}') == '{"ok":true}'

    def test_json_fence_stripped(self):
        assert self._clean('```json\n{"ok":true}\n```') == '{"ok":true}'

    def test_bare_fence_stripped(self):
        assert self._clean('```\n{"ok":true}\n```') == '{"ok":true}'

    def test_fence_without_trailing_newline(self):
        assert self._clean('```json\n{"ok":true}```') == '{"ok":true}'

    def test_fence_with_surrounding_whitespace(self):
        assert self._clean('  ```json\n{"ok":true}\n```  ') == '{"ok":true}'

    def test_multiline_json_fence_stripped(self):
        raw = '```json\n{\n  "items": [1, 2]\n}\n```'
        result = self._clean(raw)
        assert json.loads(result) == {"items": [1, 2]}

    def test_empty_returns_empty(self):
        assert self._clean("") == ""
        assert self._clean(None) == ""
        assert self._clean("   ") == ""

    def test_extraction_succeeds_with_fenced_response(self):
        """End-to-end: a model that wraps JSON in fences still produces valid data."""
        client = _client("claude-haiku-4-5")
        fenced = '```json\n{"items": [{"name": "test"}]}\n```'
        resp = _make_response(fenced)
        with patch("psweep.extraction.llm_client.completion", return_value=resp), \
             patch("psweep.extraction.llm_client.completion_cost", return_value=0.0):
            out = client.extract("text", SCHEMA)
        assert out["data"] == {"items": [{"name": "test"}]}


class TestResponseContentHandling:
    """Strip-before-check prevents whitespace confusion; all empty variants raise."""

    def _run(self, content):
        client = _client("gpt-4o-mini")
        resp = _make_response(content)
        with patch("psweep.extraction.llm_client.completion", return_value=resp), \
             patch("psweep.extraction.llm_client.completion_cost", return_value=0.0):
            return client.extract("text", SCHEMA)

    def test_valid_json_succeeds(self):
        out = self._run(json.dumps({"items": [1, 2]}))
        assert out["data"] == {"items": [1, 2]}

    def test_valid_json_with_surrounding_whitespace_succeeds(self):
        # Content with leading/trailing whitespace must still parse correctly.
        out = self._run("  " + json.dumps({"items": []}) + "\n")
        assert out["data"] == {"items": []}

    def test_empty_string_raises_extraction_error(self):
        with pytest.raises(ExtractionError, match="Empty response"):
            self._run("")

    def test_whitespace_only_raises_extraction_error(self):
        # Previously passed `not content` check but failed json.loads at char 0.
        with pytest.raises(ExtractionError, match="Empty response"):
            self._run("   \n\t  ")

    def test_none_content_raises_extraction_error(self):
        with pytest.raises(ExtractionError, match="Empty response"):
            self._run(None)

    def test_empty_choices_raises_extraction_error(self):
        client = _client("gpt-4o-mini")
        resp = MagicMock()
        resp.choices = []
        with patch("psweep.extraction.llm_client.completion", return_value=resp):
            with pytest.raises(ExtractionError, match="Empty response"):
                client.extract("text", SCHEMA)

    def test_invalid_json_raises_extraction_error(self):
        # Model returned non-JSON text (preamble, markdown fence, etc.)
        with pytest.raises(ExtractionError, match="non-JSON content"):
            self._run("not valid json {{{")


# ── proxy JSON instruction injection ─────────────────────────────────────────


class TestJsonPromptInjection:
    """In proxy mode the JSON instruction must appear in the messages."""

    def _capture_messages(self, model, user_text="some text"):
        client = _client(model)
        ok_response = _make_response(json.dumps({"items": []}))
        with patch("psweep.extraction.llm_client.completion", return_value=ok_response) as mock_c, \
             patch("psweep.extraction.llm_client.completion_cost", return_value=0.0):
            client.extract(user_text, SCHEMA)
        params = mock_c.call_args[1] if mock_c.call_args[1] else mock_c.call_args[0][0]
        return params.get("messages", [])

    def test_json_instruction_injected_when_absent(self):
        messages = self._capture_messages("claude-haiku-4-5", user_text="extract data")
        all_content = " ".join(m["content"] for m in messages).lower()
        assert "json" in all_content

    def test_json_instruction_not_duplicated_when_already_present(self):
        messages = self._capture_messages(
            "claude-haiku-4-5", user_text="return JSON output"
        )
        combined = " ".join(m["content"] for m in messages).lower()
        # Only one occurrence of the injection phrase (not doubled).
        assert combined.count("respond with a json object") <= 1
