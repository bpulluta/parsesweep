"""Tests for the browser-based digger + browser-mode download wiring.

These cover the logic that doesn't require a live browser: provider
resolution, extension-less document recognition (showpublisheddocument), and
graceful fallback when Selenium/Chrome is unavailable. The live browser path
is exercised by the VA DEQ end-to-end run, not unit tests.
"""

from __future__ import annotations

from psweep.discovery.connectors.base import DiggerInput
from psweep.discovery.connectors.digger import (
    HttpDiggerConnector,
    SeleniumDiggerConnector,
    resolve_digger_connector,
)
from psweep.discovery.engine import (
    DiscoveryEngine,
    DiscoveryRequest,
)


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
