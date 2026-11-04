"""SerpApi-based seeker connector for web search discovery."""

from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

from psweep.discovery.constants import RESERVED_SEARCH_PARAMS
from psweep.utils.file_io import atomic_json
from ..retry import compute_backoff, is_transient_error
from .base import BaseSeekerConnector, SeekerInput
from psweep.discovery.network import send_get
from psweep.security import resolve_tls_verification


_TEMPLATE_TOKEN = re.compile(r"\{([a-zA-Z0-9_]+)(\?)?\}")


class SerpApiSearchTimeout(RuntimeError):
    """Explicit incomplete search, carrying results from completed queries."""

    def __init__(
        self, candidates: list[dict[str, Any]], query_errors: list[str]
    ) -> None:
        self.candidates = candidates
        self.query_errors = query_errors
        super().__init__("; ".join(query_errors))


def _is_search_timeout(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    try:
        from requests.exceptions import Timeout
    except ImportError:
        return False
    return isinstance(exc, Timeout)


class SerpApiSeeker(BaseSeekerConnector):
    """
    SerpApi-based seeker connector for Google Search discovery.

    Normalizes SerpApi organic search results to discovery candidates.
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
        cache_refresh: bool = False,
        timeout_seconds: float = 45,
    ):
        """
        Initialize SerpApi seeker connector.

        Parameters
        ----------
        api_key : str | None
            Optional override for SERPAPI_API_KEY env var.
            If not provided, read from SERPAPI_API_KEY.
        ssl_verify : bool | None
            Optional override for TLS verification behavior.
            If omitted, defaults to env-driven value from
            SERPAPI_SSL_VERIFY / PSWEEP_SSL_VERIFY (verified by default).
        """
        self.api_key = (
            api_key or os.getenv("SERPAPI_API_KEY")
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
        # Live outbound attempts, including retries and failed/time-out requests.
        # Cache hits are free; actual provider billing for failures is unknown.
        self.queries_run: int = 0
        self.query_results: list[dict[str, Any]] = []
        # Optional on-disk result cache so re-running acquire during tuning does
        # not re-pay SerpApi for identical queries. Keyed on the request params.
        self.cache_dir = cache_dir
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        # When True, ignore existing cache entries on read (forcing a fresh
        # live fetch) while still writing new results. Drives the discover
        # ``--fresh`` "start fresh" semantics without discarding the cache
        # for subsequent tuning runs.
        self.cache_refresh = bool(cache_refresh)
        self.timeout_seconds = timeout_seconds
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
    def _is_transient_search_error(exc: Exception) -> bool:
        return _is_search_timeout(exc) or is_transient_error(exc)

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
                self.queries_run += 1
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
        """Resolve the public-only TLS compatibility option explicitly."""
        return resolve_tls_verification("SERPAPI_SSL_VERIFY", explicit)

    def _ensure_prepared(self) -> None:
        """Verify SerpApi credentials are explicitly configured."""
        if not self.api_key:
            raise ValueError(
                "SerpApi connector requires the SERPAPI_API_KEY environment variable or api_key parameter."
            )

    def discover(self, seeker_input: SeekerInput) -> list[dict[str, Any]]:
        """
        Execute search query against Google via SerpApi.

        Normalizes organic search results to discovery candidates.

        Parameters
        ----------
        seeker_input : SeekerInput
            Query and constraints

        Returns
        -------
        list[dict[str, Any]]
            List of normalized candidate dictionaries with url, source, title, snippet.

        Raises
        ------
        ValueError
            If query is empty or invalid.
        SerpApiSearchTimeout
            If any query exhausts timeout retries; includes completed results and
            explicit query errors, after attempting the remaining queries.
        RuntimeError
            If SerpApi fails for another reason; aborts without partial success.
        """
        rendered_queries = resolve_search_queries(seeker_input)
        if not rendered_queries:
            raise ValueError(
                "No search query could be rendered from the configured templates."
            )

        search_callable = self._build_search_callable()

        deduped_candidates: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        timeout_errors: list[str] = []
        for rendered_query in rendered_queries:
            try:
                params = self._build_search_params(
                    seeker_input, rendered_query
                )
                results = self._cache_get(params)
                cache_hit = results is not None
                if results is None:
                    results = self._execute_search_with_retry(
                        search_callable, params
                    )
                self._validate_response(results)
                if not cache_hit:
                    self._cache_put(params, results)
            except Exception as exc:
                sanitized_message = self._sanitize_error_message(str(exc))
                if _is_search_timeout(exc):
                    timeout_errors.append(
                        f"SerpApi search failed for query '{rendered_query}': "
                        f"{sanitized_message}"
                    )
                    continue
                raise RuntimeError(
                    f"SerpApi search failed for query '{rendered_query}': {sanitized_message}"
                ) from exc

            normalized = self._normalize_results(results, rendered_query)
            per_query: dict[str, dict[str, Any]] = {}
            for candidate in normalized:
                per_query.setdefault(candidate["url"], candidate)
            limited = list(per_query.values())[: seeker_input.max_results]
            self.query_results.append(
                {
                    "query": rendered_query,
                    "cache_hit": cache_hit,
                    "returned": len(normalized),
                    "unique": len(per_query),
                    "retained": len(limited),
                    "limit": seeker_input.max_results,
                }
            )
            for candidate in limited:
                url = candidate["url"]
                if url not in seen_urls:
                    seen_urls.add(url)
                    deduped_candidates.append(candidate)

        if timeout_errors:
            raise SerpApiSearchTimeout(deduped_candidates, timeout_errors)
        return deduped_candidates

    @staticmethod
    def _validate_response(response: Any) -> None:
        """Accept confirmed empty searches; reject errors and malformed results."""
        if not isinstance(response, dict):
            msg = "SerpApi response must be an object"
            raise TypeError(msg)
        if response.get("error"):
            metadata = response.get("search_metadata")
            information = response.get("search_information")
            confirmed_empty = (
                response["error"] == "Google hasn't returned any results for this query."
                and isinstance(metadata, dict)
                and metadata.get("status") == "Success"
                and isinstance(information, dict)
                and information.get("organic_results_state") == "Fully empty"
                and not response.get("organic_results")
                and not response.get("news_results")
            )
            if not confirmed_empty:
                raise RuntimeError(str(response["error"]))
        for field in ("organic_results", "news_results"):
            if field in response and (
                not isinstance(response[field], list)
                or not all(isinstance(row, dict) for row in response[field])
            ):
                msg = f"SerpApi response.{field} must be an array of objects"
                raise ValueError(msg)

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
        if self.cache_refresh:
            return None
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
            atomic_json(path, results)
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
        """Return the single verified, bounded SerpApi transport."""
        return self._search_via_http

    def _search_via_http(self, params: dict[str, Any]) -> dict[str, Any]:
        """Query SerpApi through the shared public-destination boundary."""
        request_params = dict(params)
        request_params["api_key"] = self.api_key

        response, _ = send_get(
            "https://serpapi.com/search.json?" + urlencode(request_params, doseq=True),
            timeout=self.timeout_seconds,
            verify=self.ssl_verify,
            gate=lambda url: urlsplit(url).scheme == "https"
            and urlsplit(url).hostname == "serpapi.com",
        )
        try:
            response.raise_for_status()
            return response.json()
        finally:
            response.close()

    @staticmethod
    def _build_search_params(
        seeker_input: SeekerInput, rendered_query: str,
    ) -> dict[str, Any]:
        """Build SerpApi params; ``search.params`` may not override reserved keys."""
        extra = seeker_input.params or {}
        reserved = RESERVED_SEARCH_PARAMS.intersection(extra)
        if reserved:
            msg = (
                "SerpApi reserved parameters cannot be overridden: "
                + ", ".join(sorted(reserved))
            )
            raise ValueError(msg)
        return {
            "engine": "google",
            **{str(key): value for key, value in extra.items()},
            "q": rendered_query,
            "num": seeker_input.max_results,
        }

    @staticmethod
    def _normalize_results(
        serpapi_response: dict[str, Any],
        query: str,
    ) -> list[dict[str, Any]]:
        """
        Normalize SerpApi organic results to discovery candidate format.

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


def resolve_search_queries(seeker_input: SeekerInput) -> list[str]:
    """Render each template against the context; skip ones missing required tokens."""
    context = seeker_input.context or {}
    rendered: dict[str, None] = {}
    for template in seeker_input.queries:
        try:
            query = render_query_template(template, context)
        except ValueError:
            continue
        if query:
            rendered[query] = None
    return list(rendered)


def render_query_template(template: str, context: dict[str, Any]) -> str:
    """Render ``{field}`` (required) and ``{field?}`` (optional) tokens."""

    def replace_token(match: re.Match[str]) -> str:
        value = context.get(match.group(1))
        if value is None or not str(value).strip():
            if match.group(2):
                return ""
            raise ValueError(
                f"Missing required template token: {match.group(1)}"
            )
        return str(value).strip()

    rendered = _TEMPLATE_TOKEN.sub(replace_token, template)
    return re.sub(r"\s+", " ", rendered).strip()
