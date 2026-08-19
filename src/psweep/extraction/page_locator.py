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

Results are cached in a ``.pages/<stem>.json`` sidecar next to the file (keyed
on the file's size/mtime and the section description) so the LLM call happens
once. Everything is best-effort: on any failure ``locate`` returns ``None`` and
the caller falls back to full-document extraction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PAGE_CACHE_DIRNAME = ".pages"

# Single source of truth for the ``pages.auto_locate`` defaults, shared by the
# CLI resolver and PageLocator so ``--help``/docs and runtime never drift.
DEFAULT_PAGE_TRIGGER_CHARS = 200_000
DEFAULT_MAX_SELECTED_PAGES = 30

# Generic English words to ignore when deriving keywords from a description.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with",
    "their", "its", "that", "this", "not", "is", "are", "be", "by", "as",
    "document", "section", "sections", "page", "pages", "each", "any", "all",
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
        trigger_chars: int = DEFAULT_PAGE_TRIGGER_CHARS,
        keywords: list[str] | None = None,
        snippet_chars: int = 600,
    ) -> None:
        self._description = section_description
        self._model = model
        self._models = models
        self._default_model = default_model
        self.trigger_chars = int(trigger_chars)
        self._snippet_chars = max(120, int(snippet_chars))
        self._keywords = [
            k.lower() for k in (keywords or self._derive_keywords())
        ]
        self._client: Any = None

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

            self._client = build_llm_client(
                self._model,
                models=self._models,
                default_model=self._default_model,
            )
        return self._client

    def _section_hash(self) -> str:
        return hashlib.sha256(self._description.encode("utf-8")).hexdigest()[:12]


class PageLocator(_BaseLocator):
    """Find the page range holding a described section in a large PDF."""

    def __init__(
        self,
        section_description: str,
        *,
        model: str | None = None,
        models: dict[str, str] | None = None,
        default_model: str | None = None,
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
            trigger_chars=trigger_chars,
            keywords=keywords,
            snippet_chars=snippet_chars,
        )
        self._max_pages = max(1, int(max_selected_pages))
        self._context = max(0, int(context_pages))

    # -- keyword heuristics ------------------------------------------------

    def _score_page(self, text: str) -> int:
        low = text.lower()
        return sum(low.count(kw) for kw in self._keywords)

    def _candidate_pages(self, pages: list[str]) -> list[int]:
        """Return 0-indexed candidate page numbers (dense pages + neighbors)."""
        scores = [self._score_page(p) for p in pages]
        if not any(scores):
            return []
        # Keep pages scoring at least ~40% of the max hit density.
        threshold = max(1, int(0.4 * max(scores)))
        keep: set[int] = set()
        for idx, score in enumerate(scores):
            if score >= threshold:
                for j in range(idx - self._context, idx + self._context + 1):
                    if 0 <= j < len(pages):
                        keep.add(j)
        return sorted(keep)

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
            "section, as JSON {\"ranges\": [{\"start\": N, \"end\": M}]}.\n\n"
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
        except Exception as exc:  # noqa: BLE001 - fall back to no targeting
            logger.debug(f"Page locator LLM call failed: {exc}")
            return None

        data = result.get("data") if isinstance(result, dict) else None
        ranges = (data or {}).get("ranges") or []
        starts, ends = [], []
        for r in ranges:
            try:
                starts.append(int(r["start"]))
                ends.append(int(r["end"]))
            except (KeyError, TypeError, ValueError):
                continue
        if not starts:
            return None
        start = max(1, min(starts))
        end = min(len(pages), max(ends))
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
            stat = pdf_path.stat()
            if int(meta.get("source_size", -1)) != stat.st_size:
                return None
            if int(meta.get("source_mtime_ns", -1)) != stat.st_mtime_ns:
                return None
            if meta.get("section_hash") != self._section_hash():
                return None
            rng = meta.get("range")
            if not rng:
                return None
            return int(rng[0]), int(rng[1])
        except Exception:  # noqa: BLE001 - a bad cache must never break the run
            return None

    def _write_cache(
        self, pdf_path: Path, rng: tuple[int, int], method: str
    ) -> None:
        cache = self._cache_path(pdf_path)
        try:
            stat = pdf_path.stat()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(
                json.dumps(
                    {
                        "source_size": stat.st_size,
                        "source_mtime_ns": stat.st_mtime_ns,
                        "section_hash": self._section_hash(),
                        "section_description": self._description,
                        "range": [rng[0], rng[1]],
                        "method": method,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - caching is an optimization
            pass

    # -- entry point -------------------------------------------------------

    def locate(
        self, pdf_path: str | Path, pages: list[str] | None = None
    ) -> tuple[int, int] | None:
        """Return the 1-indexed inclusive page range for the section, or None."""
        pdf_path = Path(pdf_path)

        cached = self._read_cache(pdf_path)
        if cached is not None:
            logger.debug(f"Page targeting cache hit for {pdf_path.name}")
            return cached

        if pages is None:
            from .pdf_utils import extract_pages_text

            pages = extract_pages_text(pdf_path)
        if not pages:
            return None

        candidates = self._candidate_pages(pages)
        if not candidates:
            logger.debug(
                f"Page targeting: no candidate pages for {pdf_path.name}"
            )
            return None

        rng = self._confirm_with_llm(pages, candidates)
        if rng is None:
            return None

        self._write_cache(pdf_path, rng, method="heuristic+llm")
        return rng
