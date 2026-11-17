"""Shared discovery test fixtures and helpers.

Small, reusable factories for discovery-focused tests. Imported the same way
as ``_helpers`` (pytest inserts the ``tests`` directory on ``sys.path``), e.g.::

    from discovery_helpers import make_candidate, FakeResponse, patch_requests_get

Kept intentionally minimal: only helpers duplicated across multiple discovery
test modules belong here.
"""

from __future__ import annotations

from threading import Lock
from types import SimpleNamespace

from psweep.discovery.models import CandidateScore, DiscoveryCandidate


def review_evidence(quote: str) -> dict:
    """Complete reviewer evidence for an explicit quoted offline source fixture."""
    return {
        "identity_quote": quote,
        "content_quote": quote,
        "scope": {"value": "fixture source scope", "quote": quote},
        "claim_kind": {"value": "direct fixture claim", "quote": quote},
        "source_date": None,
        "coverage": {},
    }


class MeteredReviewClient:
    """Keep injected review clients on the production physical-attempt usage contract."""

    def extract(self, *args, before_request=None, **kwargs):
        if before_request:
            before_request()
        try:
            result = self.respond(*args, **kwargs)
        except Exception as exc:
            self._measure(getattr(exc, "usage", None))
            raise
        self._measure(result)
        return result

    def respond(self, *args, **kwargs):
        """Supply a response without replacing metering or the request-budget boundary."""
        raise NotImplementedError

    def __init__(self):
        self.usage = {
            "calls": 0,
            "cost": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "unknown_cost_calls": 0,
        }
        self._usage_lock = Lock()

    @property
    def usage_snapshot(self):
        with self._usage_lock:
            return dict(self.usage)

    def _measure(self, result):
        result = result if isinstance(result, dict) else {}
        with self._usage_lock:
            usage = self.usage
            usage["calls"] += 1
            usage["unknown_cost_calls"] += any(
                key not in result for key in ("cost", "input_tokens", "output_tokens")
            )
            for key in ("cost", "input_tokens", "output_tokens"):
                usage[key] += result.get(key, 0)


def make_candidate(
    url: str,
    *,
    reasons: list[str] | None = None,
    score: CandidateScore | None = None,
    source: str = "test",
    **extra: object,
) -> DiscoveryCandidate:
    """Build a ``DiscoveryCandidate`` for tests.

    ``score`` defaults to a neutral ``CandidateScore()``. Callers that need a
    non-default score (e.g. prioritizer tests) pass ``score=`` explicitly.
    """
    return DiscoveryCandidate(
        url=url,
        source=source,
        score=score if score is not None else CandidateScore(),
        reasons=reasons or [],
        **extra,
    )


class FakeResponse:
    """Streaming ``requests.get`` stub for download-stage discovery tests.

    Mimics the subset of the ``requests.Response`` API the download stage uses:
    ``url``, ``headers``, ``status_code``, ``text``, ``raise_for_status()``, and
    streamed ``iter_content()``. Body chunks default to a minimal valid PDF so
    ``startswith(b"%PDF")`` checks pass.
    """

    def __init__(
        self,
        *,
        url: str,
        content_type: str = "application/pdf",
        chunks: list[bytes] | None = None,
        text: str = "",
        status_code: int = 200,
    ):
        self.url = url
        self.headers = {"Content-Type": content_type}
        self.text = text
        self.status_code = status_code
        self._chunks = chunks or [b"%PDF-1.4\n", b"mock-content"]

    def raise_for_status(self):
        return None

    def close(self):
        """Model response ownership without any external resource."""
        self.closed = True

    def iter_content(self, chunk_size: int = 65536):
        del chunk_size
        yield from self._chunks


def patch_requests_get(
    monkeypatch,
    *,
    url: str,
    content_type: str = "application/pdf",
    chunks: list[bytes] | None = None,
    text: str = "",
) -> None:
    """Patch ``requests.get`` to return a single :class:`FakeResponse`."""
    monkeypatch.setattr(
        "psweep.discovery.network.request_get",
        lambda *_args, **_kwargs: FakeResponse(
            url=url,
            content_type=content_type,
            chunks=chunks,
            text=text,
        ),
    )


def patch_socket_stream(
    monkeypatch,
    bodies: dict[str, list[tuple[float, bytes]]],
    *,
    content_type: str = "application/pdf",
) -> None:
    """Serve each URL's ``(seconds, chunk)`` reads through the real bounded transport.

    Each read advances a fake monotonic clock by its seconds, so deadlines
    are exercised without sleeping.
    """
    from psweep.discovery import transport

    now = [0.0]
    monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def get(_session, url, **_kwargs):
        pending = iter(bodies[url])

        def read1(_size, decode_content=True):
            seconds, chunk = next(pending, (0.0, b""))
            now[0] += seconds
            return chunk

        return SimpleNamespace(
            raw=SimpleNamespace(read1=read1),
            status_code=200,
            headers={"Content-Type": content_type},
            url=url,
            cookies={},
            encoding=None,
            close=lambda: None,
            raise_for_status=lambda: None,
        )

    monkeypatch.setattr(transport.requests.Session, "get", get)
