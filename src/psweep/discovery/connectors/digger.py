"""Digger connector abstraction and lightweight provider resolver."""

from __future__ import annotations

from html.parser import HTMLParser
import re
import time
from urllib.parse import urljoin, urlparse

from ..constants import DOWNLOADABLE_EXTENSIONS
from ..retry import compute_backoff, is_transient_error
from ..urls import url_extension
from .base import BaseDiggerConnector, DiggerArtifact, DiggerInput


class NullDiggerConnector(BaseDiggerConnector):
    """
    Default digger connector placeholder.

    This connector keeps the runtime contract stable while concrete
    crawlers (for example Crawlee/Playwright) are implemented in later slices.
    """

    @staticmethod
    def _normalize_budget(value: int, *, minimum: int = 0) -> int:
        return max(minimum, int(value))

    @staticmethod
    def _effective_item_limit(
        candidate_urls: list[str], digger_input: DiggerInput
    ) -> int:
        max_pages = NullDiggerConnector._normalize_budget(
            digger_input.max_pages
        )
        max_files = NullDiggerConnector._normalize_budget(
            digger_input.max_files
        )
        return min(len(candidate_urls), max_pages, max_files)

    @staticmethod
    def _matches_allowed_domain(
        url: str, allowed_domains: list[str] | None
    ) -> bool:
        """Return True if the URL's host is within any entry in allowed_domains.

        Matching rules:
        - If allowed_domains is None or empty, all URLs are allowed.
        - Exact match: host == domain entry.
        - Subdomain match: host ends with '.' + domain entry.
        """
        if not allowed_domains:
            return True
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            return False
        host_lower = host.lower()
        for domain in allowed_domains:
            d = (domain or "").strip().lower()
            if not d:
                continue
            if host_lower == d or host_lower.endswith("." + d):
                return True
        return False

    @staticmethod
    def _matches_file_filter(
        url: str, include_url_patterns: list[str] | None
    ) -> bool:
        """Return True if the URL matches any of the include_url_patterns.

        If include_url_patterns is None or empty, all URLs pass.
        """
        if not include_url_patterns:
            return True
        return any(
            re.search(pattern, url, flags=re.IGNORECASE) is not None
            for pattern in include_url_patterns
            if isinstance(pattern, str) and pattern
        )

    @staticmethod
    def _matches_include_patterns(
        *,
        url: str,
        link_text: str,
        include_url_patterns: list[str] | None,
        include_link_text_patterns: list[str] | None,
    ) -> bool:
        url_patterns = include_url_patterns or []
        text_patterns = include_link_text_patterns or []

        url_match = True
        if url_patterns:
            url_match = any(
                re.search(pattern, url, flags=re.IGNORECASE) is not None
                for pattern in url_patterns
                if isinstance(pattern, str) and pattern
            )

        text_match = True
        if text_patterns:
            text_match = any(
                re.search(pattern, link_text, flags=re.IGNORECASE) is not None
                for pattern in text_patterns
                if isinstance(pattern, str) and pattern
            )

        return url_match and text_match

    @staticmethod
    def _resolve_candidate_urls(
        digger_input: DiggerInput,
    ) -> tuple[list[str], str]:
        extra_params = (
            digger_input.extra_params
            if isinstance(digger_input.extra_params, dict)
            else {}
        )
        index_page_mode = extra_params.get("index_page_mode")

        mode_enabled = False
        collect_all_matching_links = False
        if isinstance(index_page_mode, dict):
            mode_enabled = bool(index_page_mode.get("enabled"))
            collect_all_matching_links = bool(
                index_page_mode.get("collect_all_matching_links")
            )

        if not (mode_enabled and collect_all_matching_links):
            # Not in sweep mode: apply domain allowlist and file filter to seeds.
            filtered = [
                u
                for u in digger_input.seed_urls
                if NullDiggerConnector._matches_allowed_domain(
                    u, digger_input.allowed_domains
                )
                and NullDiggerConnector._matches_file_filter(
                    u, digger_input.include_url_patterns
                )
            ]
            return filtered, "seed_only"

        index_links = extra_params.get("index_links")
        if not isinstance(index_links, list):
            return digger_input.seed_urls, "seed_only"

        sweep_urls: list[str] = []
        for raw_link in index_links:
            if isinstance(raw_link, str):
                url = raw_link
                link_text = ""
            elif isinstance(raw_link, dict):
                url = str(raw_link.get("url") or "")
                link_text = str(raw_link.get("text") or "")
            else:
                continue

            if not url:
                continue
            if not url.lower().startswith(("http://", "https://")):
                continue

            if not NullDiggerConnector._matches_include_patterns(
                url=url,
                link_text=link_text,
                include_url_patterns=digger_input.include_url_patterns,
                include_link_text_patterns=digger_input.include_link_text_patterns,
            ):
                continue

            sweep_urls.append(url)

        if sweep_urls:
            # Preserve order while dropping duplicates.
            deduped: list[str] = []
            seen: set[str] = set()
            for url in sweep_urls:
                if url in seen:
                    continue
                seen.add(url)
                deduped.append(url)
            # Apply domain allowlist to sweep candidates.
            allowed = [
                u
                for u in deduped
                if NullDiggerConnector._matches_allowed_domain(
                    u, digger_input.allowed_domains
                )
            ]
            if allowed:
                return allowed, "centralized_index_sweep"

        seed_candidates = digger_input.seed_urls
        # Apply domain allowlist and file filter to seed fallback.
        seed_candidates = [
            u
            for u in seed_candidates
            if NullDiggerConnector._matches_allowed_domain(
                u, digger_input.allowed_domains
            )
            and NullDiggerConnector._matches_file_filter(
                u, digger_input.include_url_patterns
            )
        ]
        return seed_candidates, "seed_only"

    def discover(self, digger_input: DiggerInput) -> list[DiggerArtifact]:
        effective_max_depth = self._normalize_budget(digger_input.max_depth)
        effective_max_pages = self._normalize_budget(digger_input.max_pages)
        effective_max_files = self._normalize_budget(digger_input.max_files)
        effective_timeout_seconds = self._normalize_budget(
            digger_input.timeout_seconds
        )
        candidate_urls, discovery_mode = self._resolve_candidate_urls(
            digger_input
        )
        item_limit = self._effective_item_limit(candidate_urls, digger_input)

        started_at = time.monotonic()
        artifacts: list[DiggerArtifact] = []
        for index, seed_url in enumerate(candidate_urls):
            elapsed_seconds = time.monotonic() - started_at
            if elapsed_seconds >= float(effective_timeout_seconds):
                break
            if index >= item_limit:
                break

            artifacts.append(
                DiggerArtifact(
                    url=seed_url,
                    source="null_digger",
                    status="seed_staged",
                    metadata={
                        "discovery_mode": discovery_mode,
                        "max_depth": effective_max_depth,
                        "max_pages": effective_max_pages,
                        "max_files": effective_max_files,
                        "timeout_seconds": effective_timeout_seconds,
                        "budget_item_limit": item_limit,
                        "elapsed_seconds": round(elapsed_seconds, 6),
                    },
                )
            )
        return artifacts

    def supports_provider(self, provider: str) -> bool:
        return provider.lower() in {"null", "seed_only", "none"}


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
    """HTTP-based digger provider for real discovery from live hub/seed pages."""

    _DEFAULT_REQUEST_HEADERS = {
        "User-Agent": "ParseSweep/2.0 (+discovery)"
    }
    _USER_AGENT = "ParseSweep/2.0 (+discovery)"

    @staticmethod
    def _normalize_budget(value: int, *, minimum: int = 0) -> int:
        return max(minimum, int(value))

    @staticmethod
    def _is_http_url(url: str) -> bool:
        return bool(re.match(r"^https?://", url or "", flags=re.IGNORECASE))

    @classmethod
    def _is_document_url(cls, url: str) -> bool:
        # Canonical extension parser (shared with the candidate selector) so all
        # "is this a fetchable file?" checks agree on how an extension is read.
        return url_extension(url) in DOWNLOADABLE_EXTENSIONS

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
        headers = dict(cls._DEFAULT_REQUEST_HEADERS)
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

    _is_transient_error = staticmethod(is_transient_error)

    @staticmethod
    def _backoff_for_attempt(
        *, attempt: int, initial_backoff: float, max_backoff: float
    ) -> float:
        return compute_backoff(
            attempt,
            initial_backoff_seconds=initial_backoff,
            max_backoff_seconds=max_backoff,
        )

    def _fetch_html(
        self,
        *,
        url: str,
        timeout_seconds: int,
        ssl_verify: bool,
        request_headers: dict[str, str],
        retry_config: tuple[int, float, float],
    ) -> tuple[str, int]:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError(
                "requests dependency is required for HTTP digger provider"
            ) from exc

        max_attempts, initial_backoff, max_backoff = retry_config
        last_exc: BaseException | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.get(
                    url,
                    timeout=max(1, timeout_seconds),
                    allow_redirects=True,
                    verify=ssl_verify,
                    headers=request_headers,
                )
                response.raise_for_status()
                content_type = str(
                    (response.headers or {}).get("Content-Type") or ""
                ).lower()
                if "html" not in content_type and "text/" not in content_type:
                    return "", attempt
                return response.text or "", attempt
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts or not self._is_transient_error(
                    exc
                ):
                    raise
                delay = self._backoff_for_attempt(
                    attempt=attempt + 1,
                    initial_backoff=initial_backoff,
                    max_backoff=max_backoff,
                )
                if delay > 0:
                    time.sleep(delay)

        if last_exc is not None:
            raise last_exc
        return "", 0

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

    @staticmethod
    def _passes_filters(
        *,
        url: str,
        link_text: str,
        digger_input: DiggerInput,
        require_document_match: bool,
    ) -> bool:
        if not NullDiggerConnector._matches_allowed_domain(
            url, digger_input.allowed_domains
        ):
            return False

        if require_document_match:
            return NullDiggerConnector._matches_include_patterns(
                url=url,
                link_text=link_text,
                include_url_patterns=digger_input.include_url_patterns,
                include_link_text_patterns=digger_input.include_link_text_patterns,
            )

        return True

    def _discover_centralized(
        self,
        *,
        digger_input: DiggerInput,
        timeout_seconds: int,
        ssl_verify: bool,
        retry_config: tuple[int, float, float],
        request_headers: dict[str, str],
        max_pages: int,
        max_files: int,
    ) -> tuple[list[DiggerArtifact], int]:
        extra_params = (
            digger_input.extra_params
            if isinstance(digger_input.extra_params, dict)
            else {}
        )
        raw_links = extra_params.get("index_links")
        seed_pages = list(digger_input.seed_urls)

        candidate_links: list[dict[str, str]] = []
        pages_fetched = 0
        if isinstance(raw_links, list):
            for item in raw_links:
                if isinstance(item, str):
                    candidate_links.append({"url": item, "text": ""})
                elif isinstance(item, dict):
                    candidate_links.append(
                        {
                            "url": str(item.get("url") or ""),
                            "text": str(item.get("text") or ""),
                        }
                    )
        else:
            for page_url in seed_pages:
                if pages_fetched >= max_pages:
                    break
                if not self._is_http_url(page_url):
                    continue
                try:
                    html_text, _ = self._fetch_html(
                        url=page_url,
                        timeout_seconds=timeout_seconds,
                        ssl_verify=ssl_verify,
                        request_headers=request_headers,
                        retry_config=retry_config,
                    )
                except Exception:
                    continue
                pages_fetched += 1
                if html_text:
                    candidate_links.extend(
                        self._extract_links(html_text, page_url)
                    )

        artifacts: list[DiggerArtifact] = []
        seen_urls: set[str] = set()
        for link in candidate_links:
            url = str(link.get("url") or "")
            link_text = str(link.get("text") or "")
            if not self._is_http_url(url):
                continue
            if url in seen_urls:
                continue
            if not self._passes_filters(
                url=url,
                link_text=link_text,
                digger_input=digger_input,
                require_document_match=True,
            ):
                continue

            seen_urls.add(url)
            artifacts.append(
                DiggerArtifact(
                    url=url,
                    source="http_digger",
                    status="link_discovered",
                    metadata={
                        "discovery_mode": "centralized_index_sweep",
                        "link_text": link_text,
                        "pages_fetched": pages_fetched,
                    },
                )
            )
            if len(artifacts) >= max_files:
                break

        return artifacts, pages_fetched

    def _discover_distributed(
        self,
        *,
        digger_input: DiggerInput,
        timeout_seconds: int,
        ssl_verify: bool,
        retry_config: tuple[int, float, float],
        request_headers: dict[str, str],
        max_depth: int,
        max_pages: int,
        max_files: int,
    ) -> tuple[list[DiggerArtifact], int]:
        # Each queue item carries its originating seed URL so discovered
        # child documents can be attributed back to the target that seeded
        # the crawl (target provenance for distributed/crawl domains).
        queue: list[tuple[str, int, str]] = [
            (url, 0, url) for url in digger_input.seed_urls
        ]
        visited: set[str] = set()
        seen_artifacts: set[str] = set()
        artifacts: list[DiggerArtifact] = []
        pages_fetched = 0

        while (
            queue and pages_fetched < max_pages and len(artifacts) < max_files
        ):
            current_url, depth, origin_seed = queue.pop(0)
            if current_url in visited:
                continue
            visited.add(current_url)

            if not self._is_http_url(current_url):
                continue

            if self._is_document_url(current_url):
                if (
                    self._passes_filters(
                        url=current_url,
                        link_text="",
                        digger_input=digger_input,
                        require_document_match=True,
                    )
                    and current_url not in seen_artifacts
                ):
                    seen_artifacts.add(current_url)
                    artifacts.append(
                        DiggerArtifact(
                            url=current_url,
                            source="http_digger",
                            status="seed_staged",
                            metadata={
                                "discovery_mode": "distributed_http",
                                "source_seed": origin_seed,
                            },
                        )
                    )
                continue

            if depth >= max_depth:
                continue

            try:
                html_text, _ = self._fetch_html(
                    url=current_url,
                    timeout_seconds=timeout_seconds,
                    ssl_verify=ssl_verify,
                    request_headers=request_headers,
                    retry_config=retry_config,
                )
            except Exception:
                continue

            pages_fetched += 1
            if not html_text:
                continue

            for link in self._extract_links(html_text, current_url):
                url = link["url"]
                link_text = link["text"]
                if not NullDiggerConnector._matches_allowed_domain(
                    url, digger_input.allowed_domains
                ):
                    continue

                if self._is_document_url(url):
                    if not self._passes_filters(
                        url=url,
                        link_text=link_text,
                        digger_input=digger_input,
                        require_document_match=True,
                    ):
                        continue
                    if url in seen_artifacts:
                        continue
                    seen_artifacts.add(url)
                    artifacts.append(
                        DiggerArtifact(
                            url=url,
                            source="http_digger",
                            status="link_discovered",
                            metadata={
                                "discovery_mode": "distributed_http",
                                "link_text": link_text,
                                "source_seed": origin_seed,
                            },
                        )
                    )
                    if len(artifacts) >= max_files:
                        break
                    continue

                if depth + 1 < max_depth and url not in visited:
                    queue.append((url, depth + 1, origin_seed))

        return artifacts, pages_fetched

    def discover(self, digger_input: DiggerInput) -> list[DiggerArtifact]:
        started_at = time.monotonic()
        effective_max_depth = self._normalize_budget(digger_input.max_depth)
        effective_max_pages = self._normalize_budget(digger_input.max_pages)
        effective_max_files = self._normalize_budget(digger_input.max_files)
        effective_timeout_seconds = self._normalize_budget(
            digger_input.timeout_seconds
        )
        extra_params = (
            digger_input.extra_params
            if isinstance(digger_input.extra_params, dict)
            else {}
        )

        ssl_verify = bool(extra_params.get("ssl_verify", True))
        retry_config = self._resolve_retry_config(extra_params)
        request_headers = self._resolve_request_headers(extra_params)

        index_page_mode = (
            extra_params.get("index_page_mode")
            if isinstance(extra_params, dict)
            else None
        )
        sweep_enabled = (
            isinstance(index_page_mode, dict)
            and bool(index_page_mode.get("enabled"))
            and bool(index_page_mode.get("collect_all_matching_links"))
        )

        if (
            effective_timeout_seconds <= 0
            or effective_max_pages <= 0
            or effective_max_files <= 0
        ):
            return []

        if sweep_enabled:
            artifacts, pages_fetched = self._discover_centralized(
                digger_input=digger_input,
                timeout_seconds=effective_timeout_seconds,
                ssl_verify=ssl_verify,
                retry_config=retry_config,
                request_headers=request_headers,
                max_pages=effective_max_pages,
                max_files=effective_max_files,
            )
        else:
            artifacts, pages_fetched = self._discover_distributed(
                digger_input=digger_input,
                timeout_seconds=effective_timeout_seconds,
                ssl_verify=ssl_verify,
                retry_config=retry_config,
                request_headers=request_headers,
                max_depth=effective_max_depth,
                max_pages=effective_max_pages,
                max_files=effective_max_files,
            )

        elapsed = round(time.monotonic() - started_at, 6)
        for artifact in artifacts:
            metadata = dict(artifact.metadata or {})
            metadata.update(
                {
                    "max_depth": effective_max_depth,
                    "max_pages": effective_max_pages,
                    "max_files": effective_max_files,
                    "timeout_seconds": effective_timeout_seconds,
                    "pages_fetched": pages_fetched,
                    "elapsed_seconds": elapsed,
                }
            )
            artifact.metadata = metadata
        return artifacts

    def supports_provider(self, provider: str) -> bool:
        return provider.lower() in {
            "http",
            "requests",
            "basic_crawl",
        }


