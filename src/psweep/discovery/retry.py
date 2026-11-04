"""Shared retry primitives: backoff math and retryable-failure classification.

Every in-run retry loop and every cross-run completion decision uses these, so
"may succeed later" has one definition.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import requests

from psweep.utils.error_taxonomy import build_error_record

from .browser import BrowserDownloadError
from .transport import NetworkPolicyError

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


def is_transient_error(exc: BaseException) -> bool:
    """Return True when a later attempt of the same request may succeed.

    Timeouts, connection errors, truncated streams, browser network failures
    and HTTP 429/5xx are transient; TLS certificate errors and other HTTP 4xx
    responses are terminal. A policy rejection is transient only when its
    evaluation failed transiently (e.g. robots.txt timed out) or a transient
    error caused it. Typed errors are classified by type and status; only
    untyped errors fall back to message markers.
    """
    if isinstance(exc, NetworkPolicyError):
        cause = exc.__cause__
        return bool(getattr(exc.result, "retryable", False)) or (
            cause is not None and is_transient_error(cause)
        )
    if isinstance(exc, requests.exceptions.SSLError):
        return False
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return _transient_status(exc.response.status_code)
    if isinstance(exc, BrowserDownloadError):
        return exc.status is None or _transient_status(exc.status)
    if isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return True
    message = _URL.sub("", str(exc)).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def _transient_status(status: int) -> bool:
    return status == 429 or status >= 500


def error_record(exc: BaseException, **context: str | None) -> dict[str, object]:
    """Build the canonical error record, classified by :func:`is_transient_error`."""
    return {**build_error_record(exc, **context), "retryable": is_transient_error(exc)}


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
