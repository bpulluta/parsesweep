"""Fixture-based integration tests for digger connector with realistic HTML scenarios.

These tests validate digger behavior against real HTML fixtures representing:
- Static pages with downloadable links
- JS-heavy pages (simulated with noscript/placeholder content)
- Mixed content portals with various document types
- Centralized index pages for sweep mode

The fixtures help ensure the digger abstraction correctly handles:
- Link extraction and validation from realistic HTML
- Budget enforcement with real document discovery scenarios
- Domain and file filter compliance with complex pages
- Topology modes (seed-only, centralized sweep) with fixture data
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from psweep.discovery.connectors import (
    DiggerArtifact,
    DiggerInput,
    NullDiggerConnector,
)


def get_fixture_path(fixture_name: str) -> Path:
    """Resolve an HTML fixture file."""
    test_dir = Path(__file__).parent
    fixture_path = test_dir / "fixtures" / "digger_html" / fixture_name
    if not fixture_path.exists():
        raise FileNotFoundError(f"Fixture not found: {fixture_path}")
    return fixture_path


def extract_links_from_html(html_content: str) -> list[dict[str, str]]:
    """Extract all href links from HTML content.

    Returns list of dicts with 'url' and 'text' keys.
    Simple implementation for test fixtures (not a full HTML parser).
    """
    links: list[dict[str, str]] = []
    # Match <a href="...">...</a> tags
    pattern = r'<a\s+href=["\']([^"\']+)["\'][^>]*>([^<]*)</a>'
    for match in re.finditer(pattern, html_content, flags=re.IGNORECASE):
        url = match.group(1)
        text = match.group(2).strip()
        if url.startswith(("http://", "https://")):
            links.append({"url": url, "text": text})
    return links


def _links_from_fixture(fixture_name: str) -> list[dict[str, str]]:
    fixture = get_fixture_path(fixture_name)
    html = fixture.read_text()
    return extract_links_from_html(html)


def _discover_seed_urls(
    seed_urls: list[str],
    *,
    max_depth: int = 1,
    max_pages: int = 10,
    max_files: int = 10,
    timeout_seconds: int = 30,
    allowed_domains: list[str] | None = None,
    include_url_patterns: list[str] | None = None,
    include_link_text_patterns: list[str] | None = None,
    extra_params: dict[str, object] | None = None,
) -> list[DiggerArtifact]:
    digger_input = DiggerInput(
        seed_urls=seed_urls,
        max_depth=max_depth,
        max_pages=max_pages,
        max_files=max_files,
        timeout_seconds=timeout_seconds,
        allowed_domains=allowed_domains,
        include_url_patterns=include_url_patterns,
        include_link_text_patterns=include_link_text_patterns,
        extra_params=extra_params,
    )
    return NullDiggerConnector().discover(digger_input)


class TestDiggerWithStaticPageFixture:
    """Test digger with static HTML page fixture (no JS rendering needed)."""

    def test_static_page_discovers_all_links(self):
        """Static page fixture contains simple downloadable links."""
        links = _links_from_fixture("static_ordinance_page.html")

        # Should find all links in the fixture
        assert len(links) >= 5
        urls = [link["url"] for link in links]
        assert "https://chaffeecounty.org/docs/ordinance.pdf" in urls
        assert "https://chaffeecounty.org/docs/53007.pdf" in urls
        assert "https://chaffeecounty.org/docs/chapter-11.docx" in urls

    def test_static_page_with_file_extension_filter(self):
        """Digger should filter links by file extension pattern."""
        links = _links_from_fixture("static_ordinance_page.html")

        # Create digger input to discover only PDFs
        pdf_urls = [l["url"] for l in links if l["url"].endswith(".pdf")]
        artifacts = _discover_seed_urls(
            pdf_urls,
            include_url_patterns=[r"\.pdf$"],
        )

        # All discovered artifacts should be PDFs
        assert all(a.url.endswith(".pdf") for a in artifacts)
        assert len(artifacts) >= 2

    def test_static_page_with_domain_allowlist(self):
        """Domain allowlist should filter links from disallowed domains."""
        links = _links_from_fixture("static_ordinance_page.html")

        # Fixture contains both county.org and external-site.com links
        all_urls = [l["url"] for l in links]
        assert any("chaffeecounty.org" in u for u in all_urls)
        assert any("external-site.com" in u for u in all_urls)

        # Create digger input that only allows county.org
        artifacts = _discover_seed_urls(
            all_urls,
            allowed_domains=["chaffeecounty.org"],
        )

        # All discovered artifacts should be from allowed domain
        assert all("chaffeecounty.org" in a.url for a in artifacts)
        assert not any("external-site.com" in a.url for a in artifacts)

    def test_static_page_combined_allowlist_and_extension_filter(self):
        """Combined domain allowlist and file extension filter on realistic page."""
        links = _links_from_fixture("static_ordinance_page.html")

        all_urls = [l["url"] for l in links]

        # Filter for PDFs from county.org
        artifacts = _discover_seed_urls(
            all_urls,
            allowed_domains=["chaffeecounty.org"],
            include_url_patterns=[r"\.pdf$"],
        )

        # All artifacts should be PDFs from allowed domain
        assert all(a.url.endswith(".pdf") and "chaffeecounty.org" in a.url for a in artifacts)
        # Fixture has ordinance.pdf and 53007.pdf from chaffeecounty.org
        assert len(artifacts) >= 2


class TestDiggerWithJsHeavyPageFixture:
    """Test digger with JS-heavy DOM fixture (simulated dynamic content)."""

    def test_js_heavy_page_static_content_visibility(self):
        """JS-heavy fixture should show what's statically visible (not JS-rendered)."""
        links = _links_from_fixture("js_heavy_portal.html")

        # The fixture has static content and JS-only content
        # Simple extraction should find only the static links
        urls = [l["url"] for l in links]

        # Static links should be discovered
        assert "https://portal.county.gov/docs/geothermal-ordinance.pdf" in urls
        assert "https://portal.county.gov/docs/solar-regulations.pdf" in urls
        # Portal fallback links
        assert "https://portal.county.gov/codes/ch10-geothermal.pdf" in urls
        # Revize site links
        assert any("cms2.revize.com" in u for u in urls)

    def test_js_heavy_page_multiple_domain_sources(self):
        """JS-heavy fixture includes links from multiple domains."""
        links = _links_from_fixture("js_heavy_portal.html")

        urls = [l["url"] for l in links]
        hosts = set()
        for url in urls:
            host = urlparse(url).hostname
            if host:
                hosts.add(host)

        # Fixture has at least portal.county.gov and cms2.revize.com
        assert len(hosts) >= 2

    def test_js_heavy_page_with_multiple_domain_allowlist(self):
        """Allow links from specific trusted domains in JS-heavy page."""
        links = _links_from_fixture("js_heavy_portal.html")

        all_urls = [l["url"] for l in links]

        # Allow both portal.county.gov and cms2.revize.com
        artifacts = _discover_seed_urls(
            all_urls,
            max_pages=20,
            allowed_domains=["portal.county.gov", "cms2.revize.com"],
        )

        # All artifacts should be from allowed domains
        for artifact in artifacts:
            host = urlparse(artifact.url).hostname or ""
            assert any(
                host == domain or host.endswith("." + domain)
                for domain in ["portal.county.gov", "cms2.revize.com"]
            )


