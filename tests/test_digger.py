"""Crawl connector tests: network gating, browser rendering, and the shared BFS."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests
from psweep.discovery import network

from psweep.discovery.connectors import (
    DiggerInput,
    HttpDiggerConnector,
    resolve_digger_connector,
)
from psweep.discovery.connectors.digger import SeleniumDiggerConnector, crawl
from psweep.discovery.engine import DiscoveryEngine

from test_discovery_network_policy import Response


class FakeBrowser:
    """A navigation-only browser substitute with an observable final URL."""

    pages = {}
    navigations = []
    closed = False

    def __init__(self, **kwargs):
        self.options = kwargs
        self.current_url = ""

    def __enter__(self):
        self._start()
        return self

    def __exit__(self, *args):
        self.close()

    def _start(self):
        pass

    def fetch_html(self, url, **kwargs):
        self.navigations.append(url)
        final, html, links = self.pages[url]
        self.current_url = final
        self.links = links
        return html

    def current_links(self):
        return self.links

    def close(self):
        self.closed = True


def test_http_crawl_gates_children_without_filtering_hub_paths(monkeypatch):
    """Document include patterns cannot prevent visiting parent crawl hubs."""
    pages = {
        "https://county.gov/hub": '<a href="/allowed">Codes</a>'
        '<a href="/private">Private Codes</a>',
        "https://county.gov/allowed": '<a href="/doc.pdf">Document</a>',
    }
    calls = []
    waits = []
    checks = []

    def get(url, **kwargs):
        calls.append(url)
        return Response(url, text=pages[url])

    def gate(url):
        checks.append(url)
        return not url.endswith("/private")

    monkeypatch.setattr(network, "request_get", get)
    result = HttpDiggerConnector().discover(
        DiggerInput(
            sources=["https://county.gov/hub"],
            max_depth=3,
            max_pages=10,
            include_url_patterns=[r"\.pdf$"],
            extra_params={
                "request_gate": gate,
                "rate_limiter": SimpleNamespace(
                    wait=lambda: waits.append(True)
                ),
            },
        ),
    )
    assert calls == list(pages)
    assert "https://county.gov/private" in checks
    assert waits == [True, True]
    assert [artifact.url for artifact in result.artifacts] == [
        "https://county.gov/doc.pdf"
    ]


def test_http_redirect_to_denied_url_is_not_followed(monkeypatch):
    """Source-page redirects use the crawl's network guard."""
    response = Response(
        "https://county.gov/hub", status=302, location="/private"
    )
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return response

    monkeypatch.setattr(network, "request_get", get)
    result = HttpDiggerConnector().discover(
        DiggerInput(
            sources=[response.url],
            max_depth=1,
            max_pages=5,
            extra_params={"request_gate": lambda url: not url.endswith("/private")},
        ),
    )
    assert result.artifacts == []
    assert calls == [response.url]
    assert response.closed


def test_http_fetch_keeps_return_shape_and_verified_tls(monkeypatch):
    """The HTML fetch contract remains text and attempt count."""
    response = Response("https://county.gov/hub", text="html")
    options = []

    def get(url, **kwargs):
        options.append(kwargs)
        return response

    monkeypatch.setattr(network, "request_get", get)
    assert HttpDiggerConnector()._fetch_html(
        url=response.url,
        timeout_seconds=6,
        ssl_verify=True,
        request_headers={"X-App": "value"},
        retry_config=(1, 0, 0),
    ) == ("html", 1)
    assert options[0]["verify"] is True
    assert options[0]["timeout"] == 6
    assert options[0]["headers"] == {"X-App": "value"}
    assert response.closed


def test_http_redirect_resolves_links_against_landing_page(monkeypatch):
    """Relative child links use the observed HTTP landing-page directory."""
    calls = []
    responses = [
        Response(
            "https://county.gov/hub", status=302, location="/codes/index"
        ),
        Response(
            "https://county.gov/codes/index", text='<a href="doc.pdf">Doc</a>'
        ),
    ]

    def get(url, **kwargs):
        calls.append(url)
        return responses[len(calls) - 1]

    monkeypatch.setattr(network, "request_get", get)
    result = HttpDiggerConnector().discover(
        DiggerInput(
            sources=["https://county.gov/hub"], max_depth=1, max_pages=5,
        ),
    )
    assert [artifact.url for artifact in result.artifacts] == [
        "https://county.gov/codes/doc.pdf"
    ]
    assert all(response.closed for response in responses)


