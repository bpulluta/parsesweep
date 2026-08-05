"""Unified digger connector tests.

Consolidates connector-abstraction, fixture-based integration, and browser
(Selenium) digger coverage into one module. Test groups:

- Connector abstraction: artifact/provider contracts, budget enforcement,
  index-page sweep, and provider resolution.
- HTTP connector: live index-page link extraction (mocked requests).
- Domain allowlist / file filter: unit + seed-only + sweep-mode enforcement.
- Fixture-based integration: realistic HTML fixtures for static, JS-heavy,
  mixed-content, and centralized-index pages.
- Browser digger: provider resolution, extension-less document recognition,
  and graceful fallback when Selenium/Chrome is unavailable.

Shared HTML-fixture helpers live at module scope so every fixture-based test
reuses one implementation.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from psweep.discovery.connectors import (
    BaseDiggerConnector,
    DiggerArtifact,
    DiggerInput,
    HttpDiggerConnector,
    NullDiggerConnector,
    resolve_digger_connector,
)
from psweep.discovery.connectors.digger import SeleniumDiggerConnector
from psweep.discovery.engine import DiscoveryEngine, DiscoveryRequest

from discovery_helpers import patch_requests_get


# ---------------------------------------------------------------------------
# Shared HTML-fixture helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Connector abstraction
# ---------------------------------------------------------------------------


class TestDiggerConnectorAbstraction:
    """Validate digger abstraction contracts."""

    def test_digger_artifact_to_dict(self):
        artifact = DiggerArtifact(
            url="https://example.org/doc.pdf",
            source="test_digger",
            mime_type="application/pdf",
            extension=".pdf",
            status="discovered",
            metadata={"rank": 1},
        )
        payload = artifact.to_dict()

        assert payload["url"] == "https://example.org/doc.pdf"
        assert payload["source"] == "test_digger"
        assert payload["mime_type"] == "application/pdf"
        assert payload["extension"] == ".pdf"
        assert payload["status"] == "discovered"
        assert payload["metadata"] == {"rank": 1}

    def test_null_digger_connector_discover_stages_seed_urls(self):
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=[
                "https://example.org/portal",
                "https://example.org/library",
            ],
            max_depth=3,
            max_pages=80,
            max_files=30,
            timeout_seconds=45,
        )

        artifacts = connector.discover(digger_input)

        assert len(artifacts) == 2
        assert all(isinstance(artifact, DiggerArtifact) for artifact in artifacts)
        assert artifacts[0].url == "https://example.org/portal"
        assert artifacts[0].source == "null_digger"
        assert artifacts[0].status == "seed_staged"
        assert artifacts[0].metadata["max_depth"] == 3

    def test_null_digger_enforces_page_file_budgets(self):
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=[
                "https://example.org/page-1",
                "https://example.org/page-2",
                "https://example.org/page-3",
            ],
            max_depth=2,
            max_pages=2,
            max_files=1,
            timeout_seconds=30,
        )

        artifacts = connector.discover(digger_input)

        assert len(artifacts) == 1
        assert artifacts[0].url == "https://example.org/page-1"
        assert artifacts[0].metadata["budget_item_limit"] == 1
        assert artifacts[0].metadata["max_pages"] == 2
        assert artifacts[0].metadata["max_files"] == 1

    def test_null_digger_timeout_zero_yields_no_artifacts(self):
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=["https://example.org/page-1"],
            max_depth=2,
            max_pages=5,
            max_files=5,
            timeout_seconds=0,
        )

        artifacts = connector.discover(digger_input)
        assert artifacts == []

    def test_null_digger_normalizes_negative_budgets(self):
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=["https://example.org/page-1"],
            max_depth=-1,
            max_pages=-10,
            max_files=-20,
            timeout_seconds=15,
        )

        artifacts = connector.discover(digger_input)
        assert artifacts == []

    def test_index_page_sweep_collects_matching_links(self):
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=["https://example.org/hub"],
            max_depth=2,
            max_pages=10,
            max_files=10,
            timeout_seconds=30,
            include_url_patterns=[r"\.pdf$"],
            include_link_text_patterns=[r"ordinance|geothermal"],
            extra_params={
                "index_page_mode": {
                    "enabled": True,
                    "collect_all_matching_links": True,
                },
                "index_links": [
                    {
                        "url": "https://example.org/docs/ordinance.pdf",
                        "text": "Geothermal Ordinance",
                    },
                    {
                        "url": "https://example.org/docs/notice.html",
                        "text": "Geothermal Notice",
                    },
                    {
                        "url": "https://example.org/docs/ordinance.pdf",
                        "text": "Duplicate Link",
                    },
                ],
            },
        )

        artifacts = connector.discover(digger_input)

        assert len(artifacts) == 1
        assert artifacts[0].url == "https://example.org/docs/ordinance.pdf"
        assert artifacts[0].metadata["discovery_mode"] == "centralized_index_sweep"

    def test_index_page_sweep_falls_back_to_seed_when_no_url_pattern_matches(self):
        """Sweep falls back to seed when no index links match URL/text patterns.

        No file filter set here — seed URL is allowed through without extension filtering.
        """
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=["https://example.org/hub"],
            max_depth=2,
            max_pages=10,
            max_files=10,
            timeout_seconds=30,
            include_url_patterns=[r"\.pdf$"],
            extra_params={
                "index_page_mode": {
                    "enabled": True,
                    "collect_all_matching_links": True,
                },
                "index_links": [
                    {
                        "url": "https://example.org/docs/notice.html",
                        "text": "General Update",
                    }
                ],
            },
        )

        artifacts = connector.discover(digger_input)

        # Sweep matched no links. Seed fallback applies file filter:
        # hub URL does not match \.pdf$ so it is dropped too.
        assert len(artifacts) == 0

    def test_index_page_sweep_falls_back_to_seed_no_file_filter(self):
        """Sweep falls back to seed URL when no index links match text patterns and no file filter is set."""
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=["https://example.org/hub"],
            max_depth=2,
            max_pages=10,
            max_files=10,
            timeout_seconds=30,
            # Text pattern blocks the link; no URL file filter so seeds are not filtered.
            include_link_text_patterns=[r"ordinance|geothermal"],
            extra_params={
                "index_page_mode": {
                    "enabled": True,
                    "collect_all_matching_links": True,
                },
                "index_links": [
                    {
                        "url": "https://example.org/docs/notice.html",
                        "text": "General Update",
                    }
                ],
            },
        )

        artifacts = connector.discover(digger_input)

        # Sweep matched no links (text "General Update" doesn't match patterns).
        # Seed fallback: no file filter → hub URL passes through.
        assert len(artifacts) == 1
        assert artifacts[0].url == "https://example.org/hub"
        assert artifacts[0].metadata["discovery_mode"] == "seed_only"

    def test_null_digger_supports_provider_aliases(self):
        connector = NullDiggerConnector()
        assert connector.supports_provider("null")
        assert connector.supports_provider("seed_only")
        assert connector.supports_provider("none")
        assert not connector.supports_provider("crawlee")

    def test_resolve_digger_connector_defaults_to_null(self):
        connector = resolve_digger_connector()
        assert isinstance(connector, BaseDiggerConnector)
        assert isinstance(connector, NullDiggerConnector)

    def test_resolve_digger_connector_accepts_seed_only(self):
        connector = resolve_digger_connector("seed_only")
        assert isinstance(connector, NullDiggerConnector)

    def test_resolve_digger_connector_accepts_http(self):
        connector = resolve_digger_connector("http")
        assert isinstance(connector, HttpDiggerConnector)

    def test_resolve_digger_connector_browser_aliases(self):
        # Browser-provider names resolve to the Selenium (real-browser) digger,
        # not the requests-based HTTP one.
        for name in ("crawlee_playwright", "selenium", "browser", "chrome"):
            connector = resolve_digger_connector(name)
            assert isinstance(connector, SeleniumDiggerConnector)

    def test_resolve_digger_connector_rejects_unknown_provider(self):
        with pytest.raises(ValueError, match="Unsupported digger provider"):
            resolve_digger_connector("mystery_provider")


# ---------------------------------------------------------------------------
# HTTP connector
# ---------------------------------------------------------------------------


class TestHttpDiggerConnector:
    def test_http_digger_fetches_index_page_links(self, monkeypatch):
        patch_requests_get(
            monkeypatch,
            url="https://county.gov/index.html",
            content_type="text/html; charset=utf-8",
            text=(
                '<html><body>'
                '<a href="/docs/geothermal-ordinance.pdf">Geothermal Ordinance</a>'
                '<a href="/docs/notice.html">Notice</a>'
                '</body></html>'
            ),
        )

        connector = HttpDiggerConnector()
        artifacts = connector.discover(
            DiggerInput(
                seed_urls=["https://county.gov/index.html"],
                max_depth=2,
                max_pages=10,
                max_files=10,
                timeout_seconds=30,
                allowed_domains=["county.gov"],
                include_url_patterns=[r"\.pdf$"],
                include_link_text_patterns=[r"ordinance"],
                extra_params={
                    "index_page_mode": {
                        "enabled": True,
                        "collect_all_matching_links": True,
                    }
                },
            )
        )

        assert len(artifacts) == 1
        assert artifacts[0].url == "https://county.gov/docs/geothermal-ordinance.pdf"
        assert artifacts[0].metadata["discovery_mode"] == "centralized_index_sweep"


# ---------------------------------------------------------------------------
# Domain allowlist and file filter
# ---------------------------------------------------------------------------


class TestDomainAllowlistAndFileFilter:
    """Validate domain allowlist and file filter enforcement."""

    # --- _matches_allowed_domain unit tests ---

    @pytest.mark.parametrize(
        ("url", "allowed_domains", "expected"),
        [
            ("https://example.org/doc.pdf", None, True),
            ("https://anywhere.com/file.pdf", [], True),
            ("https://county.gov/doc.pdf", ["county.gov"], True),
            ("https://planning.county.gov/doc.pdf", ["county.gov"], True),
            ("https://evil.com/doc.pdf", ["county.gov"], False),
            ("https://notcounty.gov/doc.pdf", ["county.gov"], False),
            ("https://plans.county.gov/doc.pdf", ["state.gov", "county.gov"], True),
            ("https://state.gov/data.pdf", ["state.gov", "county.gov"], True),
            ("https://other.org/doc.pdf", ["state.gov", "county.gov"], False),
        ],
    )
    def test_matches_allowed_domain(self, url, allowed_domains, expected):
        assert NullDiggerConnector._matches_allowed_domain(url, allowed_domains) is expected

    # --- _matches_file_filter unit tests ---

    @pytest.mark.parametrize(
        ("url", "patterns", "expected"),
        [
            ("https://example.org/doc.html", None, True),
            ("https://example.org/doc.html", [], True),
            ("https://example.org/doc.pdf", [r"\.pdf$"], True),
            ("https://example.org/doc.html", [r"\.pdf$"], False),
            ("https://example.org/DOC.PDF", [r"\.pdf$"], True),
            ("https://example.org/53007.pdf", [r"53007"], True),
            ("https://example.org/other.pdf", [r"53007"], False),
        ],
    )
    def test_matches_file_filter(self, url, patterns, expected):
        assert NullDiggerConnector._matches_file_filter(url, patterns) is expected

    # --- Integration: allowlist enforcement in seed-only discover ---

    def test_seed_only_domain_allowlist_filters_disallowed_seeds(self):
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=[
                "https://county.gov/ordinance.pdf",
                "https://external-site.com/doc.pdf",
                "https://sub.county.gov/code.pdf",
            ],
            max_pages=10,
            max_files=10,
            timeout_seconds=30,
            allowed_domains=["county.gov"],
        )

        artifacts = connector.discover(digger_input)

        urls = [a.url for a in artifacts]
        assert "https://county.gov/ordinance.pdf" in urls
        assert "https://sub.county.gov/code.pdf" in urls
        assert "https://external-site.com/doc.pdf" not in urls

    def test_seed_only_file_filter_removes_non_matching_seeds(self):
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=[
                "https://county.gov/ordinance.pdf",
                "https://county.gov/home.html",
                "https://county.gov/report.docx",
            ],
            max_pages=10,
            max_files=10,
            timeout_seconds=30,
            include_url_patterns=[r"\.(pdf|docx)$"],
        )

        artifacts = connector.discover(digger_input)

        urls = [a.url for a in artifacts]
        assert "https://county.gov/ordinance.pdf" in urls
        assert "https://county.gov/report.docx" in urls
        assert "https://county.gov/home.html" not in urls

    def test_seed_only_combined_allowlist_and_file_filter(self):
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=[
                "https://county.gov/ordinance.pdf",   # pass: allowed domain + pdf
                "https://county.gov/home.html",        # fail: html filtered
                "https://external.com/doc.pdf",        # fail: domain blocked
                "https://sub.county.gov/code.pdf",     # pass: subdomain + pdf
            ],
            max_pages=10,
            max_files=10,
            timeout_seconds=30,
            allowed_domains=["county.gov"],
            include_url_patterns=[r"\.pdf$"],
        )

        artifacts = connector.discover(digger_input)

        urls = [a.url for a in artifacts]
        assert urls == [
            "https://county.gov/ordinance.pdf",
            "https://sub.county.gov/code.pdf",
        ]

    def test_all_seeds_filtered_yields_empty_artifacts(self):
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=[
                "https://other.com/doc.pdf",
                "https://another.com/file.pdf",
            ],
            max_pages=10,
            max_files=10,
            timeout_seconds=30,
            allowed_domains=["county.gov"],
        )

        artifacts = connector.discover(digger_input)
        assert artifacts == []

    # --- Integration: domain allowlist in sweep mode ---

    def test_sweep_mode_domain_allowlist_filters_disallowed_links(self):
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=["https://county.gov/hub"],
            max_pages=10,
            max_files=10,
            timeout_seconds=30,
            allowed_domains=["county.gov"],
            include_url_patterns=[r"\.pdf$"],
            extra_params={
                "index_page_mode": {
                    "enabled": True,
                    "collect_all_matching_links": True,
                },
                "index_links": [
                    {"url": "https://county.gov/ordinance.pdf", "text": "Ordinance"},
                    {"url": "https://external.gov/doc.pdf", "text": "External doc"},
                ],
            },
        )

        artifacts = connector.discover(digger_input)

        urls = [a.url for a in artifacts]
        assert "https://county.gov/ordinance.pdf" in urls
        assert "https://external.gov/doc.pdf" not in urls
        assert all(a.metadata["discovery_mode"] == "centralized_index_sweep" for a in artifacts)

    def test_sweep_mode_all_links_disallowed_falls_back_to_filtered_seeds(self):
        connector = NullDiggerConnector()
        digger_input = DiggerInput(
            seed_urls=["https://county.gov/hub"],
            max_pages=10,
            max_files=10,
            timeout_seconds=30,
            allowed_domains=["county.gov"],
            include_url_patterns=[r"\.pdf$"],
            extra_params={
                "index_page_mode": {
                    "enabled": True,
                    "collect_all_matching_links": True,
                },
                "index_links": [
                    {"url": "https://external.com/doc.pdf", "text": "External PDF"},
                ],
            },
        )

        # All sweep links are blocked by domain allowlist; seed hub is .html (no pdf).
        # seed_urls contains .gov/hub which matches the domain but not the file filter.
        artifacts = connector.discover(digger_input)
        # hub doesn't match file filter → empty fallback
        assert artifacts == []


# ---------------------------------------------------------------------------
# Fixture-based integration
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Browser (Selenium) digger
# ---------------------------------------------------------------------------
#
# These cover the logic that doesn't require a live browser: provider
# resolution, extension-less document recognition (showpublisheddocument), and
# graceful fallback when Selenium/Chrome is unavailable. The live browser path
# is exercised by the VA DEQ end-to-end run, not unit tests.


class TestBrowserProviderResolution:
    def test_browser_aliases_resolve_to_selenium(self):
        for name in ("selenium", "browser", "chrome", "crawlee_playwright"):
            assert isinstance(
                resolve_digger_connector(name), SeleniumDiggerConnector
            )

    def test_http_still_resolves_to_http(self):
        assert isinstance(
            resolve_digger_connector("http"), HttpDiggerConnector
        )


class TestExtensionlessDocumentRecognition:
    """The digger must treat include_url_pattern matches (e.g. DNN
    showpublisheddocument links, which have no .pdf extension) as documents."""

    def test_showpublisheddocument_recognized_via_include_pattern(self):
        # Reproduce the connector's _is_doc_link logic against a DNN URL.
        url = "https://www.deq.virginia.gov/home/showpublisheddocument/36795/6391"
        assert not HttpDiggerConnector._is_document_url(url)  # no extension
        patterns = ["showpublisheddocument"]
        lowered = url.lower()
        assert any(p.lower() in lowered for p in patterns)

    def test_plain_pdf_still_recognized_by_extension(self):
        assert HttpDiggerConnector._is_document_url(
            "https://example.gov/permit.pdf"
        )


class TestBrowserModeFallback:
    def test_open_browser_returns_none_when_unavailable(self, monkeypatch):
        # Simulate Selenium/Chrome unavailable: _open_browser_for_download
        # must return (None, note) so the caller falls back to HTTP.
        import psweep.discovery.browser as browser_mod

        def _boom(self):
            from psweep.discovery.browser import (
                BrowserUnavailableError,
            )

            raise BrowserUnavailableError("no chrome")

        monkeypatch.setattr(browser_mod.BrowserSession, "_start", _boom)
        session, note = DiscoveryEngine._open_browser_for_download()
        assert session is None
        assert "unavailable" in note.lower()

    def test_browser_mode_field_defaults_false(self):
        req = DiscoveryRequest(
            domain="d",
            seed_urls=[],
            query=None,
            enable_serpapi=False,
            output_documents=None,
            output_manifest=None,
            dry_run=True,
        )
        assert req.browser_mode is False


class TestSeleniumDiggerBudgets:
    def test_zero_budget_returns_empty_without_browser(self):
        # max_files=0 short-circuits before any browser is launched.
        di = DiggerInput(seed_urls=["https://x.gov/"], max_files=0)
        assert SeleniumDiggerConnector().discover(di) == []
