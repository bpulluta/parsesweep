"""One classifier decides which failures may succeed on a later attempt."""

import ssl

import httpx
import litellm
import openai
import pytest
import requests

from psweep.discovery.browser import BrowserDownloadError
from psweep.utils.error_taxonomy import build_error_record
from psweep.utils.retry import (
    browser_may_recover,
    is_retryable,
    is_transient_error,
    may_retry_in_run,
)


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} error", response=response)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (requests.Timeout("read timed out"), True),
        (requests.ConnectionError("reset"), True),
        (_http_error(429), True),
        (_http_error(503), True),
        (_http_error(403), False),
        (_http_error(404), False),
        (requests.exceptions.SSLError("certificate has expired"), False),
        (ValueError("unsupported content type"), False),
        (requests.exceptions.ChunkedEncodingError("truncated stream"), True),
        (TimeoutError("query timed out"), True),
        # fetch() rejections also cover CORS/mixed-content/cert failures.
        (BrowserDownloadError("Browser download failed: TypeError: Failed to fetch"), False),
        (BrowserDownloadError("Browser download failed: net::ERR_CERT_DATE_INVALID"), False),
        (BrowserDownloadError("Browser download failed: net::ERR_CONNECTION_RESET"), True),
        (BrowserDownloadError("Browser download failed: script timeout"), True),
        (BrowserDownloadError("Browser download failed: HTTP 500", status_code=500), True),
        (BrowserDownloadError("Browser download failed: HTTP 429", status_code=429), True),
        (BrowserDownloadError(
            "Browser download failed for https://x.gov/ord-2504.pdf: HTTP 404",
            status_code=404,
        ), False),
        (RuntimeError("failed for https://x.gov/timeout-503.pdf: not a document"), False),
    ],
)
def test_transient_error_classification(exc, expected):
    assert is_transient_error(exc) is expected


def _provider(name, **extra):
    return getattr(litellm, name)(
        message="provider failed", model="m", llm_provider="openai", **extra
    )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(408), True),
        (_provider("Timeout"), True),
        (_provider("APIConnectionError"), True),
        (_provider("InternalServerError"), True),
        (_provider("ServiceUnavailableError"), True),
        (_provider("BadGatewayError"), True),
        (_provider("RateLimitError"), True),
        (_provider("BadRequestError"), False),
        (_provider("AuthenticationError"), False),
        (_provider("ContextWindowExceededError"), False),
        (_provider("NotFoundError"), False),
    ],
)
def test_provider_errors_are_classified_by_status(exc, expected):
    assert is_transient_error(exc) is expected


def _caused_by(exc, cause, *, implicit=False):
    if implicit:
        exc.__context__ = cause
    else:
        exc.__cause__ = cause
    return exc


_REQUEST = httpx.Request("POST", "https://llm.example/v1/chat/completions")
_CERT = ssl.SSLCertVerificationError("certificate is not trusted")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (openai.APIConnectionError(request=_REQUEST), True),
        (openai.APITimeoutError(request=_REQUEST), True),
        (_caused_by(openai.APIConnectionError(request=_REQUEST), _CERT), False),
        # litellm maps a certificate failure to a 500 while handling it.
        (_caused_by(_provider("InternalServerError"), _CERT, implicit=True), False),
        (_caused_by(requests.ConnectionError("reset"), _CERT), False),
    ],
)
def test_sdk_connection_errors_are_transient_unless_tls_failed(exc, expected):
    assert is_transient_error(exc) is expected


@pytest.mark.parametrize(
    ("status", "header"),
    [
        (429, "X-Vercel-Mitigated"),
        (403, "cf-mitigated"),
        (503, "cf-mitigated"),
        (405, "x-amzn-waf-action"),
    ],
)
def test_bot_manager_challenges_retry_in_run_but_are_final(status, header):
    """Edges sample challenges, so a repeat may pass; a URL challenged on
    every attempt is a final outcome, not an incomplete target."""
    exc = _http_error(status)
    exc.response.headers[header] = "challenge"
    assert may_retry_in_run(exc) is True
    assert is_transient_error(exc) is False
    assert build_error_record(exc, stage="download")["retryable"] is False


