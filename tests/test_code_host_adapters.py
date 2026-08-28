"""Tests for code-host content adapters (Municode server-API capture).

The adapter is pure: it takes an injected ``get(url, headers) -> (status, text)``
callable, so these tests drive the full resolution chain with canned JSON and
never touch the network.
"""

from __future__ import annotations

import json

from psweep.discovery.code_host_adapters import (
    MunicodeAdapter,
    _strip_html,
    resolve_adapter,
)

_PUBLIC_URL = (
    "https://library.municode.com/tx/friendswood/codes/code_of_ordinances"
    "?nodeId=PTIICICO_CH26EN_ARTVIIOIGA"
)


def _friendswood_get(calls: list[tuple[str, dict]] | None = None):
    """Return a fake ``get`` that answers the Friendswood chain."""
    responses = {
        "/api/Clients/stateAbbr?stateAbbr=tx": [
            {"ClientID": 1, "ClientName": "Frisco"},
            {"ClientID": 2291, "ClientName": "Friendswood"},
        ],
        "/api/ClientContent/2291": {"codes": [{"productId": 13883}]},
        "/api/Jobs/latest/13883": {"Id": 481221},
    }

    def _get(url: str, headers: dict) -> tuple[int, str]:
        if calls is not None:
            calls.append((url, headers))
        if "CodesContent" in url:
            assert "nodeId=PTIICICO_CH26EN_ARTVIIOIGA" in url
            assert "groupChunks=true" in url
            body = {
                "Docs": [
                    {
                        "Content": (
                            "<div><h2>Sec. 26-290. Natural gas compressor "
                            "stations.</h2><p>A minimum building setback of "
                            "<b>500&nbsp;feet</b> from residential districts "
                            "applies to every compressor station.</p></div>"
                        )
                    }
                ]
            }
            return 200, json.dumps(body)
        for suffix, payload in responses.items():
            if url.endswith(suffix):
                return 200, json.dumps(payload)
        return 404, ""

    return _get


class TestCanHandle:
    def test_matches_municode_hosts(self):
        a = MunicodeAdapter()
        assert a.can_handle(_PUBLIC_URL)
        assert a.can_handle("https://library.municode.com/tx/keller/codes/x")
        # Only the content host is claimed; marketing/bare hosts fall through
        # to the browser rather than being mis-parsed.
        assert not a.can_handle("https://www.municode.com/x")
        assert not a.can_handle("https://municode.com/x")
        assert not a.can_handle("https://ecode360.com/12345")
        assert not a.can_handle("https://example.com/tx/friendswood")

    def test_resolve_adapter_selects_municode_only(self):
        assert isinstance(resolve_adapter(_PUBLIC_URL), MunicodeAdapter)
        assert resolve_adapter("https://ecode360.com/12345") is None


class TestFetchText:
    def test_happy_path_returns_clean_text(self):
        text = MunicodeAdapter().fetch_text(_PUBLIC_URL, _friendswood_get())
        assert text is not None
        assert "compressor" in text.lower()
        assert "500" in text and "feet" in text
        # HTML tags stripped, entities decoded (no &nbsp;, no <b>).
        assert "<" not in text and ">" not in text
        assert "&nbsp;" not in text

    def test_sends_csrf_header_and_generic_ua(self):
        calls: list[tuple[str, dict]] = []
        MunicodeAdapter().fetch_text(_PUBLIC_URL, _friendswood_get(calls))
        assert calls, "adapter made no HTTP calls"
        for _url, headers in calls:
            assert headers.get("X-CSRF") == "1"
            ua = headers.get("User-Agent", "")
            assert "ParseSweep" in ua
            # Never a named AI-crawler UA (Municode disallows those).
            assert "GPTBot" not in ua and "Bytespider" not in ua

    def test_missing_node_id_returns_none(self):
        url = "https://library.municode.com/tx/friendswood/codes/code_of_ordinances"
        assert MunicodeAdapter().fetch_text(url, _friendswood_get()) is None

    def test_unknown_client_returns_none(self):
        def _get(url: str, headers: dict) -> tuple[int, str]:
            if "Clients/stateAbbr" in url:
                return 200, json.dumps([{"ClientID": 1, "ClientName": "Frisco"}])
            return 404, ""

        assert MunicodeAdapter().fetch_text(_PUBLIC_URL, _get) is None

    def test_api_401_returns_none(self):
        # Simulate the missing-X-CSRF 401 gate at the first call.
        def _get(url: str, headers: dict) -> tuple[int, str]:
            return 401, ""

        assert MunicodeAdapter().fetch_text(_PUBLIC_URL, _get) is None

    def test_transport_error_is_swallowed(self):
        def _get(url: str, headers: dict) -> tuple[int, str]:
            raise ConnectionError("network down")

        assert MunicodeAdapter().fetch_text(_PUBLIC_URL, _get) is None

    def test_no_wrong_city_on_prefix_slug(self):
        # A slug that does not EXACTLY match any client must NOT resolve to a
        # different, shorter municipality whose name is a prefix (data safety):
        # slug "van_alstyne" must not become client "Van".
        url = "https://library.municode.com/tx/van_alstyne/codes/x?nodeId=N"

        def _get(u: str, h: dict) -> tuple[int, str]:
            if "Clients/stateAbbr" in u:
                return 200, json.dumps([{"ClientID": 7, "ClientName": "Van"}])
            return 404, ""

        assert MunicodeAdapter().fetch_text(url, _get) is None

    def test_non_string_content_keeps_valid_siblings(self):
        # A malformed Doc (non-str Content) must not raise or discard the whole
        # node — valid sibling docs are still returned.
        def _get(u: str, h: dict) -> tuple[int, str]:
            if "Clients/stateAbbr" in u:
                return 200, json.dumps(
                    [{"ClientID": 2291, "ClientName": "Friendswood"}]
                )
            if u.endswith("/api/ClientContent/2291"):
                return 200, json.dumps({"codes": [{"productId": 13883}]})
            if u.endswith("/api/Jobs/latest/13883"):
                return 200, json.dumps({"Id": 481221})
            if "CodesContent" in u:
                return 200, json.dumps(
                    {
                        "Docs": [
                            {"Content": 12345},  # malformed
                            {"Content": "<p>compressor setback 500 feet</p>"},
                        ]
                    }
                )
            return 404, ""

        text = MunicodeAdapter().fetch_text(_PUBLIC_URL, _get)
        assert text is not None
        assert "compressor setback 500 feet" in text


class TestStripHtml:
    def test_strips_tags_and_unescapes(self):
        assert _strip_html("<p>a&amp;b <b>500&nbsp;ft</b></p>") == "a&b 500 ft"

    def test_empty_fragment(self):
        assert _strip_html("") == ""

    def test_non_string_fragment_is_coerced(self):
        # Never-raise contract: a non-str fragment is coerced, not crashed.
        assert _strip_html(12345) == "12345"
        assert _strip_html(None) == ""