@pytest.mark.parametrize(
    "connector", [HttpDiggerConnector, SeleniumDiggerConnector]
)
def test_render_navigation_is_gated_and_paced(monkeypatch, connector):
    """Both Selenium crawling and HTTP rendering gate each navigation."""
    hub = "https://county.gov/hub"
    child = "https://county.gov/child"
    monkeypatch.setattr(FakeBrowser, "navigations", [])
    monkeypatch.setattr(
        FakeBrowser,
        "pages",
        {
            hub: (
                hub,
                '<a href="/child">Codes</a>',
                [{"url": child, "text": "Codes"}],
            ),
            child: (
                child,
                '<a href="/doc.pdf">Doc</a>',
                [{"url": "https://county.gov/doc.pdf", "text": "Doc"}],
            ),
        },
    )
    monkeypatch.setattr("psweep.discovery.browser.BrowserSession", FakeBrowser)
    monkeypatch.setattr(
        network,
        "request_get",
        lambda url, **_kwargs: Response(url, text="<app-root></app-root>"),
    )
    checks = []
    final_checks = []
    waits = []
    result = connector().discover(
        DiggerInput(
            sources=[hub],
            max_depth=3,
            max_pages=10,
            extra_params={
                "request_gate": checks.append,
                "response_url_gate": final_checks.append,
                "rate_limiter": SimpleNamespace(
                    wait=lambda: waits.append(True)
                ),
                "browser_escalation": True,
            },
        ),
    )
    assert FakeBrowser.navigations == [hub, child]
    assert checks.count(hub) >= 1
    assert checks.count(child) >= 1
    assert final_checks.count(hub) >= 1
    assert final_checks.count(child) >= 1
    assert len(waits) == (4 if connector is HttpDiggerConnector else 2)
    assert [artifact.url for artifact in result.artifacts] == [
        "https://county.gov/doc.pdf"
    ]


@pytest.mark.parametrize(
    "connector", [HttpDiggerConnector, SeleniumDiggerConnector]
)
def test_render_denied_final_url_discards_links(monkeypatch, connector):
    """Post-navigation checks reject documents from a denied landing page."""
    hub = "https://county.gov/hub"
    final = "https://outside.org/private"
    monkeypatch.setattr(FakeBrowser, "navigations", [])
    monkeypatch.setattr(
        FakeBrowser,
        "pages",
        {
            hub: (
                final,
                '<a href="https://county.gov/doc.pdf">Doc</a>',
                [{"url": "https://county.gov/doc.pdf", "text": "Doc"}],
            ),
        },
    )
    monkeypatch.setattr("psweep.discovery.browser.BrowserSession", FakeBrowser)
    monkeypatch.setattr(
        network,
        "request_get",
        lambda url, **_kwargs: Response(url, text="<app-root></app-root>"),
    )
    result = connector().discover(
        DiggerInput(
            sources=[hub],
            max_depth=1,
            max_pages=5,
            extra_params={
                "request_gate": lambda url: url != final,
                "browser_escalation": True,
            },
        ),
    )
    assert FakeBrowser.navigations == [hub]
    assert result.artifacts == []


@pytest.mark.parametrize(
    "connector", [HttpDiggerConnector, SeleniumDiggerConnector]
)
def test_strict_browser_policy_fails_before_navigation(monkeypatch, connector):
    """A strict boundary cannot claim to intercept browser subresources."""
    monkeypatch.setattr(FakeBrowser, "navigations", [])
    monkeypatch.setattr("psweep.discovery.browser.BrowserSession", FakeBrowser)
    monkeypatch.setattr(
        network,
        "request_get",
        lambda url, **_kwargs: Response(url, text="<app-root></app-root>"),
    )
    with pytest.raises(RuntimeError, match=r"subresource|intercept"):
        connector().discover(
            DiggerInput(
                sources=["https://county.gov/hub"],
                max_depth=1,
                max_pages=5,
                extra_params={
                    "browser_policy_strict": True,
                    "browser_escalation": True,
                },
            ),
        )
    assert FakeBrowser.navigations == []


