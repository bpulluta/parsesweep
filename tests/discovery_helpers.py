"""Shared discovery test fixtures and helpers.

Small, reusable factories for discovery-focused tests. Imported the same way
as ``_helpers`` (pytest inserts the ``tests`` directory on ``sys.path``), e.g.::

    from discovery_helpers import make_candidate, FakeResponse, patch_requests_get

Kept intentionally minimal: only helpers duplicated across multiple discovery
test modules belong here.
"""

from __future__ import annotations

from psweep.discovery.models import CandidateScore, DiscoveryCandidate


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

    def iter_content(self, chunk_size: int = 65536):
        del chunk_size
        for chunk in self._chunks:
            yield chunk


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
        "requests.get",
        lambda *_args, **_kwargs: FakeResponse(
            url=url,
            content_type=content_type,
            chunks=chunks,
            text=text,
        ),
    )
