"""Tests for digger connector abstraction and provider resolution."""

from __future__ import annotations

import pytest

from streamline_extract.acquisition.connectors import (
    BaseDiggerConnector,
    DiggerArtifact,
    DiggerInput,
    HttpDiggerConnector,
    NullDiggerConnector,
    resolve_digger_connector,
)


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

    def test_resolve_digger_connector_accepts_crawlee_alias(self):
        connector = resolve_digger_connector("crawlee_playwright")
        assert isinstance(connector, HttpDiggerConnector)

    def test_resolve_digger_connector_rejects_unknown_provider(self):
        with pytest.raises(ValueError, match="Unsupported digger provider"):
            resolve_digger_connector("mystery_provider")


class TestHttpDiggerConnector:
    def test_http_digger_fetches_index_page_links(self, monkeypatch):
        class FakeResponse:
            status_code = 200

            def __init__(self):
                self.headers = {"Content-Type": "text/html; charset=utf-8"}
                self.text = (
                    '<html><body>'
                    '<a href="/docs/geothermal-ordinance.pdf">Geothermal Ordinance</a>'
                    '<a href="/docs/notice.html">Notice</a>'
                    '</body></html>'
                )

            def raise_for_status(self):
                return None

        monkeypatch.setattr("requests.get", lambda *args, **kwargs: FakeResponse())

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


class TestDomainAllowlistAndFileFilter:
    """Validate domain allowlist and file filter enforcement."""

    # --- _matches_allowed_domain unit tests ---

    def test_no_allowlist_permits_all_urls(self):
        assert NullDiggerConnector._matches_allowed_domain(
            "https://example.org/doc.pdf", None
        )
        assert NullDiggerConnector._matches_allowed_domain(
            "https://anywhere.com/file.pdf", []
        )

    def test_exact_domain_match_is_allowed(self):
        assert NullDiggerConnector._matches_allowed_domain(
            "https://county.gov/doc.pdf", ["county.gov"]
        )

    def test_subdomain_match_is_allowed(self):
        assert NullDiggerConnector._matches_allowed_domain(
            "https://planning.county.gov/doc.pdf", ["county.gov"]
        )

    def test_unrelated_domain_is_rejected(self):
        assert not NullDiggerConnector._matches_allowed_domain(
            "https://evil.com/doc.pdf", ["county.gov"]
        )

    def test_partial_domain_suffix_is_not_allowed(self):
        # "notcounty.gov" must not match "county.gov"
        assert not NullDiggerConnector._matches_allowed_domain(
            "https://notcounty.gov/doc.pdf", ["county.gov"]
        )

    def test_multiple_allowed_domains_any_match_passes(self):
        allowed = ["state.gov", "county.gov"]
        assert NullDiggerConnector._matches_allowed_domain(
            "https://plans.county.gov/doc.pdf", allowed
        )
        assert NullDiggerConnector._matches_allowed_domain(
            "https://state.gov/data.pdf", allowed
        )
        assert not NullDiggerConnector._matches_allowed_domain(
            "https://other.org/doc.pdf", allowed
        )

    # --- _matches_file_filter unit tests ---

    def test_no_file_filter_permits_all_urls(self):
        assert NullDiggerConnector._matches_file_filter("https://example.org/doc.html", None)
        assert NullDiggerConnector._matches_file_filter("https://example.org/doc.html", [])

    def test_file_filter_matches_extension_pattern(self):
        assert NullDiggerConnector._matches_file_filter(
            "https://example.org/doc.pdf", [r"\.pdf$"]
        )
        assert not NullDiggerConnector._matches_file_filter(
            "https://example.org/doc.html", [r"\.pdf$"]
        )

    def test_file_filter_case_insensitive(self):
        assert NullDiggerConnector._matches_file_filter(
            "https://example.org/DOC.PDF", [r"\.pdf$"]
        )

    def test_file_filter_matches_specific_filename(self):
        assert NullDiggerConnector._matches_file_filter(
            "https://example.org/53007.pdf", [r"53007"]
        )
        assert not NullDiggerConnector._matches_file_filter(
            "https://example.org/other.pdf", [r"53007"]
        )

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

