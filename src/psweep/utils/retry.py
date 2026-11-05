"""Shared retry primitives: backoff math and retryable-failure classification.

Every in-run retry loop and every cross-run completion decision uses these, so
"may succeed later" has one definition.
"""

from __future__ import annotations

import re
import ssl
from collections.abc import Mapping

import openai
import requests


# Message markers for untyped transient failures (e.g. Selenium timeouts).
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "try again",
    "rate limit",
    "connection reset",
    "connection aborted",
    "connection refused",
    "name resolution",
    # Chrome network error codes (Selenium navigation and error pages).
    "net::err_connection_",
    "net::err_timed_out",
    "net::err_name_not_resolved",
    "net::err_internet_disconnected",
    "net::err_network_changed",
    "net::err_address_unreachable",
    "net::err_empty_response",
)
_URL = re.compile(r"\S+://\S+")
# Edge bot-manager challenges (Cloudflare, Vercel, AWS WAF) answer 403/429/503.
# Edges sample them (Cloudflare challenged ~1 in 3 identical requests to county
# sites), so a run repeats one, but a URL challenged on every attempt is final.
_CHALLENGE_HEADERS: tuple[str, ...] = (
    "cf-mitigated",
    "x-vercel-mitigated",
    "x-amzn-waf-action",
)


def compute_backoff(
    attempt: int,
    *,
    initial_backoff_seconds: float,
    max_backoff_seconds: float,
) -> float:
    """Exponential backoff (seconds) before ``attempt``.

    Attempt 1 waits 0 (first try). Attempt N waits
    ``initial * 2**(N-2)``, clamped to ``max_backoff_seconds`` when positive.
    """
    if attempt <= 1:
        return 0.0
    wait = initial_backoff_seconds * (2 ** (attempt - 2))
    if max_backoff_seconds <= 0:
        return max(0.0, wait)
    return min(wait, max_backoff_seconds)


def _is_challenge(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    return isinstance(exc, requests.exceptions.HTTPError) and any(
        name in headers for name in _CHALLENGE_HEADERS
    )


def may_retry_in_run(exc: BaseException) -> bool:
    """Return True when the same run should repeat the request: transient
    errors and sampled bot-manager challenges. Only transient errors leave
    a target incomplete for the next run (``is_transient_error``)."""
    return is_transient_error(exc) or _is_challenge(exc)


def is_transient_error(exc: BaseException) -> bool:
    """Return True when a later attempt of the same request may succeed.

    Timeouts, connection errors, truncated streams and HTTP 408/429/5xx are
    transient unless a bot-manager challenge answered. TLS errors (anywhere in
    the cause chain) and other HTTP 4xx responses are terminal. An error that
    declares a boolean ``retryable`` (policy rejections) is transient when it
    says so or a transient error caused it. Errors carrying an int
    ``status_code`` (provider SDKs, browser fetches) are classified by status;
    status-less errors fall back to message markers (``Failed to fetch`` also
    covers CORS and mixed-content rejections, so it is not a marker).
    """
    declared = getattr(exc, "retryable", None)
    if isinstance(declared, bool):
        cause = exc.__cause__
        return declared or (cause is not None and is_transient_error(cause))
    if _tls_failure(exc):
        return False
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return _transient_status(exc.response.status_code) and not _is_challenge(exc)
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return _transient_status(status)
    if isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            openai.APIConnectionError,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return True
    message = _URL.sub("", str(exc)).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def _tls_failure(exc: BaseException | None) -> bool:
    """Return True when a TLS error raised or caused ``exc``; SDKs wrap
    certificate failures as connection errors or HTTP 500s."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, (ssl.SSLError, requests.exceptions.SSLError)):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def _transient_status(status: int) -> bool:
    return status in {408, 429} or status >= 500


def browser_may_recover(exc: BaseException) -> bool:
    """Return True when a real browser may pass where the HTTP client was refused."""
    if not isinstance(exc, requests.exceptions.HTTPError):
        return False
    status = getattr(exc.response, "status_code", None)
    return status in {401, 403, 429, 451} or any(
        marker in str(exc).lower() for marker in ("forbidden", "unauthorized", "access denied")
    )


def is_retryable(record: Mapping[str, object]) -> bool:
    """Return True when a failed download/review record may succeed on re-run.

    Only retryable failures block partition publication and keep a target
    incomplete; terminal failures stay recorded but do not block.
    """
    if record.get("status") == "failed":
        return bool(record.get("retryable"))
    if record.get("review_failed"):
        return bool(record.get("review_retryable"))
    return False
