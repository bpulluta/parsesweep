"""Internal acquisition boundary for policy-gated discovery requests."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

from .retry import compute_backoff, is_transient_error

URLGate = Callable[[str], object]
RetryConfig = tuple[int, float, float]
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_ORIGIN_HEADERS = {"authorization", "cookie", "proxy-authorization", "host"}


class RateLimiter(Protocol):
    """Provide the shared acquisition pacing operation."""

    def wait(self) -> None:
        """Wait until the next outbound request may start."""
        ...


class NetworkPolicyError(RuntimeError):
    """A URL gate denied acquisition or could not be evaluated."""

    def __init__(self, url: str, reason: str, result: object = None) -> None:
        self.url = url
        self.result = result
        self.blocking_code = getattr(result, "blocking_code", None)
        super().__init__(f"Discovery acquisition denied for '{url}': {reason}")


def check_url(url: str, gate: URLGate | None) -> None:
    """Run a gate, accepting bools or policy-check results.

    A callback returning ``None`` may enforce by raising. Failures fail
    closed and are never classified as transient network errors.
    """
    if gate is None:
        return
    try:
        result = gate(url)
    except NetworkPolicyError:
        raise
    except Exception as exc:
        raise NetworkPolicyError(url, str(exc)) from exc
    allowed = getattr(result, "allowed", result)
    if allowed is None or allowed is True:
        return
    if allowed is not False:
        reason = "URL gate must return bool, None, or a policy result"
        raise NetworkPolicyError(url, reason, result)
    messages = getattr(result, "messages", [])
    reason = "; ".join(messages) if messages else "URL gate rejected access"
    raise NetworkPolicyError(url, reason, result)


def _redirect_headers(
    headers: dict[str, str],
    previous_url: str,
    next_url: str,
) -> dict[str, str]:
    """Drop origin-bound headers for cross-origin redirects."""
    previous = urlparse(previous_url)
    following = urlparse(next_url)
    if (previous.scheme, previous.netloc) == (
        following.scheme,
        following.netloc,
    ):
        return headers
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _ORIGIN_HEADERS
    }


def _get_redirect_chain(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    verify: bool,
    stream: bool,
    gate: URLGate | None,
    response_url_gate: URLGate | None,
    limiter: RateLimiter | None,
) -> Any:
    """Check each target before following a bounded redirect chain."""
    import requests  # noqa: PLC0415 - dependency is optional at import time

    current_url = url
    current_headers = headers
    cookies = requests.cookies.RequestsCookieJar()
    for hop in range(requests.sessions.DEFAULT_REDIRECT_LIMIT + 1):
        parsed = urlparse(current_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise NetworkPolicyError(current_url, "not an HTTP(S) URL")
        # Requests removes dot segments and decodes unreserved escapes.
        # Gate the actual serialized URL before any request is sent.
        current_url = str(requests.Request("GET", current_url).prepare().url)
        check_url(current_url, gate)
        if limiter is not None:
            limiter.wait()
        cookie_options = {"cookies": cookies} if cookies else {}
        response = requests.get(
            current_url,
            headers=current_headers,
            timeout=timeout,
            verify=verify,
            stream=stream,
            allow_redirects=False,
            **cookie_options,
        )
        try:
            observed_url = str(getattr(response, "url", "") or current_url)
            if observed_url != current_url:
                check_url(observed_url, gate)
            location = (response.headers or {}).get("Location")
            if response.status_code in _REDIRECT_STATUSES and location:
                response_cookies = getattr(response, "cookies", None)
                if response_cookies:
                    cookies.update(response_cookies)
                if hop >= requests.sessions.DEFAULT_REDIRECT_LIMIT:
                    message = "Discovery redirect limit exceeded"
                    raise requests.TooManyRedirects(  # noqa: TRY301
                        message,
                        response=response,
                    )
                next_url = urljoin(observed_url, location)
                current_headers = _redirect_headers(
                    current_headers,
                    current_url,
                    next_url,
                )
                current_url = next_url
            else:
                check_url(observed_url, response_url_gate)
                return response
        except BaseException:
            response.close()
            raise
        response.close()
    message = "Discovery redirect limit exceeded"
    raise requests.TooManyRedirects(message)


def send_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 60,
    verify: bool = True,
    stream: bool = False,
    gate: URLGate | None = None,
    response_url_gate: URLGate | None = None,
    limiter: RateLimiter | None = None,
    retry: RetryConfig = (1, 0.0, 0.0),
    raise_for_status: bool = True,
) -> tuple[Any, int]:
    """Return a response and attempt count through the GET boundary.

    All GETs, including redirects and retries, check ``gate`` and wait
    on ``limiter``. ``response_url_gate`` checks the final URL before
    callers use the body. Redirect/error responses are closed; callers
    own and must close successful responses. Status errors are retried
    with discovery backoff. Robots lookup disables ``raise_for_status``
    to interpret missing rules without invoking a recursive gate.
    """
    max_attempts, initial_backoff, max_backoff = retry
    for attempt in range(1, max(1, max_attempts) + 1):
        response = None
        try:
            response = _get_redirect_chain(
                url,
                headers=dict(headers or {}),
                timeout=timeout,
                verify=verify,
                stream=stream,
                gate=gate,
                response_url_gate=response_url_gate,
                limiter=limiter,
            )
            if raise_for_status:
                response.raise_for_status()
        except BaseException as exc:
            if response is not None:
                response.close()
            if (
                not isinstance(exc, Exception)
                or isinstance(exc, NetworkPolicyError)
                or attempt >= max_attempts
                or not is_transient_error(exc)
            ):
                raise
            delay = compute_backoff(
                attempt + 1,
                initial_backoff_seconds=initial_backoff,
                max_backoff_seconds=max_backoff,
            )
            if delay > 0:
                time.sleep(delay)
        else:
            return response, attempt
    message = "Unexpected empty retry cycle for discovery request"
    raise RuntimeError(message)