class TestDiggerWithMixedContentFixture:
    """Test digger with mixed content portal fixture (multiple doc types)."""

    def test_mixed_content_hub_multiple_document_types(self):
        """Hub page fixture contains multiple document types."""
        links = _links_from_fixture("mixed_content_hub.html")

        urls = [l["url"] for l in links]

        # Should find PDFs, DOCX, and static HTML
        assert any(u.endswith(".pdf") for u in urls)
        assert any(u.endswith(".docx") for u in urls)
        assert any(u.endswith(".html") for u in urls)
        # Fixture intentionally has external domain link
        assert any("external-site.com" in u for u in urls)

    def test_mixed_content_filter_supported_document_types(self):
        """Filter mixed content hub for supported product formats only."""
        links = _links_from_fixture("mixed_content_hub.html")

        all_urls = [l["url"] for l in links]

        # Filter for supported formats (pdf, docx, doc, txt, xlsx, csv)
        artifacts = _discover_seed_urls(
            all_urls,
            include_url_patterns=[r"\.(pdf|docx|doc|txt|xlsx|csv)$"],
        )

        # All artifacts should match supported formats
        assert all(
            a.url.endswith((".pdf", ".docx", ".doc", ".txt", ".xlsx", ".csv"))
            for a in artifacts
        )
        # Hub fixture has multiple supported types
        assert len(artifacts) >= 3

    def test_mixed_content_discovery_with_category_keywords(self):
        """Digger with keyword-based filtering on mixed content hub."""
        links = _links_from_fixture("mixed_content_hub.html")

        all_urls = [l["url"] for l in links]

        # Filter for 'geothermal' keyword in URLs
        artifacts = _discover_seed_urls(
            all_urls,
            include_url_patterns=[r"geothermal"],
        )

        # All artifacts should contain 'geothermal' in URL
        assert all("geothermal" in a.url.lower() for a in artifacts)


