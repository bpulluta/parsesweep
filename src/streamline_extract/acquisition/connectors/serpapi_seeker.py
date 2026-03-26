"""SerpApi-based seeker connector for web search discovery."""

from __future__ import annotations

import os
import re
import time
from typing import Any

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
    ):
        """
        Initialize SerpApi seeker connector.
        
        Args:
            api_key: Optional override for SERPAPI_API_KEY env var.
                     If not provided, falls back to SERPAPI_API_KEY or SERPAPI_KEY.
            ssl_verify: Optional override for TLS verification behavior.
                        If omitted, defaults to env-driven value from
                        SERPAPI_SSL_VERIFY / STREAMLINE_EXTRACT_SSL_VERIFY (default: true).
        """
        self.api_key = api_key or os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")
        self.ssl_verify = self._resolve_ssl_verify(ssl_verify)
        self.retry_max_attempts = max(1, int(retry_max_attempts))
        self.retry_initial_backoff_seconds = max(0.0, float(retry_initial_backoff_seconds))
        self.retry_max_backoff_seconds = max(0.0, float(retry_max_backoff_seconds))
        self.min_request_interval_seconds = max(0.0, float(min_request_interval_seconds))
        self._last_request_monotonic = 0.0
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

    @staticmethod
    def _is_transient_search_error(exc: BaseException) -> bool:
        lowered = str(exc).lower()
        markers = (
            "429",
            "rate limit",
            "timeout",
            "temporarily unavailable",
            "try again",
            "connection reset",
            "connection aborted",
            "connection refused",
            "ssl",
            "tls",
            "503",
            "504",
        )
        return any(marker in lowered for marker in markers)

    def _backoff_for_attempt(self, attempt: int) -> float:
        if attempt <= 1:
            return 0.0
        wait = self.retry_initial_backoff_seconds * (2 ** (attempt - 2))
        if self.retry_max_backoff_seconds <= 0:
            return max(0.0, wait)
        return min(wait, self.retry_max_backoff_seconds)

    def _execute_search_with_retry(self, search_callable, params: dict[str, Any]) -> dict[str, Any]:
        last_exc: BaseException | None = None

        for attempt in range(1, self.retry_max_attempts + 1):
            try:
                self._respect_rate_limit()
                return search_callable(params)
            except Exception as exc:
                last_exc = exc
                if attempt >= self.retry_max_attempts or not self._is_transient_search_error(exc):
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
            or os.getenv("STREAMLINE_EXTRACT_SSL_VERIFY")
            or "true"
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

        rendered_queries = self._resolve_search_queries(seeker_input, template_context)
        if not rendered_queries:
            raise ValueError(
                "Seeker query cannot be resolved. Provide --query or query family templates."
            )

        search_callable = self._build_search_callable()

        deduped_candidates: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for rendered_query in rendered_queries:
            try:
                params = self._build_search_params(seeker_input, rendered_query)
                results = self._execute_search_with_retry(search_callable, params)
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

    def _sanitize_error_message(self, message: str) -> str:
        """Redact SerpApi credentials from exception text before surfacing it."""
        sanitized = str(message)
        sanitized = re.sub(r"([?&]api_key=)[^&\s')]+", r"\1<redacted>", sanitized, flags=re.IGNORECASE)
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

        def _search_with_google_search(params: dict[str, Any]) -> dict[str, Any]:
            request_params = dict(params)
            request_params["api_key"] = self.api_key
            return GoogleSearch(request_params).get_dict()

        return _search_with_google_search

    def _search_via_http(self, params: dict[str, Any]) -> dict[str, Any]:
        """Issue a direct SerpApi HTTP request with explicit TLS verify control."""
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("requests dependency is required for SerpApi HTTP fallback") from exc

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
            "q": rendered_query if rendered_query is not None else seeker_input.query,
            "engine": "google",
            "num": seeker_input.max_results,
        }

        # Allow extra params (e.g., location, num_pages)
        if seeker_input.extra_params:
            for key, value in seeker_input.extra_params.items():
                if key == "template_context":
                    continue
                params[key] = value

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
            rendered = SerpApiSeeker._render_query_template(raw_query, template_context)
            if rendered:
                return [rendered]

        templates_to_try: list[str] = []
        use_query_family = str(extra_params.get("use_query_family") or "").strip()
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
                rendered = SerpApiSeeker._render_query_template(template, template_context)
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
                reasons.append(f"Snippet match detected")

            candidate = {
                "url": url,
                "source": "serpapi_google",
                "title": title or None,
                "snippet": snippet or None,
                "reasons": reasons,
            }
            candidates.append(candidate)

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