@pytest.mark.parametrize(
    ("exc", "expected"),
    [(_http_error(503), True), (_http_error(403), False), (_http_error(404), False)],
)
def test_in_run_retry_otherwise_follows_transient_classification(exc, expected):
    assert may_retry_in_run(exc) is expected


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_http_error(403), True),
        (_http_error(429), True),
        (_http_error(451), True),
        (_http_error(404), False),
        (_http_error(500), False),
        (requests.HTTPError("Access Denied"), True),
        (requests.ConnectionError("forbidden"), False),
    ],
)
def test_browser_may_recover_refusals_only(exc, expected):
    assert browser_may_recover(exc) is expected


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"status": "failed", "retryable": True}, True),
        ({"status": "failed", "retryable": False}, False),
        ({"status": "failed"}, False),
        ({"status": "downloaded", "review_failed": True, "review_retryable": True}, True),
        ({"status": "downloaded", "review_failed": True, "review_retryable": False}, False),
        ({"status": "downloaded"}, False),
    ],
)
def test_record_retryability(record, expected):
    assert is_retryable(record) is expected


def test_build_error_record_uses_typed_classification():
    """Status digits or markers inside a URL never make a failure retryable."""
    exc = _http_error(404)
    exc.args = ("404 Client Error for url: https://x.gov/ord-2504-timeout.pdf",)
    assert build_error_record(exc, stage="discovery.download")["retryable"] is False


def test_browser_download_raises_typed_status():
    from psweep.discovery.browser import BrowserSession

    session = BrowserSession.__new__(BrowserSession)
    driver = type("Driver", (), {
        "current_url": "https://x.gov/",
        "set_script_timeout": lambda *_a: None,
        "execute_async_script": lambda *_a: '{"ok": false, "status": 503}',
    })()
    session._driver = driver
    with pytest.raises(BrowserDownloadError) as caught:
        session.download_bytes("https://x.gov/ord-404.pdf")
    assert caught.value.status_code == 503 and is_transient_error(caught.value)


class WebDriverException(Exception):
    """Mirrors Selenium's ``str()``: ``Message: <driver error>``."""

    def __str__(self):
        return f"Message: {self.args[0]}"


@pytest.mark.parametrize(("code", "expected"), [
    ("ERR_CONNECTION_RESET", True),
    ("ERR_CONNECTION_TIMED_OUT", True),
    ("ERR_NAME_NOT_RESOLVED", True),
    ("ERR_INTERNET_DISCONNECTED", True),
    ("ERR_NETWORK_CHANGED", True),
    ("ERR_TIMED_OUT", True),
    ("ERR_CERT_DATE_INVALID", False),
])
def test_chrome_network_errors_are_transient(code, expected):
    exc = WebDriverException(f"unknown error: net::{code}\n  (Session info: chrome=126)")
    assert is_transient_error(exc) is expected


def _session(current_url, page_source="", get=lambda _url: None):
    from psweep.discovery.browser import BrowserSession

    session = BrowserSession.__new__(BrowserSession)
    session._driver = type("Driver", (), {
        "current_url": current_url,
        "page_source": page_source,
        "get": staticmethod(get),
        "set_script_timeout": lambda *_a: None,
    })()
    return session


@pytest.mark.parametrize("navigate", [
    lambda s: s.fetch_html("https://x.gov/page", settle_seconds=0),
    lambda s: s.download_bytes("https://x.gov/doc.pdf"),
])
def test_chrome_error_page_raises_transient_navigation_error(navigate):
    from psweep.discovery.browser import BrowserNavigationError

    session = _session(
        "chrome-error://chromewebdata/",
        '<div class="error-code">ERR_CONNECTION_RESET</div>',
    )
    with pytest.raises(BrowserNavigationError, match=r"net::ERR_CONNECTION_RESET") as caught:
        navigate(session)
    assert is_transient_error(caught.value)


def test_driver_navigation_error_propagates_as_transient():
    def get(_url):
        raise WebDriverException("unknown error: net::ERR_INTERNET_DISCONNECTED")

    with pytest.raises(WebDriverException) as caught:
        _session("about:blank", get=get).fetch_html("https://x.gov/", settle_seconds=0)
    assert is_transient_error(caught.value)


def test_policy_rejection_is_transient_only_when_caused_by_a_transient_error():
    from psweep.discovery.network import NetworkPolicyError, check_url

    def flaky_gate(_url):
        raise requests.ReadTimeout("robots.txt read timed out")

    with pytest.raises(NetworkPolicyError) as transient:
        check_url("https://x.gov/doc.pdf", flaky_gate)
    with pytest.raises(NetworkPolicyError) as denied:
        check_url("https://x.gov/doc.pdf", lambda _url: False)
    assert is_transient_error(transient.value)
    assert not is_transient_error(denied.value)


def test_retry_classifier_has_no_discovery_imports():
    """Extraction imports the classifier, so it must not import discovery."""
    import ast
    from pathlib import Path

    import psweep.utils.retry as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("discovery" in name for name in imported)
