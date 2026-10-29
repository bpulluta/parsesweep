"""LLM-assisted page targeting for large documents.

Large source documents (e.g. a 600+ page tariff book) overflow the extraction
context and get truncated before the section you actually want. This module
finds the page range that holds a described section so extraction can target
only those pages.

Strategy (cheap first, LLM second):

1. Heuristic pre-filter — score each page by keyword hits and keep the dense
   pages plus a little context around them.
2. LLM confirm — send a compact index (page number + snippet) of just the
   candidate pages and ask which contiguous range contains the section.

Results are cached in a ``.pages/<stem>.json`` sidecar, verified against
source SHA-256 and resolved model/effective settings. Cached bounds and
caps are checked without parsing. ``locate`` returns ``None`` on failure
so the caller can fall back to full-document extraction.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from psweep.utils.file_io import artifact_sha256, staged_file
from psweep.exceptions import APIKeyError
from .llm_factory import resolve_llm_kwargs

logger = logging.getLogger(__name__)

PAGE_CACHE_DIRNAME = ".pages"

# Single source of truth for the ``pages.auto_locate`` defaults, shared by the
# CLI resolver and PageLocator so ``--help``/docs and runtime never drift.
DEFAULT_PAGE_TRIGGER_CHARS = 200_000
DEFAULT_MAX_SELECTED_PAGES = 30

# Generic English words to ignore when deriving keywords from a description.
_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "for",
    "to",
    "in",
    "on",
    "with",
    "their",
    "its",
    "that",
    "this",
    "not",
    "is",
    "are",
    "be",
    "by",
    "as",
    "document",
    "section",
    "sections",
    "page",
    "pages",
    "each",
    "any",
    "all",
}

_LOCATE_SYSTEM = (
    "You locate where a described section lives inside a long document. You are "
    "given numbered page excerpts. Return the smallest contiguous page range "
    "that fully contains the described section. If it is not present, return an "
    "empty ranges list."
)

_LOCATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ranges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
                "required": ["start", "end"],
            },
        },
        "reason": {"type": "string"},
    },
    "required": ["ranges"],
}


class _BaseLocator:
    """Shared infrastructure for LLM-assisted document section locators."""

    def __init__(
        self,
        section_description: str,
        *,
        model: str | None = None,
        models: dict[str, str] | None = None,
        default_model: str | None = None,
        llm_config: dict | None = None,
        trigger_chars: int = DEFAULT_PAGE_TRIGGER_CHARS,
        keywords: list[str] | None = None,
        snippet_chars: int = 600,
    ) -> None:
        self._description = section_description
        self._model = model
        self._models = models
        self._default_model = default_model
        self._llm_config = llm_config
        self.trigger_chars = int(trigger_chars)
        self._snippet_chars = max(120, int(snippet_chars))
        if keywords is not None and (
            not isinstance(keywords, list)
            or any(not isinstance(keyword, str) for keyword in keywords)
        ):
            message = "Locator keywords must be a list of strings"
            raise ValueError(message)
        self._keywords = [
            k.lower()
            for k in (
                self._derive_keywords() if keywords is None else keywords
            )
        ]
        self._client: Any = None
        self.incomplete = False

    def _derive_keywords(self) -> list[str]:
        """Pull content words from the section description as default keywords."""
        words = re.findall(r"[a-zA-Z][a-zA-Z/]{2,}", self._description.lower())
        seen: list[str] = []
        for word in words:
            if word not in _STOPWORDS and word not in seen:
                seen.append(word)
        return seen

    def _ensure_client(self) -> Any:
        if self._client is None:
            from .llm_factory import build_llm_client

            kwargs = resolve_llm_kwargs(
                self._model,
                models=self._models,
                default_model=self._default_model,
                llm_config=self._llm_config,
            )
            if not kwargs.get("api_key") or (
                kwargs["provider"] == "azure"
                and not kwargs.get("base_url")
                and not kwargs.get("azure_endpoint")
            ):
                raise APIKeyError
            self._client = build_llm_client(
                self._model,
                models=self._models,
                default_model=self._default_model,
                llm_config=self._llm_config,
            )
        return self._client

    @property
    def usage(self) -> dict:
        """Expose actual calls without creating a client on reuse."""
        return dict(getattr(self._client, "usage", {}) or {})

    def _dense_indices(self, texts) -> list[int]:
        lowered = (text.lower() for text in texts)
        scores = [
            sum(text.count(kw) for kw in self._keywords) for text in lowered
        ]
        threshold = max(1, int(0.4 * max(scores, default=0)))
        return [i for i, score in enumerate(scores) if score >= threshold]

    def _fingerprint(self) -> str:
        resolved = resolve_llm_kwargs(
            self._model,
            models=self._models,
            default_model=self._default_model,
            llm_config=self._llm_config,
        )
        settings = {
            "locator": type(self).__name__,
            "description": self._description,
            "keywords": self._keywords,
            "trigger_chars": self.trigger_chars,
            "snippet_chars": self._snippet_chars,
            "model": {
                k: resolved.get(k)
                for k in (
                    "model",
                    "provider",
                    "base_url",
                    "azure_endpoint",
                    "azure_api_version",
                )
            },
        }
        if isinstance(self, PageLocator):
            settings.update(
                max_pages=self._max_pages, context_pages=self._context
            )
        return hashlib.sha256(
            json.dumps(settings, sort_keys=True).encode()
        ).hexdigest()

    def _cache_metadata(self, file_path: Path) -> dict:
        stat = file_path.stat()
        return {
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "source_sha256": artifact_sha256(file_path),
            "fingerprint": self._fingerprint(),
        }

    def _cache_matches(self, file_path: Path, meta: dict) -> bool:
        expected = self._cache_metadata(file_path)
        return all(meta.get(key) == value for key, value in expected.items())

    @staticmethod
    def _publish_cache(entries: list[tuple[Path, bytes]]) -> None:
        """Stage/flush cache products and publish metadata last."""
        with contextlib.ExitStack() as stack:
            staged = [
                stack.enter_context(staged_file(destination, content))
                for destination, content in entries
            ]
            for temporary, (destination, _) in zip(staged, entries):
                temporary.replace(destination)


class PageLocator(_BaseLocator):
    """Find the page range holding a described section in a large PDF."""

    def __init__(
        self,
        section_description: str,
        *,
        model: str | None = None,
        models: dict[str, str] | None = None,
        default_model: str | None = None,
        llm_config: dict | None = None,
        trigger_chars: int = DEFAULT_PAGE_TRIGGER_CHARS,
        max_selected_pages: int = DEFAULT_MAX_SELECTED_PAGES,
        keywords: list[str] | None = None,
        context_pages: int = 1,
        snippet_chars: int = 600,
    ) -> None:
        super().__init__(
            section_description,
            model=model,
            models=models,
            default_model=default_model,
            llm_config=llm_config,
            trigger_chars=trigger_chars,
            keywords=keywords,
            snippet_chars=snippet_chars,
        )
        self._max_pages = max(1, int(max_selected_pages))
        self._context = max(0, int(context_pages))

    # -- keyword heuristics ------------------------------------------------

    def _candidate_pages(self, pages: list[str]) -> list[int]:
        """Return dense page indices and their configured neighbors."""
        return sorted(
            {
                neighbor
                for idx in self._dense_indices(pages)
                for neighbor in range(
                    max(0, idx - self._context),
                    min(len(pages), idx + self._context + 1),
                )
            }
        )

    # -- LLM confirmation --------------------------------------------------

    def _confirm_with_llm(
        self, pages: list[str], candidates: list[int]
    ) -> tuple[int, int] | None:
        """Ask the LLM for the contiguous 1-indexed range over candidate pages."""
        index_lines = []
        for idx in candidates:
            snippet = " ".join(pages[idx].split())[: self._snippet_chars]
            index_lines.append(f"[page {idx + 1}] {snippet}")
        index_text = "\n\n".join(index_lines)

        user_prompt = (
            f"TARGET SECTION: {self._description}\n\n"
            "Below are excerpts from candidate pages of a long document (page "
            "numbers are 1-indexed and correspond to the real document). Return "
            "the smallest contiguous page range that fully contains the target "
            'section, as JSON {"ranges": [{"start": N, "end": M}]}.\n\n'
            f"CANDIDATE PAGES:\n{index_text}"
        )
        try:
            result = self._ensure_client().extract(
                text=index_text,
                schema=_LOCATE_SCHEMA,
                system_prompt=_LOCATE_SYSTEM,
                user_prompt=user_prompt,
                suppress_errors=True,
            )
        except APIKeyError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to no targeting
            logger.debug(f"Page locator LLM call failed: {exc}")
            return None

        data = result.get("data") if isinstance(result, dict) else None
        ranges = data.get("ranges") if isinstance(data, dict) else None
        valid = []
        for rng in ranges if isinstance(ranges, list) else []:
            if not isinstance(rng, dict):
                continue
            start, end = rng.get("start"), rng.get("end")
            if type(start) is int and type(end) is int and start <= end:
                valid.append((start, end))
        if not valid:
            return None
        start = max(1, min(rng[0] for rng in valid))
        end = min(len(pages), max(rng[1] for rng in valid))
        if end < start:
            return None
        # Cap the span so a bad answer can't select the whole book.
        if end - start + 1 > self._max_pages:
            end = start + self._max_pages - 1
        return (start, end)

    # -- cache -------------------------------------------------------------

    @staticmethod
    def _cache_path(pdf_path: Path) -> Path:
        return pdf_path.parent / PAGE_CACHE_DIRNAME / f"{pdf_path.stem}.json"

    def _read_cache(self, pdf_path: Path) -> tuple[int, int] | None:
        cache = self._cache_path(pdf_path)
        if not cache.exists():
            return None
        try:
            meta = json.loads(cache.read_text(encoding="utf-8"))
            if not self._cache_matches(pdf_path, meta):
                return None
            start, end = meta["range"]
            page_count = meta.get("page_count")
            if any(type(n) is not int for n in (start, end, page_count)):
                return None
            if not 1 <= start <= end <= page_count:
                return None
            if end - start + 1 > self._max_pages:
                return None
        except Exception:  # noqa: BLE001 - a bad cache must never break the run
            return None
        else:
            return start, end

    def _write_cache(
        self,
        pdf_path: Path,
        rng: tuple[int, int],
        method: str,
        page_count: int,
    ) -> None:
        try:
            meta = {
                **self._cache_metadata(pdf_path),
                "range": list(rng),
                "page_count": page_count,
                "method": method,
            }
            self._publish_cache(
                [
                    (
                        self._cache_path(pdf_path),
                        json.dumps(meta, indent=2).encode(),
                    )
                ]
            )
        except (OSError, TypeError, ValueError):
            logger.debug(
                "Page cache write failed for %s", pdf_path, exc_info=True
            )

    def locate_cached(self, pdf_path: str | Path) -> tuple[int, int] | None:
        """Return a verified range without parsing or client setup."""
        pdf_path = Path(pdf_path)
        return (
            self._read_cache(pdf_path)
            if pdf_path.suffix.lower() == ".pdf"
            else None
        )

    # -- entry point -------------------------------------------------------

    def locate(
        self, pdf_path: str | Path, pages: list[str] | None = None
    ) -> tuple[int, int] | None:
        """Return the 1-indexed inclusive page range for the section, or None."""
        pdf_path = Path(pdf_path)

        if pdf_path.suffix.lower() != ".pdf":
            return None
        cached = self.locate_cached(pdf_path)
        if cached is not None:
            logger.debug(f"Page targeting cache hit for {pdf_path.name}")
            return cached

        if pages is None:
            from .pdf_utils import extract_pages_text

            pages = extract_pages_text(pdf_path)
            if sum(map(len, pages)) <= self.trigger_chars:
                return None
        if not pages:
            return None

        candidates = self._candidate_pages(pages)
        if not candidates:
            logger.debug(
                f"Page targeting: no candidate pages for {pdf_path.name}"
            )
            return None

        rng = self._confirm_with_llm(pages, candidates)
        self.incomplete = rng is None
        if rng is None:
            return None

        self._write_cache(
            pdf_path,
            rng,
            method="heuristic+llm",
            page_count=len(pages),
        )
        return rng