class TestDiggerWithCentralizedIndexSweepFixture:
    """Test digger sweep mode with centralized index page fixture."""

    def test_centralized_index_page_structure(self):
        """Centralized index fixture represents a complete document hub."""
        links = _links_from_fixture("centralized_index_sweep.html")

        # Index should have significant number of links
        assert len(links) >= 10

        urls = [l["url"] for l in links]

        # Fixture should have geothermal, solar, and tariff documents
        geothermal = [u for u in urls if "geothermal" in u.lower()]
        solar = [u for u in urls if "solar" in u.lower()]
        tariff = [u for u in urls if "tariff" in u.lower()]

        assert len(geothermal) >= 3
        assert len(solar) >= 2
        assert len(tariff) >= 2

    def test_sweep_mode_with_centralized_index_fixture(self):
        """Sweep mode discovers links from centralized index page."""
        links = _links_from_fixture("centralized_index_sweep.html")

        # Extract links from fixture to simulate index page discovery
        index_links = [
            {"url": l["url"], "text": l["text"]} for l in links
        ]

        # Create digger input in sweep mode with fixture links as index
        artifacts = _discover_seed_urls(
            ["https://docs.county.gov/index.html"],
            max_depth=2,
            max_pages=50,
            max_files=30,
            timeout_seconds=60,
            include_url_patterns=[r"\.pdf$"],
            extra_params={
                "index_page_mode": {
                    "enabled": True,
                    "collect_all_matching_links": True,
                },
                "index_links": index_links,
            },
        )

        # Should discover PDF links from sweep
        assert all(a.url.endswith(".pdf") for a in artifacts)
        assert artifacts[0].metadata["discovery_mode"] == "centralized_index_sweep"
        # Fixture has multiple PDFs
        assert len(artifacts) >= 5

    def test_sweep_mode_geothermal_filtering_from_index(self):
        """Sweep mode with geothermal-specific filtering on index."""
        links = _links_from_fixture("centralized_index_sweep.html")

        index_links = [
            {"url": l["url"], "text": l["text"]} for l in links
        ]

        # Filter to geothermal PDFs only
        artifacts = _discover_seed_urls(
            ["https://docs.county.gov/index.html"],
            max_depth=2,
            max_pages=50,
            max_files=30,
            timeout_seconds=60,
            include_url_patterns=[r"geothermal.*\.pdf$", r"53007\.pdf$"],
            include_link_text_patterns=[r"geothermal"],
            extra_params={
                "index_page_mode": {
                    "enabled": True,
                    "collect_all_matching_links": True,
                },
                "index_links": index_links,
            },
        )

        # All artifacts should be geothermal-related
        assert all(
            "geothermal" in a.url.lower() or "53007" in a.url
            for a in artifacts
        )
        assert len(artifacts) >= 2