class SeleniumDiggerConnector(BaseDiggerConnector):
    """Browser-based digger for bot-protected sites (Akamai/Cloudflare).

    Drives a real headless Chrome (via :mod:`..browser`) to crawl pages that
    reject requests-based clients at the edge, extracting document links with
    the same filtering the HTTP digger uses (including extension-less links via
    ``include_url_patterns``). Discovered artifacts carry ``source_seed``
    provenance. Downloads for these sites must also go through the browser
    (``browser_mode: true``) since edge managers fingerprint the TLS layer.
    """

    _PROVIDERS = {
        "selenium",
        "browser",
        "chrome",
        "crawlee",
        "crawlee_playwright",
        "playwright",
    }

    def supports_provider(self, provider: str) -> bool:
        return provider.lower() in self._PROVIDERS

    def discover(self, digger_input: DiggerInput) -> list[DiggerArtifact]:
        from ..browser import BrowserSession

        max_depth = max(0, int(digger_input.max_depth))
        max_pages = max(0, int(digger_input.max_pages))
        max_files = max(0, int(digger_input.max_files))
        if max_pages <= 0 or max_files <= 0:
            return []

        artifacts: list[DiggerArtifact] = []
        seen: set[str] = set()
        visited: set[str] = set()
        pages_fetched = 0

        def _stage_document(url: str, seed: str, mode: str) -> None:
            if url in seen:
                return
            if not HttpDiggerConnector._passes_filters(
                url=url,
                link_text="",
                digger_input=digger_input,
                require_document_match=True,
            ):
                return
            seen.add(url)
            artifacts.append(
                DiggerArtifact(
                    url=url,
                    source="selenium_digger",
                    status=mode,
                    metadata={
                        "discovery_mode": "browser",
                        "source_seed": seed,
                    },
                )
            )

        def _is_doc_link(url: str) -> bool:
            # A link is a document if it has a document extension OR matches a
            # configured include pattern (e.g. "showpublisheddocument" for DNN
            # sites whose permit links carry no file extension).
            if HttpDiggerConnector._is_document_url(url):
                return True
            patterns = digger_input.include_url_patterns or []
            lowered = url.lower()
            return any(str(p).lower() in lowered for p in patterns)

        with BrowserSession() as browser:
            queue: list[tuple[str, int, str]] = [
                (url, 0, url) for url in digger_input.seed_urls
            ]
            while (
                queue
                and pages_fetched < max_pages
                and len(artifacts) < max_files
            ):
                current_url, depth, seed = queue.pop(0)
                if current_url in visited:
                    continue
                visited.add(current_url)
                if not HttpDiggerConnector._is_http_url(current_url):
                    continue

                if _is_doc_link(current_url):
                    _stage_document(current_url, seed, "seed_staged")
                    continue
                if depth >= max_depth:
                    continue

                try:
                    browser.fetch_html(current_url)
                except Exception:  # noqa: BLE001 - skip unreachable page
                    continue
                pages_fetched += 1

                for link in browser.current_links():
                    url = link["url"]
                    if not NullDiggerConnector._matches_allowed_domain(
                        url, digger_input.allowed_domains
                    ):
                        continue
                    if _is_doc_link(url):
                        _stage_document(url, seed, "link_discovered")
                        if len(artifacts) >= max_files:
                            break
                    elif depth + 1 < max_depth and url not in visited:
                        queue.append((url, depth + 1, seed))
        return artifacts


def resolve_digger_connector(
    provider: str | None = None,
) -> BaseDiggerConnector:
    """Resolve digger connector by provider name."""
    normalized = (provider or "null").strip().lower()

    for connector in (
        NullDiggerConnector(),
        HttpDiggerConnector(),
        SeleniumDiggerConnector(),
    ):
        if connector.supports_provider(normalized):
            return connector

    raise ValueError(
        f"Unsupported digger provider '{provider}'. Supported providers: "
        "null, seed_only, none, http, requests, basic_crawl, "
        "selenium, browser, chrome, crawlee, crawlee_playwright, playwright."
    )
