"""Shared retry primitives for the discovery module.

Exponential backoff and transient-error classification were previously
implemented three times (engine downloads, SerpApi seeker, HTTP digger) with
byte-identical backoff math and near-identical marker lists. Centralizing them
removes the drift risk; each caller keeps its own retry *loop* (they differ in
rate-limiting and return shapes) but shares this math and this predicate.
"""

from __future__ import annotations

# Substrings that mark a retryable, transient failure. Union of what the three
# call sites checked independently — a superset is safe for retries.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "timeout",
    "temporarily unavailable",
    "try again",
    "rate limit",
    "connection reset",
    "connection aborted",
    "connection refused",
    "name resolution",
    "dns",
    "ssl",
    "tls",
    "429",
    "503",
    "504",
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


def is_transient_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like a retryable transient failure.

    Checks ``requests`` network exception types when available, then falls back
    to substring markers in the exception message.
    """
    try:  # Optional: requests may not be importable in minimal environments.
        import requests

        if isinstance(
            exc,
            (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.SSLError,
            ),
        ):
            return True
    except Exception:  # noqa: BLE001 - requests missing / import side effects
        pass

    lowered = str(exc).lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)
