"""SerpApi-based seeker connector for web search discovery."""

from __future__ import annotations

import os
import re
import time
from typing import Any

from ..retry import compute_backoff, is_transient_error
from .base import BaseSeekerConnector, SeekerInput


class SerpApiSeeker(BaseSeekerConnector):
    """
    SerpApi-based seeker connector for Google Search discovery.

    Normalizes SerpApi organic search results to acquisition candidates.
    Handles API key management and error scenarios gracefully.
    """

    def __init__(
        self,
        api_key: str | None = None,
        ssl_verify: bool | None = None,
        retry_max_attempts: int = 3,
        retry_initial_backoff_seconds: float = 1.0,
        retry_max_backoff_seconds: float = 8.0,
        min_request_interval_seconds: float = 0.0,
        cache_dir: str | None = None,
        cache_ttl_seconds: float = 0.0,
    ):
        """
        Initialize SerpApi seeker connector.

        Args:
            api_key: Optional override for SERPAPI_API_KEY env var.
                     If not provided, falls back to SERPAPI_API_KEY or SERPAPI_KEY.
            ssl_verify: Optional override for TLS verification behavior.
                        If omitted, defaults to env-driven value from
                        SERPAPI_SSL_VERIFY / PSWEEP_SSL_VERIFY (default: false).
        """
        self.api_key = (
            api_key or os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")
        )
        self.ssl_verify = self._resolve_ssl_verify(ssl_verify)
        self.retry_max_attempts = max(1, int(retry_max_attempts))
        self.retry_initial_backoff_seconds = max(
            0.0, float(retry_initial_backoff_seconds)
        )
        self.retry_max_backoff_seconds = max(
            0.0, float(retry_max_backoff_seconds)
        )
        self.min_request_interval_seconds = max(
            0.0, float(min_request_interval_seconds)
        )
        self._last_request_monotonic = 0.0
        # Optional on-disk result cache so re-running acquire during tuning does
        # not re-pay SerpApi for identical queries. Keyed on the request params.
        self.cache_dir = cache_dir
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._ensure_prepared()

    def _respect_rate_limit(self) -> None:
        if self.min_request_interval_seconds <= 0:
            return

        now = time.monotonic()
        elapsed = now - self._last_request_monotonic
        remaining = self.min_request_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
            now = time.monotonic()
        self._last_request_monotonic = now

    _is_transient_search_error = staticmethod(is_transient_error)

    def _backoff_for_attempt(self, attempt: int) -> float:
        return compute_backoff(
            attempt,
            initial_backoff_seconds=self.retry_initial_backoff_seconds,
            max_backoff_seconds=self.retry_max_backoff_seconds,
        )

    def _execute_search_with_retry(
        self, search_callable, params: dict[str, Any]
    ) -> dict[str, Any]:
        last_exc: BaseException | None = None

        for attempt in range(1, self.retry_max_attempts + 1):
            try:
                self._respect_rate_limit()
                return search_callable(params)
            except Exception as exc:
                last_exc = exc
                if (
                    attempt >= self.retry_max_attempts
                    or not self._is_transient_search_error(exc)
                ):
                    raise
                delay = self._backoff_for_attempt(attempt + 1)
                if delay > 0:
                    time.sleep(delay)

        if last_exc is not None:
            raise last_exc
        return {}

    @staticmethod
    def _resolve_ssl_verify(explicit: bool | None) -> bool:
        """Resolve SSL verification behavior from explicit arg or environment."""
        if explicit is not None:
            return bool(explicit)

        raw_value = (
            os.getenv("SERPAPI_SSL_VERIFY")
            or os.getenv("PSWEEP_SSL_VERIFY")
            or "false"
        )
        normalized = str(raw_value).strip().lower()
        return normalized not in {"0", "false", "no", "off"}

    def _ensure_prepared(self) -> None:
        """Verify SerpApi is available and configured."""
        try:
            import serpapi  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "SerpApi seeker requires optional 'serpapi' dependency. "
                "Install with: pixi install"
            )

        if not self.api_key:
            raise ValueError(
                "SerpApi connector requires SERPAPI_API_KEY or SERPAPI_KEY environment variable or api_key parameter."
            )

    def discover(self, seeker_input: SeekerInput) -> list[dict[str, Any]]:
        """
        Execute search query against Google via SerpApi.

        Normalizes organic search results to acquisition candidates.

        Args:
            seeker_input: Query and constraints

        Returns:
            List of normalized candidate dictionaries with url, source, title, snippet.

        Raises:
            ValueError: If query is empty or invalid.
            RuntimeError: If SerpApi API call fails.
        """
        template_context = {}
        if isinstance(seeker_input.extra_params, dict):
            maybe_context = seeker_input.extra_params.get("template_context")
            if isinstance(maybe_context, dict):
                template_context = {
                    str(key): str(value)
                    for key, value in maybe_context.items()
                    if value is not None
                }

        rendered_queries = self._resolve_search_queries(
            seeker_input, template_context
        )
        if not rendered_queries:
            raise ValueError(
                "Seeker query cannot be resolved. Provide --query or query family templates."
            )

        search_callable = self._build_search_callable()

        deduped_candidates: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for rendered_query in rendered_queries:
            try:
                params = self._build_search_params(
                    seeker_input, rendered_query
                )
                results = self._cache_get(params)
                if results is None:
                    results = self._execute_search_with_retry(
                        search_callable, params
                    )
                    self._cache_put(params, results)
            except Exception as exc:
                sanitized_message = self._sanitize_error_message(str(exc))
                raise RuntimeError(
                    f"SerpApi search failed for query '{rendered_query}': {sanitized_message}"
                ) from exc

            for candidate in self._normalize_results(results, rendered_query):
                url = candidate.get("url")
                if not isinstance(url, str) or not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                deduped_candidates.append(candidate)

        return deduped_candidates

    # -- result cache ------------------------------------------------------

    def _cache_path(self, params: dict[str, Any]):
        """Return the cache file path for a query's params, or None if disabled.

        Keyed on the request params with the API key removed so the same query
        maps to the same file across runs (and never persists a credential).
        """
        if not self.cache_dir:
            return None
        import hashlib
        import json
        from pathlib import Path

        safe = {k: v for k, v in params.items() if k != "api_key"}
        blob = json.dumps(safe, sort_keys=True, default=str)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
        return Path(self.cache_dir) / f"{digest}.json"

    def _cache_get(self, params: dict[str, Any]) -> dict[str, Any] | None:
        path = self._cache_path(params)
        if path is None or not path.exists():
            return None
        if self.cache_ttl_seconds > 0:
            import time

            if time.time() - path.stat().st_mtime > self.cache_ttl_seconds:
                return None
        try:
            import json

            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _cache_put(self, params: dict[str, Any], results: dict[str, Any]) -> None:
        path = self._cache_path(params)
        if path is None:
            return
        try:
            import json

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(results, default=str), encoding="utf-8"
            )
        except (OSError, TypeError):
            return

    def _sanitize_error_message(self, message: str) -> str:
        """Redact SerpApi credentials from exception text before surfacing it."""
        sanitized = str(message)
        sanitized = re.sub(
            r"([?&]api_key=)[^&\s')]+",
            r"\1<redacted>",
            sanitized,
            flags=re.IGNORECASE,
        )
        if self.api_key:
            sanitized = sanitized.replace(self.api_key, "<redacted>")
        return sanitized

    def _build_search_callable(self):
        """Return a search callable compatible with available SerpApi client APIs."""
        if not self.ssl_verify:
            return self._search_via_http

        try:
            from serpapi import Client

            try:
                client = Client(api_key=self.api_key)
            except Exception as exc:
                raise RuntimeError(
                    f"SerpApi initialization failed: {exc}"
                ) from exc

            return client.search
        except ImportError:
            pass

        try:
            from serpapi import GoogleSearch
        except ImportError as exc:
            raise RuntimeError(
                "SerpApi seeker requires optional 'serpapi' dependency."
            ) from exc

        def _search_with_google_search(
            params: dict[str, Any],
        ) -> dict[str, Any]:
            request_params = dict(params)
            request_params["api_key"] = self.api_key
            return GoogleSearch(request_params).get_dict()

        return _search_with_google_search

    def _search_via_http(self, params: dict[str, Any]) -> dict[str, Any]:
        """Issue a direct SerpApi HTTP request with explicit TLS verify control."""
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError(
                "requests dependency is required for SerpApi HTTP fallback"
            ) from exc

        request_params = dict(params)
        request_params["api_key"] = self.api_key

        response = requests.get(
            "https://serpapi.com/search.json",
            params=request_params,
            timeout=45,
            verify=self.ssl_verify,
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _build_search_params(
        seeker_input: SeekerInput,
        rendered_query: str | None = None,
    ) -> dict[str, Any]:
        """Build SerpApi search parameters."""
        params = {
            "q": rendered_query
            if rendered_query is not None
            else seeker_input.query,
            "engine": "google",
            "num": seeker_input.max_results,
        }

        # Engine control keys consumed by query resolution — never forward these
        # to SerpApi.
        control_keys = {
            "template_context",
            "query_templates",
            "query_families",
            "use_query_family",
            "serpapi_params",
        }

        if seeker_input.extra_params:
            for key, value in seeker_input.extra_params.items():
                if key in control_keys:
                    continue
                params[key] = value

            # Verbatim SerpApi params (e.g. {"tbm": "nws"} for Google News,
            # {"tbs": "qdr:y"} for recency). Merged last so they win.
            passthrough = seeker_input.extra_params.get("serpapi_params")
            if isinstance(passthrough, dict):
                for key, value in passthrough.items():
                    params[str(key)] = value

        return params

    @staticmethod
    def _render_query_template(template: str, context: dict[str, str]) -> str:
        """
        Render a query template containing required and optional tokens.

        Supported syntax:
        - {field}: required token, raises when missing
        - {field?}: optional token, removed when missing
        """
        rendered = template
        token_pattern = re.compile(r"\{([a-zA-Z0-9_]+)(\?)?\}")

        def replace_token(match: re.Match[str]) -> str:
            token_name = match.group(1)
            optional = match.group(2) == "?"

            value = context.get(token_name)
            if value is None or not str(value).strip():
                if optional:
                    return ""
                raise ValueError(
                    f"Missing required template token: {token_name}"
                )
            return str(value).strip()

        rendered = token_pattern.sub(replace_token, rendered)
        rendered = re.sub(r"\s+", " ", rendered).strip()
        return rendered

    @staticmethod
    def _resolve_search_queries(
        seeker_input: SeekerInput,
        template_context: dict[str, str],
    ) -> list[str]:
        """
        Resolve one or more rendered search queries.

        Resolution order:
        1. Explicit seeker_input.query (highest priority)
        2. Selected query family templates (`use_query_family` + `query_families`)
        3. Top-level query_templates fallback
        """
        rendered_queries: list[str] = []
        raw_query = (seeker_input.query or "").strip()
        extra_params = (
            seeker_input.extra_params
            if isinstance(seeker_input.extra_params, dict)
            else {}
        )

        if raw_query:
            rendered = SerpApiSeeker._render_query_template(
                raw_query, template_context
            )
            if rendered:
                return [rendered]

        templates_to_try: list[str] = []
        use_query_family = str(
            extra_params.get("use_query_family") or ""
        ).strip()
        query_families = extra_params.get("query_families")
        if use_query_family and isinstance(query_families, dict):
            family_templates = query_families.get(use_query_family)
            if isinstance(family_templates, list):
                templates_to_try.extend(
                    str(template)
                    for template in family_templates
                    if isinstance(template, str)
                )

        if not templates_to_try:
            top_level_templates = extra_params.get("query_templates")
            if isinstance(top_level_templates, list):
                templates_to_try.extend(
                    str(template)
                    for template in top_level_templates
                    if isinstance(template, str)
                )

        for template in templates_to_try:
            try:
                rendered = SerpApiSeeker._render_query_template(
                    template, template_context
                )
            except ValueError:
                # Skip templates that require missing hints and continue with broader fallback.
                continue
            if rendered:
                rendered_queries.append(rendered)

        # Preserve order while deduplicating rendered queries.
        deduped_queries: list[str] = []
        seen_queries: set[str] = set()
        for query in rendered_queries:
            if query in seen_queries:
                continue
            seen_queries.add(query)
            deduped_queries.append(query)
        return deduped_queries

    @staticmethod
    def _normalize_results(
        serpapi_response: dict[str, Any],
        query: str,
    ) -> list[dict[str, Any]]:
        """
        Normalize SerpApi organic results to acquisition candidate format.

        Extracts organic results ensuring proper URL validation.
        Tracks source and reasoning for scoring stage.
        """
        candidates: list[dict[str, Any]] = []
        organic_results = serpapi_response.get("organic_results", [])

        for idx, result in enumerate(organic_results):
            url = result.get("link")
            if not url or not SerpApiSeeker._is_valid_url(url):
                continue

            title = result.get("title", "")
            snippet = result.get("snippet", "")

            reasons = [
                f"SerpApi result rank {idx + 1} for query '{query}'",
            ]
            if snippet and len(snippet) > 0:
                reasons.append("Snippet match detected")

            candidate = {
                "url": url,
                "source": "serpapi_google",
                "title": title or None,
                "snippet": snippet or None,
                "reasons": reasons,
            }
            candidates.append(candidate)

        # Google News (tbm=nws / engine=google_news) returns news_results, each
        # carrying a published `date` — a high-value temporal signal. Flatten
        # any nested `stories` and fold the date into the candidate text so it is
        # preserved for scoring, selection, and downstream extraction.
        candidates.extend(
            SerpApiSeeker._normalize_news_results(serpapi_response, query)
        )

        return candidates

    @staticmethod
    def _normalize_news_results(
        serpapi_response: dict[str, Any],
        query: str,
    ) -> list[dict[str, Any]]:
        """Normalize SerpApi news_results (Google News) to candidate format."""
        candidates: list[dict[str, Any]] = []
        news_results = serpapi_response.get("news_results", [])
        if not isinstance(news_results, list):
            return candidates

        # Flatten one level of nested stories (topic clusters).
        flattened: list[dict[str, Any]] = []
        for item in news_results:
            if not isinstance(item, dict):
                continue
            stories = item.get("stories")
            if isinstance(stories, list) and stories:
                flattened.extend(s for s in stories if isinstance(s, dict))
            else:
                flattened.append(item)

        for idx, result in enumerate(flattened):
            url = result.get("link")
            if not url or not SerpApiSeeker._is_valid_url(url):
                continue

            title = result.get("title", "")
            snippet = result.get("snippet", "")
            date = result.get("date")
            source_name = result.get("source")
            if isinstance(source_name, dict):
                source_name = source_name.get("name")

            reasons = [f"SerpApi news result rank {idx + 1} for query '{query}'"]
            if date:
                reasons.append(f"Published date: {date}")
            if source_name:
                reasons.append(f"Source: {source_name}")

            candidates.append(
                {
                    "url": url,
                    "source": "serpapi_google_news",
                    "title": title or None,
                    "snippet": snippet or None,
                    "published_date": date or None,
                    "reasons": reasons,
                }
            )

        return candidates

    @staticmethod
    def _is_valid_url(url: str | None) -> bool:
        """Validate URL format."""
        if not url or not isinstance(url, str):
            return False
        return bool(re.match(r"^https?://", url, re.IGNORECASE))

    def supports_provider(self, provider: str) -> bool:
        """Check if this connector supports the provider."""
        return provider.lower() in {"serpapi", "serpapi_google"}
