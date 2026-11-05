"""Crawl connectors (HTTP and Selenium) sharing one bounded link-following loop."""

from __future__ import annotations

import contextlib
import re
from collections import deque
from html.parser import HTMLParser
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

from ..constants import DOCUMENT_EXTENSIONS, DOWNLOADABLE_EXTENSIONS, USER_AGENT
from psweep.discovery.network import (
    NetworkPolicyError, RateLimiter, URLGate, check_url, send_get,
)
from ..urls import (
    compile_url_patterns, is_http_url, matches_allowed_domain, url_extension,
)
from ..retry import browser_may_recover, is_transient_error
from .base import BaseDiggerConnector, CrawlResult, DiggerArtifact, DiggerInput

# Patterns that indicate an HTML response is a JS-rendered shell
# (Angular, React, Vue, Next.js) with no server-rendered content.
_JS_SHELL_MARKERS = re.compile(
    r"ng-app=|<app-root|__NEXT_DATA__|<div\s+id=[\"'](?:root|app|__next)[\"']\s*>",
    re.IGNORECASE,
)


def _is_js_shell(html_text: str) -> bool:
    """Return True if the HTML looks like a JS framework shell with no content."""
    if not html_text:
        return False
    # Quick structural check: if there are very few anchor tags AND a framework marker
    anchor_count = html_text.lower().count("<a ")
    if anchor_count > 5:
        return False  # Has real links, probably not a shell
    return bool(_JS_SHELL_MARKERS.search(html_text[:5000]))


# Navigation link texts to skip during crawling (case-insensitive).
# These waste page budget on non-content pages.
_NAVIGATION_SKIP_PATTERNS = re.compile(
    r"^(?:home|skip to|my (?:drafts|notes|saved|account)|sign (?:in|up|out)"
    r"|log ?(?:in|out)|search|contact|help|faq|about|privacy|terms"
    r"|cookie|feedback|order|cart|checkout|translate|share|print"
    r"|copyright|accessibility|back to top|menu|close|toggle)$",
    re.IGNORECASE,
)


def _is_navigation_link(link_text: str) -> bool:
    """Return True if link text looks like site navigation rather than content."""
    text = link_text.strip()
    if not text or len(text) > 100:
        return False
    return bool(_NAVIGATION_SKIP_PATTERNS.match(text))