@pytest.mark.parametrize(
    "connector",
    [HttpDiggerConnector, SeleniumDiggerConnector],
)
def test_browser_gate_denies_before_each_navigation(monkeypatch, connector):
    """Denied child pages are not navigated, including render escalation."""
    hub = "https://county.gov/hub"
    child = "https://county.gov/private"
    monkeypatch.setattr(FakeBrowser, "navigations", [])
    monkeypatch.setattr(
        FakeBrowser,
        "pages",
        {
            hub: (
                hub,
                '<a href="/private">Codes</a>',
                [{"url": child, "text": "Codes"}],
            ),
        },
    )
    monkeypatch.setattr("psweep.discovery.browser.BrowserSession", FakeBrowser)
    monkeypatch.setattr(
        network,
        "request_get",
        lambda url, **_kwargs: Response(url, text="<app-root></app-root>"),
    )
    result = connector().discover(
        DiggerInput(
            sources=[hub],
            max_depth=3,
            max_pages=10,
            extra_params={
                "request_gate": lambda url: url != child,
                "browser_escalation": True,
            },
        ),
    )
    assert FakeBrowser.navigations == [hub]
    assert result.artifacts == []


HUB = "https://county.gov/hub"
SITE = {
    HUB: [
        {"url": "https://county.gov/docs/a.pdf", "text": "Ordinance A"},
        {"url": "https://county.gov/showpublisheddocument/12", "text": "Permit"},
        {"url": "https://county.gov/codes", "text": "Codes"},
        {"url": "https://other.org/b.pdf", "text": "Outside"},
        {"url": "https://county.gov/home", "text": "Home"},
    ],
    "https://county.gov/codes": [
        {"url": "https://county.gov/codes/c.pdf", "text": "Chapter C"},
    ],
}


def _crawl(**overrides):
    fetched = []

    def fetch_links(url):
        fetched.append(url)
        return SITE.get(url)

    options = {"sources": [HUB], "max_depth": 2, "max_pages": 10}
    result = crawl(DiggerInput(**(options | overrides)), fetch_links, "test")
    return [a.url for a in result.artifacts], fetched, result.pages_fetched, result.artifacts


def test_max_depth_zero_fetches_nothing():
    urls, fetched, pages, _ = _crawl(max_depth=0)
    assert (urls, fetched, pages) == ([], [], 0)


def test_depth_one_fetches_only_sources():
    urls, fetched, _, _ = _crawl(max_depth=1)
    assert fetched == [HUB]
    assert urls == ["https://county.gov/docs/a.pdf"]


def test_depth_two_follows_pages_but_skips_navigation_links():
    urls, fetched, _, artifacts = _crawl(max_depth=2)
    assert fetched == [
        HUB, "https://county.gov/showpublisheddocument/12", "https://county.gov/codes"
    ]
    assert urls == ["https://county.gov/docs/a.pdf", "https://county.gov/codes/c.pdf"]
    assert {a.metadata["source_url"] for a in artifacts} == {HUB}


def test_shared_links_are_attributed_to_every_source_with_one_fetch():
    codes = "https://county.gov/codes"
    urls, fetched, _, artifacts = _crawl(sources=[HUB, codes])
    pairs = {(a.url, a.metadata["source_url"]) for a in artifacts}
    assert {("https://county.gov/codes/c.pdf", HUB), ("https://county.gov/codes/c.pdf", codes)} <= pairs
    assert fetched.count(codes) == 1


def test_unrestricted_crawl_stays_on_source_domains():
    urls, _, _, _ = _crawl(allowed_domains=["county.gov", "other.org"])
    assert "https://other.org/b.pdf" in urls
    assert "https://other.org/b.pdf" not in _crawl()[0]


