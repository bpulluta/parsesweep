"""Base interfaces for discovery connectors (seeker, digger, validators)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class SeekerInput:
    """Normalized input for seeker stage discovery."""

    query: str
    max_results: int = 10
    extra_params: dict[str, Any] | None = None


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

        Returns list of normalized candidates with:
        {
            "url": str,
            "source": str,
            "title": str | None,
            "snippet": str | None,
            "reasons": list[str],
        }

        Raises
        ------
            RuntimeError: If provider is unavailable or API key is missing.
            ValueError: If query is invalid.
        """
        pass

    @abstractmethod
    def supports_provider(self, provider: str) -> bool:
        """Check if this connector supports the given provider name."""
        pass


@dataclass(slots=True)
class DiggerInput:
    """Normalized input for digger stage link/document discovery."""

    seed_urls: list[str]
    max_depth: int = 2
    max_pages: int = 50
    max_files: int = 20
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


class BaseDiggerConnector(ABC):
    """
    Abstract interface for bounded crawl/download discovery (digger stage).

    Implementations crawl pages and return normalized artifact candidates.
    """

    @abstractmethod
    def discover(self, digger_input: DiggerInput) -> list[DiggerArtifact]:
        """
        Discover candidate artifacts from seed pages within crawl budgets.

        Returns
        -------
            Normalized digger artifacts for downstream validation/scoring.
        """
        pass

    @abstractmethod
    def supports_provider(self, provider: str) -> bool:
        """Check if this connector supports the given provider name."""
        pass
