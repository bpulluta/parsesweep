"""Network boundary tests use only stubbed HTTP and browser transports."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from psweep.discovery.network import NetworkPolicyError, send_get
from psweep.discovery.policies import (
    DiscoveryPolicyEvaluator,
    PolicyCheckResult,
)


class Response:
    """Track response ownership for a minimal requests substitute."""

    def __init__(self, url, *, status=200, location=None, text=""):
        self.url = url
        self.status_code = status
        self.headers = {"Content-Type": "text/html"}
        if location:
            self.headers["Location"] = location
        self.text = text
        self.closed = False

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


@pytest.mark.parametrize("denial", [False, PolicyCheckResult(allowed=False)])
def test_denied_request_is_never_sent(monkeypatch, denial):
    """A boolean or policy-result denial fails before the transport."""
    calls = []
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **_kwargs: calls.append(args),
    )
    with pytest.raises(NetworkPolicyError):
        send_get("https://county.gov/private", gate=lambda _url: denial)
    assert calls == []


def test_redirect_preserves_response_cookies(monkeypatch):
    """Manual redirects retain normal cookie-jar domain and path behavior."""
    calls = []
    first = Response("https://county.gov/hub", status=302, location="/codes")
    first.cookies = requests.cookies.RequestsCookieJar()
    first.cookies.set("session", "value", domain="county.gov", path="/")

    def get(url, **kwargs):
        calls.append(kwargs)
        return first if len(calls) == 1 else Response(url)

    monkeypatch.setattr(requests, "get", get)
    response, _ = send_get(first.url)
    jar = calls[-1]["cookies"]
    prepared = requests.Request(
        "GET",
        "https://county.gov/codes",
        cookies=jar,
    ).prepare()
    assert prepared.headers["Cookie"] == "session=value"
    external = requests.Request(
        "GET",
        "https://outside.org/codes",
        cookies=jar,
    ).prepare()
    assert "Cookie" not in external.headers
    assert first.closed
    response.close()


def test_gate_failure_is_not_retried(monkeypatch):
    """A failing policy callback cannot become a transient transport error."""
    checks = []

    def gate(url):
        checks.append(url)
        raise RuntimeError("policy timeout")

    with pytest.raises(NetworkPolicyError):
        send_get("https://county.gov/", gate=gate, retry=(3, 0, 0))
    assert checks == ["https://county.gov/"]


@pytest.mark.parametrize(
    "location", ["/private", "https://outside.org/private"]
)
def test_redirect_target_is_checked_before_fetch(monkeypatch, location):
    """Disallowed redirect paths and hosts are not contacted."""
    initial = Response("https://county.gov/hub", status=302, location=location)
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return initial

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(NetworkPolicyError):
        send_get(
            initial.url,
            gate=lambda url: url == initial.url,
            retry=(3, 0, 0),
        )
    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False
    assert initial.closed


def test_redirect_and_retry_share_pacing_and_request_options(monkeypatch):
    """Every attempt and redirect retains configured transport settings."""
    responses = [
        Response("https://county.gov/hub", status=503),
        Response("https://county.gov/hub", status=302, location="/docs"),
        Response("https://county.gov/docs"),
    ]
    calls = []
    checks = []
    final_checks = []
    waits = []
    sleeps = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return responses[len(calls) - 1]

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr("psweep.discovery.network.time.sleep", sleeps.append)
    response, attempts = send_get(
        "https://county.gov/hub",
        headers={"User-Agent": "configured"},
        timeout=7,
        verify=False,
        stream=True,
        gate=checks.append,
        response_url_gate=final_checks.append,
        limiter=SimpleNamespace(wait=lambda: waits.append(True)),
        retry=(3, 0.25, 1),
    )
    assert attempts == 2
    assert response is responses[-1]
    assert checks == [url for url, _ in calls]
    assert final_checks == [responses[0].url, response.url]
    assert len(waits) == 3
    assert sleeps == [0.25]
    assert all(
        options
        == {
            "headers": {"User-Agent": "configured"},
            "timeout": 7,
            "verify": False,
            "stream": True,
            "allow_redirects": False,
        }
        for _, options in calls
    )
    assert all(item.closed for item in responses[:-1])
    assert not response.closed  # The caller owns the successful response.


def test_final_url_denial_closes_response(monkeypatch):
    """A failed observed-URL guard closes the response without retrying."""
    response = Response("https://county.gov/doc")
    calls = []

    def get(*args, **kwargs):
        calls.append(args)
        return response

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(NetworkPolicyError):
        send_get(response.url, response_url_gate=lambda _url: False)
    assert len(calls) == 1
    assert response.closed


def test_unexpected_response_url_is_request_gated(monkeypatch):
    """A transport reporting an unexpected final URL cannot bypass the gate."""
    response = Response("https://outside.org/doc")
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: response)
    with pytest.raises(NetworkPolicyError):
        send_get(
            "https://county.gov/hub", gate=lambda url: "county.gov" in url
        )
    assert response.closed


def test_redirect_cycle_is_bounded_and_closed(monkeypatch):
    """Redirect loops do not consume unbounded requests or retain responses."""
    responses = []

    def get(url, **kwargs):
        response = Response(url, status=302, location="/hub")
        responses.append(response)
        return response

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(requests.TooManyRedirects):
        send_get("https://county.gov/hub")
    assert len(responses) <= requests.sessions.DEFAULT_REDIRECT_LIMIT + 1
    assert all(response.closed for response in responses)


def test_cross_origin_redirect_does_not_forward_credentials(monkeypatch):
    """Origin-specific secrets are not copied to a redirected host."""
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs["headers"])
        if len(calls) == 1:
            return Response(
                url, status=302, location="https://outside.org/doc"
            )
        return Response(url)

    monkeypatch.setattr(requests, "get", get)
    send_get(
        "https://county.gov/hub",
        headers={"Authorization": "secret", "Cookie": "session", "X-App": "x"},
    )
    assert calls[-1] == {"X-App": "x"}


def test_robots_uses_shared_gate_free_timeout_and_pacing(monkeypatch):
    """Robots lookup avoids recursive gates and closes responses."""
    response = Response(
        "https://county.gov/robots.txt",
        text="User-agent: *\nDisallow: /private\n",
    )
    calls = []
    waits = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(requests, "get", get)
    evaluator = DiscoveryPolicyEvaluator()
    result = evaluator.evaluate(
        url="https://county.gov/private",
        ssl_verify=False,
        robots_policy_mode="enforce",
        tos_policy_mode="ignore",
        timeout_seconds=9,
        rate_limiter=SimpleNamespace(wait=lambda: waits.append(True)),
    )
    assert not result.allowed
    assert result.blocking_code == "robots_disallowed"
    assert calls[0][1]["timeout"] == 9
    assert calls[0][1]["verify"] is False
    assert waits == [True]
    assert response.closed
    evaluator.evaluate(
        url="https://county.gov/public",
        ssl_verify=False,
        tos_policy_mode="ignore",
    )
    assert len(calls) == 1


@pytest.mark.parametrize("mode, allowed", [("warn", True), ("enforce", False)])
def test_policy_modes_do_not_change_the_gate_contract(
    monkeypatch, mode, allowed
):
    """Warn records disallows while enforce blocks before page acquisition."""
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response(url, text="User-agent: *\nDisallow: /private\n")

    monkeypatch.setattr(requests, "get", get)
    evaluator = DiscoveryPolicyEvaluator()

    def gate(url):
        return evaluator.evaluate(
            url=url,
            ssl_verify=True,
            robots_policy_mode=mode,
            tos_policy_mode="ignore",
        )

    if allowed:
        response, attempts = send_get("https://county.gov/private", gate=gate)
        assert attempts == 1
        response.close()
        assert calls == [
            "https://county.gov/robots.txt",
            "https://county.gov/private",
        ]
    else:
        with pytest.raises(NetworkPolicyError) as caught:
            send_get("https://county.gov/private", gate=gate)
        assert caught.value.blocking_code == "robots_disallowed"
        assert calls == ["https://county.gov/robots.txt"]


@pytest.mark.parametrize("status, allowed", [(404, True), (503, False)])
def test_robots_missing_rules_and_fetch_failures_keep_semantics(
    monkeypatch, status, allowed
):
    """Missing rules allow access; unavailable rules fail under enforce."""
    response = Response("https://county.gov/robots.txt", status=status)
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: response)
    result = DiscoveryPolicyEvaluator().evaluate(
        url="https://county.gov/doc",
        ssl_verify=True,
        robots_policy_mode="enforce",
        tos_policy_mode="ignore",
    )
    assert result.allowed is allowed
    assert result.blocking_code == (None if allowed else "robots_unavailable")
    assert response.closed


def test_get_status_errors_are_closed_without_exempting_404(monkeypatch):
    """Only the robots caller, not general acquisitions, interprets 404."""
    response = Response("https://county.gov/doc", status=404)
    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: response)
    with pytest.raises(requests.HTTPError):
        send_get(response.url)
    assert response.closed


@pytest.mark.parametrize("path", ["/public/../private", "/%70rivate"])
@pytest.mark.parametrize("redirect", [False, True])
def test_gate_checks_serialized_request_url(monkeypatch, path, redirect):
    """URL canonicalization cannot let denied paths reach the transport."""
    calls = []
    response = Response("https://county.gov/hub", status=302, location=path)

    def get(url, **kwargs):
        calls.append(url)
        return response

    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(NetworkPolicyError):
        send_get(
            response.url if redirect else "https://county.gov" + path,
            gate=lambda url: url != "https://county.gov/private",
        )
    assert calls == ([response.url] if redirect else [])
    if redirect:
        assert response.closed


@pytest.mark.parametrize("invalid", ["allow", 1, (False, "denied"), object()])
def test_invalid_gate_result_fails_closed(monkeypatch, invalid):
    """Malformed truthy callback values cannot silently authorize requests."""
    calls = []
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **_kwargs: calls.append(args),
    )
    with pytest.raises(NetworkPolicyError, match="must return"):
        send_get("https://county.gov/hub", gate=lambda _url: invalid)
    assert calls == []