def _acquisition_options(
    digger_input: DiggerInput,
    allowed_domains: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve network callbacks, not document include patterns."""
    extras = digger_input.extra_params or {}
    domains = allowed_domains or digger_input.allowed_domains

    def request_gate(url: str) -> bool:
        if not matches_allowed_domain(url, domains):
            return False
        check_url(url, extras.get("request_gate"))
        return True

    return {
        "request_gate": request_gate,
        "response_url_gate": extras.get("response_url_gate"),
        "rate_limiter": extras.get("rate_limiter"),
    }


class CrawlAbortError(RuntimeError):
    """A crawl-wide configuration failure; aborts instead of skipping a page."""


def _require_browser_policy_support(digger_input: DiggerInput) -> None:
    """Refuse strict browser flows before network navigation."""
    if (digger_input.extra_params or {}).get("browser_policy_strict"):
        message = (
            "Strict discovery network policy cannot intercept arbitrary "
            "browser redirects or subresource requests; use HTTP acquisition."
        )
        raise CrawlAbortError(message)


def _fetch_browser_html(
    browser: Any,
    url: str,
    *,
    request_gate: URLGate | None,
    response_url_gate: URLGate | None,
    rate_limiter: RateLimiter | None,
    **fetch_options: Any,
) -> tuple[str, str]:
    """Gate and pace navigation, then validate its landing URL."""
    check_url(url, request_gate)
    if rate_limiter is not None:
        rate_limiter.wait()
    html = browser.fetch_html(url, **fetch_options)
    final_url = str(browser.current_url or "")
    if not re.match(r"^https?://", final_url, flags=re.IGNORECASE):
        raise NetworkPolicyError(final_url, "missing browser landing URL")
    check_url(final_url, request_gate)
    check_url(final_url, response_url_gate)
    return html, final_url


def _matches_any(text: str, patterns: list[str] | None) -> bool:
    """Empty pattern lists match everything; patterns are case-insensitive regexes."""
    return not patterns or any(p.search(text) for p in compile_url_patterns(patterns))


def matches_include_patterns(url: str, link_text: str, digger_input: DiggerInput) -> bool:
    """Apply ``include_url_patterns`` to the URL and ``include_link_text_patterns`` to link text."""
    return _matches_any(url, digger_input.include_url_patterns) and _matches_any(
        link_text, digger_input.include_link_text_patterns
    )


def _is_document_link(url: str, digger_input: DiggerInput) -> bool:
    """A downloadable extension, or an extensionless URL matching ``include_url_patterns``."""
    return url_extension(url) in DOWNLOADABLE_EXTENSIONS or bool(
        digger_input.include_url_patterns
        and _matches_any(url, digger_input.include_url_patterns)
    )


def _source_domains(sources: list[str]) -> list[str] | None:
    """Scope an unrestricted crawl to the registrable domains of its sources."""
    domains = {
        ".".join(host.split(".")[-2:])
        for host in (urlparse(url).hostname for url in sources)
        if host
    }
    return sorted(domains) or None


LinkFetcher = Callable[[str], "list[dict[str, str]]"]


def crawl(
    digger_input: DiggerInput, fetch_links: LinkFetcher, source: str,
) -> CrawlResult:
    """Follow links breadth-first from each source page within the crawl budgets.

    Sources are pages at depth 0 (any non-document URL, including ``.html``);
    a page is fetched only while its depth is below ``max_depth``. Document
    links become artifacts attributed to their source URL. When link-text
    patterns are set, only matching pages are followed, and each is also an
    artifact (e.g. "Next" pagination of a listing). Deduplication is per source, so
    a link reachable from several sources is attributed to each; a page shared
    by several sources is fetched once. A source is incomplete when one of its
    pages failed with a retryable error or the page/file budget cut off a link
    it would have kept or followed.
    """
    allowed = digger_input.allowed_domains or _source_domains(digger_input.sources)
    save_pages = bool(digger_input.include_link_text_patterns)
    max_files = digger_input.max_files
    queue = deque((url, 0, url) for url in digger_input.sources)
    visited: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    fetched: dict[str, list[dict[str, str]] | None] = {}
    retryable_pages: set[str] = set()
    incomplete: set[str] = set()
    artifacts: list[DiggerArtifact] = []
    pages_fetched = 0

    def full() -> bool:
        return max_files is not None and len(artifacts) >= max_files

    def pending(url: str, depth: int, origin: str) -> bool:
        return (
            (url, origin) not in visited
            and depth < digger_input.max_depth
            and url_extension(url) not in DOCUMENT_EXTENSIONS
        )

    def classify(link: dict[str, str], depth: int, origin: str) -> tuple[bool, bool]:
        """Return whether a page link is a new artifact and a page to follow."""
        link_url, link_text = link["url"], link.get("text") or ""
        if not is_http_url(link_url) or not matches_allowed_domain(link_url, allowed):
            return False, False
        accepted = matches_include_patterns(link_url, link_text, digger_input)
        document = _is_document_link(link_url, digger_input)
        artifact = bool(
            (document or (save_pages and link_text))
            and accepted
            and (link_url, origin) not in seen
        )
        follow = (
            bool(accepted and link_text)
            if save_pages
            else not _is_navigation_link(link_text)
        )
        page = (
            follow
            and not document
            and depth + 1 < digger_input.max_depth
            and (link_url, origin) not in visited
        )
        return artifact, page

    while queue and pages_fetched < digger_input.max_pages and not full():
        url, depth, origin = queue.popleft()
        if not pending(url, depth, origin):
            continue
        visited.add((url, origin))
        if url not in fetched:
            try:
                fetched[url] = fetch_links(url)
                pages_fetched += 1
            except CrawlAbortError:
                raise
            except Exception as exc:  # noqa: BLE001 - classified, crawl continues
                fetched[url] = None
                if is_transient_error(exc):
                    retryable_pages.add(url)
        if url in retryable_pages:
            incomplete.add(origin)
        links = fetched[url] or []
        for position, link in enumerate(links, start=1):
            artifact, page = classify(link, depth, origin)
            if artifact:
                seen.add((link["url"], origin))
                artifacts.append(
                    DiggerArtifact(
                        url=link["url"],
                        source=source,
                        status="link_discovered",
                        metadata={
                            "link_text": link.get("text") or "",
                            "source_url": origin,
                        },
                    )
                )
                if full():
                    # Cut off only if a wanted link or page remains unvisited.
                    if page or any(
                        any(classify(rest, depth, origin)) for rest in links[position:]
                    ):
                        incomplete.add(origin)
                    break
            if page:
                queue.append((link["url"], depth + 1, origin))
    incomplete.update(origin for url, depth, origin in queue if pending(url, depth, origin))
    return CrawlResult(artifacts, incomplete, pages_fetched)


class _AnchorExtractor(HTMLParser):
    """Extract anchor href + visible text pairs from HTML documents."""

    def __init__(self) -> None:
        super().__init__()
        self._in_anchor = False
        self._current_href: str | None = None
        self._current_text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "a":
            return
        href = ""
        for key, value in attrs:
            if key.lower() == "href" and value:
                href = value
                break
        if not href:
            return
        self._in_anchor = True
        self._current_href = href
        self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_anchor and data:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._in_anchor or not self._current_href:
            return
        text = " ".join(
            part.strip() for part in self._current_text_parts if part.strip()
        )
        self.links.append((self._current_href, text))
        self._in_anchor = False
        self._current_href = None
        self._current_text_parts = []



class HttpDiggerConnector(BaseDiggerConnector):
    """Crawl over HTTP, escalating JS-rendered shells to a browser when enabled."""

    @staticmethod
    def _resolve_retry_config(
        extra_params: dict[str, object] | None,
    ) -> tuple[int, float, float]:
        retry_cfg = {}
        if isinstance(extra_params, dict) and isinstance(
            extra_params.get("retry"), dict
        ):
            retry_cfg = extra_params.get("retry") or {}

        max_attempts = int(retry_cfg.get("max_attempts", 3) or 3)
        initial_backoff = float(
            retry_cfg.get("initial_backoff_seconds", 1.0) or 1.0
        )
        max_backoff = float(retry_cfg.get("max_backoff_seconds", 8.0) or 8.0)

        return (
            max(1, max_attempts),
            max(0.0, initial_backoff),
            max(0.0, max_backoff),
        )

    @classmethod
    def _resolve_request_headers(
        cls, extra_params: dict[str, object] | None
    ) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT}
        raw_headers = (
            extra_params.get("request_headers")
            if isinstance(extra_params, dict)
            else None
        )
        if isinstance(raw_headers, dict):
            for key, value in raw_headers.items():
                normalized_key = str(key or "").strip()
                normalized_value = str(value or "").strip()
                if normalized_key and normalized_value:
                    headers[normalized_key] = normalized_value
        return headers

    def _fetch_html(
        self,
        *,
        url: str,
        timeout_seconds: int,
        ssl_verify: bool,
        request_headers: dict[str, str],
        retry_config: tuple[int, float, float],
        request_gate: URLGate | None = None,
        response_url_gate: URLGate | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> tuple[str, int]:
        """Fetch HTML through the boundary and release the response."""
        response, attempt = send_get(
            url,
            timeout=max(1, timeout_seconds),
            verify=ssl_verify,
            headers=request_headers,
            retry=retry_config,
            gate=request_gate,
            response_url_gate=response_url_gate,
            limiter=rate_limiter,
        )
        try:
            content_type = str(
                (response.headers or {}).get("Content-Type") or ""
            ).lower()
            if "html" not in content_type and "text/" not in content_type:
                return "", attempt
            return response.text or "", attempt
        finally:
            response.close()

    def _fetch_page_html(
        self, *, acquisition: dict[str, Any], **fetch_options: Any,
    ) -> tuple[str, str]:
        """Retain the landing URL for resolving relative links."""
        final_url = fetch_options["url"]

        def observe_url(url: str) -> None:
            nonlocal final_url
            check_url(url, acquisition.get("response_url_gate"))
            final_url = url

        html, _ = self._fetch_html(
            **fetch_options,
            **{**acquisition, "response_url_gate": observe_url},
        )
        return html, final_url

    @staticmethod
    def _extract_links(html_text: str, base_url: str) -> list[dict[str, str]]:
        parser = _AnchorExtractor()
        parser.feed(html_text)

        links: list[dict[str, str]] = []
        for href, text in parser.links:
            resolved = urljoin(base_url, href)
            if not re.match(r"^https?://", resolved, flags=re.IGNORECASE):
                continue
            links.append({"url": resolved, "text": text})
        return links

    def discover(self, digger_input: DiggerInput) -> CrawlResult:
        """Crawl source pages over HTTP and return discovered artifacts."""
        extra = digger_input.extra_params or {}
        acquisition = _acquisition_options(
            digger_input,
            digger_input.allowed_domains or _source_domains(digger_input.sources),
        )
        fetch_options = {
            "timeout_seconds": max(1, digger_input.timeout_seconds),
            "ssl_verify": bool(extra.get("ssl_verify", True)),
            "request_headers": self._resolve_request_headers(extra),
            "retry_config": self._resolve_retry_config(extra),
        }
        browser: Any = None
        browser_attempted = False

        def get_browser() -> Any:
            nonlocal browser, browser_attempted
            if browser is None and not browser_attempted:
                browser_attempted = True
                try:
                    from ..browser import BrowserSession

                    session = BrowserSession(
                        request_headers=fetch_options["request_headers"],
                        page_load_timeout=fetch_options["timeout_seconds"],
                    )
                    session._start()  # noqa: SLF001
                    browser = session
                except Exception:  # noqa: BLE001 - no browser available
                    browser = None
            return browser

        def fetch_links(url: str) -> list[dict[str, str]]:
            escalate = extra.get("browser_escalation") is True
            refused: Exception | None = None
            try:
                html_text, final_url = self._fetch_page_html(
                    url=url, acquisition=acquisition, **fetch_options,
                )
            except Exception as exc:
                if not (escalate and browser_may_recover(exc)):
                    raise
                html_text, final_url, refused = "", url, exc
            if escalate and (refused is not None or _is_js_shell(html_text)):
                _require_browser_policy_support(digger_input)
                session = get_browser()
                if session is None:
                    if refused is not None:
                        raise refused
                    return []
                html_text, final_url = _fetch_browser_html(
                    session, url, settle_seconds=4, **acquisition,
                )
            return self._extract_links(html_text, final_url) if html_text else []

        try:
            return crawl(digger_input, fetch_links, "http_digger")
        finally:
            if browser is not None:
                with contextlib.suppress(Exception):
                    browser.close()

    def supports_provider(self, provider: str) -> bool:
        """Return True for the ``http`` provider."""
        return provider == "http"


class SeleniumDiggerConnector(BaseDiggerConnector):
    """Crawl through headless Chrome for bot-protected sites.

    Uses the same link filters as HTTP; downloads from these sites also need
    ``crawl.browser_downloads`` since edge managers fingerprint TLS.
    """

    def supports_provider(self, provider: str) -> bool:
        """Return True for the ``selenium`` provider."""
        return provider == "selenium"

    def discover(self, digger_input: DiggerInput) -> CrawlResult:
        """Crawl source pages via a headless browser and return artifacts."""
        from ..browser import BrowserSession

        _require_browser_policy_support(digger_input)
        acquisition = _acquisition_options(
            digger_input,
            digger_input.allowed_domains or _source_domains(digger_input.sources),
        )
        with BrowserSession(
            request_headers=(digger_input.extra_params or {}).get("request_headers"),
            page_load_timeout=max(1, digger_input.timeout_seconds),
        ) as browser:

            def fetch_links(url: str) -> list[dict[str, str]]:
                _fetch_browser_html(browser, url, **acquisition)
                return browser.current_links()

            return crawl(digger_input, fetch_links, "selenium_digger")


def resolve_digger_connector(provider: str) -> BaseDiggerConnector:
    """Resolve the crawl connector for ``crawl.provider`` (http or selenium)."""
    for connector in (HttpDiggerConnector(), SeleniumDiggerConnector()):
        if connector.supports_provider(provider):
            return connector
    raise ValueError(
        f"Unsupported crawl provider '{provider}'. Supported: http, selenium."
    )