class TestDiggerBudgetEnforcementWithFixtures:
    """Test budget enforcement (pages, files, timeout) with fixture scenarios."""

    def test_budget_page_limit_with_many_links(self):
        """Page budget should limit discovered artifacts."""
        links = _links_from_fixture("centralized_index_sweep.html")

        all_urls = [l["url"] for l in links]

        # Create digger with strict page limit
        artifacts = _discover_seed_urls(
            all_urls,
            max_pages=3,
            max_files=20,
        )

        # Should respect page limit
        assert len(artifacts) <= 3
        assert artifacts[0].metadata["budget_item_limit"] == 3

    def test_budget_file_limit_stricter_than_page_limit(self):
        """File limit stricter than page limit should apply."""
        links = _links_from_fixture("centralized_index_sweep.html")

        all_urls = [l["url"] for l in links]

        # File limit (2) stricter than page limit (5)
        artifacts = _discover_seed_urls(
            all_urls,
            max_pages=5,
            max_files=2,
        )

        # Should respect stricter file limit
        assert len(artifacts) <= 2
        assert artifacts[0].metadata["budget_item_limit"] == 2

    def test_timeout_zero_blocks_all_discovery(self):
        """Zero timeout should prevent any discovery."""
        links = _links_from_fixture("centralized_index_sweep.html")

        all_urls = [l["url"] for l in links]

        # Zero timeout
        artifacts = _discover_seed_urls(
            all_urls,
            max_pages=50,
            max_files=50,
            timeout_seconds=0,
        )

        # Zero timeout should block discovery
        assert len(artifacts) == 0


class TestDiggerFilterCombinationsWithFixtures:
    """Test realistic filter combinations on fixture pages."""

    def test_domain_allowlist_plus_extension_plus_keyword(self):
        """Complex filtering: domain + extension (OR logic with multiple patterns)."""
        links = _links_from_fixture("centralized_index_sweep.html")

        all_urls = [l["url"] for l in links]

        # Filter: docs.county.gov domain, PDFs only
        # Note: multiple patterns are applied as OR logic, not AND
        artifacts = _discover_seed_urls(
            all_urls,
            max_pages=50,
            max_files=30,
            allowed_domains=["docs.county.gov"],
            include_url_patterns=[r"\.pdf$"],
        )

        # Validate constraints: domain allowlist + pdf extension
        for artifact in artifacts:
            assert "docs.county.gov" in artifact.url or artifact.url.startswith("https://docs.county.gov")
            assert artifact.url.endswith(".pdf")
        
        # Should discover multiple PDFs from the docs.county.gov domain
        assert len(artifacts) >= 3


class TestDiggerMetadataAccuracy:
    """Test that digger metadata is accurate with fixture scenarios."""

    def test_discovery_mode_tracking(self):
        """Discovery mode metadata should reflect actual discovery path."""
        links = _links_from_fixture("mixed_content_hub.html")

        all_urls = [l["url"] for l in links]

        artifacts = _discover_seed_urls(all_urls)

        # All should be seed_only mode (not swept)
        assert all(a.metadata["discovery_mode"] == "seed_only" for a in artifacts)

    def test_budget_metadata_completeness(self):
        """All budget metadata should be present in artifacts."""
        links = _links_from_fixture("static_ordinance_page.html")

        all_urls = [l["url"] for l in links]

        artifacts = _discover_seed_urls(
            all_urls,
            max_depth=2,
            max_files=5,
            timeout_seconds=45,
        )

        # Check metadata completeness
        for artifact in artifacts:
            assert "max_depth" in artifact.metadata
            assert "max_pages" in artifact.metadata
            assert "max_files" in artifact.metadata
            assert "timeout_seconds" in artifact.metadata
            assert "budget_item_limit" in artifact.metadata
            assert "elapsed_seconds" in artifact.metadata
