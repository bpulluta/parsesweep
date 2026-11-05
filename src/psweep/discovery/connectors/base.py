"""Base interfaces for discovery connectors (seeker, digger, validators)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SeekerInput:
    """Normalized input for seeker stage discovery."""

    queries: list[str]
    max_results: int = 10
    context: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


class BaseSeekerConnector(ABC):
    """
    Abstract interface for search discovery (seeker stage).

    Implementations discover candidate URLs from a query.
    Results are normalized to DiscoveryCandidate model.
    """

    @abstractmethod
    def discover(self, seeker_input: SeekerInput) -> list[dict[str, Any]]:
        """
        Discover candidate URLs from a query.

        Returns
        -------
        list[dict[str, Any]]
            Normalized candidate records, each shaped like::

                {
                    "url": str,
                    "source": str,
                    "title": str | None,
                    "snippet": str | None,
                    "reasons": list[str],
                }

        Raises
        ------
        RuntimeError
            If the provider is unavailable or the API key is missing.
        ValueError
            If the query is invalid.
        """

    @abstractmethod
    def supports_provider(self, provider: str) -> bool:
        """Check if this connector supports the given provider name."""


@dataclass(slots=True)
class DiggerInput:
    """Normalized input for digger stage link/document discovery."""

    sources: list[str]
    max_depth: int
    max_pages: int
    max_files: int | None = None
    timeout_seconds: int = 30
    allowed_domains: list[str] | None = None
    include_url_patterns: list[str] | None = None
    include_link_text_patterns: list[str] | None = None
    extra_params: dict[str, Any] | None = None


@dataclass(slots=True)
class DiggerArtifact:
    """Normalized digger artifact record returned by digger connectors."""

    url: str
    source: str
    mime_type: str | None = None
    extension: str | None = None
    status: str = "discovered"
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this discovered artifact to a plain dict."""
        return {
            "url": self.url,
            "source": self.source,
            "mime_type": self.mime_type,
            "extension": self.extension,
            "status": self.status,
            "metadata": self.metadata or {},
        }


@dataclass(slots=True)
class CrawlResult:
    """Crawl outcome: linked artifacts plus sources whose crawl is unfinished.

    ``incomplete_sources`` are source pages whose crawl hit a retryable fetch
    failure or ran out of the ``max_pages``/``max_files`` budget, so they must
    be crawled again on a later run.
    """

    artifacts: list[DiggerArtifact]
    incomplete_sources: set[str] = field(default_factory=set)
    pages_fetched: int = 0


class BaseDiggerConnector(ABC):
    """
    Abstract interface for bounded crawl/download discovery (digger stage).

    Implementations crawl pages and return normalized artifact candidates.
    """

    @abstractmethod
    def discover(self, digger_input: DiggerInput) -> CrawlResult:
        """
        Discover candidate artifacts linked from source pages within crawl budgets.

        Returns
        -------
            Normalized digger artifacts and the sources left incomplete.
        """

    @abstractmethod
    def supports_provider(self, provider: str) -> bool:
        """Check if this connector supports the given provider name."""