def test_include_url_patterns_mark_extensionless_documents():
    urls, _, _, _ = _crawl(max_depth=1, include_url_patterns=["showpublisheddocument"])
    assert urls == ["https://county.gov/showpublisheddocument/12"]


def test_link_text_patterns_keep_matching_pages():
    urls, _, _, _ = _crawl(max_depth=1, include_link_text_patterns=["^codes$"])
    assert urls == ["https://county.gov/codes"]


def test_budgets_bound_pages_and_files():
    assert _crawl(max_pages=1)[1] == [HUB]
    assert _crawl(max_files=1)[0] == ["https://county.gov/docs/a.pdf"]


def _crawl_with_failures(failures, **overrides):
    def fetch_links(url):
        if url in failures:
            raise failures[url]
        return SITE.get(url)

    options = {"sources": [HUB], "max_depth": 2, "max_pages": 10} | overrides
    return crawl(DiggerInput(**options), fetch_links, "test")


def test_retryable_page_failure_leaves_source_incomplete():
    codes = "https://county.gov/codes"
    timeout = requests.exceptions.ConnectTimeout("timed out")
    result = _crawl_with_failures({codes: timeout}, sources=[HUB, codes])
    assert result.incomplete_sources == {HUB, codes}
    assert "https://county.gov/docs/a.pdf" in [a.url for a in result.artifacts]


def test_terminal_page_failure_completes_source():
    response = requests.Response()
    response.status_code = 404
    missing = requests.exceptions.HTTPError("404", response=response)
    result = _crawl_with_failures({"https://county.gov/codes": missing})
    assert result.incomplete_sources == set()


def test_exhausted_budgets_leave_sources_incomplete():
    assert crawl_incomplete(max_pages=1) == {HUB}
    assert crawl_incomplete(max_files=1) == {HUB}
    assert crawl_incomplete(max_files=3) == set()
    assert crawl_incomplete(max_depth=1, max_pages=1) == set()


def crawl_incomplete(**overrides):
    return _crawl_with_failures({}, **overrides).incomplete_sources


def test_document_sources_are_not_fetched():
    _, fetched, _, _ = _crawl(sources=["https://county.gov/docs/a.pdf"])
    assert fetched == []


def test_http_and_selenium_apply_identical_link_filters(monkeypatch):
    """Both providers feed the same ``crawl`` filters."""
    html = "".join(f'<a href="{link["url"]}">{link["text"]}</a>' for link in SITE[HUB])
    monkeypatch.setattr(network, "request_get", lambda url, **_k: Response(url, text=html))
    monkeypatch.setattr(FakeBrowser, "navigations", [])
    monkeypatch.setattr(FakeBrowser, "pages", {HUB: (HUB, html, SITE[HUB])})
    monkeypatch.setattr("psweep.discovery.browser.BrowserSession", FakeBrowser)
    digger_input = {
        "sources": [HUB],
        "max_depth": 1,
        "max_pages": 5,
        "include_url_patterns": [r"\.pdf$|showpublisheddocument"],
    }
    http = HttpDiggerConnector().discover(DiggerInput(**digger_input))
    selenium = SeleniumDiggerConnector().discover(DiggerInput(**digger_input))
    expected = ["https://county.gov/docs/a.pdf", "https://county.gov/showpublisheddocument/12"]
    assert [a.url for a in http.artifacts] == [a.url for a in selenium.artifacts] == expected


def test_resolve_digger_connector():
    assert isinstance(resolve_digger_connector("http"), HttpDiggerConnector)
    assert isinstance(resolve_digger_connector("selenium"), SeleniumDiggerConnector)
    with pytest.raises(ValueError, match="Unsupported crawl provider"):
        resolve_digger_connector("browser")


def test_open_browser_returns_none_when_unavailable(monkeypatch):
    import psweep.discovery.browser as browser_mod

    def _boom(self):
        raise browser_mod.BrowserUnavailableError("no chrome")

    monkeypatch.setattr(browser_mod.BrowserSession, "_start", _boom)
    session, note = DiscoveryEngine._open_browser_for_download()
    assert session is None
    assert "unavailable" in note.lower()
