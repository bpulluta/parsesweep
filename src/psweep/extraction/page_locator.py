"""LLM-assisted page targeting for large documents.

Large source documents (e.g. a 600+ page tariff book) overflow the extraction
context and get truncated before the section you actually want. This module
finds the pages that hold a described section, plus the parts of the document
that section refers to, so extraction can target only those pages.

Strategy (cheap first, LLM second):

1. Heuristic pre-filter — score each page by keyword hits and keep the dense
   pages plus a little context around them.
2. LLM confirm — send a compact index (page number + snippet) of just the
   candidate pages and ask which page ranges contain the section.
3. Reference following — send the newly selected pages in full plus a
   one-line outline of every page, and ask which other parts they refer to
   hold information the section needs (e.g. "charges are specified in
   Sheet 75"). Repeat for the pages that adds until nothing new is referenced
   or ``max_selected_pages`` is spent; relevant parts that do not fit are
   reported as omitted.

Results are cached in a ``.pages/<stem>.json`` sidecar, verified against
source SHA-256 and resolved model/effective settings. Cached selections are
checked against the page count and budget without parsing. ``locate`` returns
``None`` on failure so the caller can fall back to full-document extraction.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psweep.utils.file_io import artifact_sha256, staged_file
from psweep.utils.page_range import (
    PageRanges,
    count_pages,
    format_page_ranges,
    normalize_page_ranges,
    page_numbers,
    ranges_from_pages,
)
from psweep.exceptions import APIKeyError
from .llm_factory import resolve_llm_kwargs

logger = logging.getLogger(__name__)

PAGE_CACHE_DIRNAME = ".pages"

# Single source of truth for the ``pages.auto_locate`` defaults, shared by the
# CLI resolver and PageLocator so ``--help``/docs and runtime never drift.
DEFAULT_PAGE_TRIGGER_CHARS = 200_000
DEFAULT_MAX_SELECTED_PAGES = 30

# Reference following shows the model one outline line per page; entries shrink
# below the per-page width on very long documents to bound the prompt size.
_OUTLINE_PAGE_CHARS = 160
_OUTLINE_TOTAL_CHARS = 120_000

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
    "given numbered page excerpts. Return the smallest page ranges that fully "
    "contain the described section; use several ranges only when it is split "
    "across separate parts of the document. If it is not present, return an "
    "empty ranges list."
)

_REFERENCE_SYSTEM = (
    "You follow cross-references inside a long document. You are given the full "
    "text of pages selected for a described section and a one-line outline of "
    "every page. Find the other parts of the document the selected text refers "
    "to, such as a schedule, rule, table, appendix or section it says applies, "
    "and use the outline to locate them. Return the page ranges of the referenced "
    "parts that hold information needed for the described section, most "
    "essential first, excluding pages already selected. If none are needed, "
    "return an empty ranges list."
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
            k.lower() for k in (self._derive_keywords() if keywords is None else keywords)
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

    @property
    def model(self) -> str | None:
        """The model answering this locator (None before any call)."""
        return getattr(self._client, "raw_model", None)

    def _dense_indices(self, texts) -> list[int]:
        lowered = (text.lower() for text in texts)
        scores = [sum(text.count(kw) for kw in self._keywords) for text in lowered]
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
            settings.update(max_pages=self._max_pages, context_pages=self._context)
        return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()

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
            for temporary, (destination, _) in zip(staged, entries, strict=True):
                temporary.replace(destination)


@dataclass(frozen=True)
class PageSelection:
    """Pages to extract, and relevant pages left out to stay within the budget."""

    ranges: PageRanges
    omitted: PageRanges = ()


class PageLocator(_BaseLocator):
    """Find the pages holding a described section, and those it refers to, in a large PDF."""

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

    # -- LLM calls ---------------------------------------------------------

    def _ask_ranges(
        self, page_total: int, *, system: str, user_prompt: str
    ) -> list[tuple[int, int]] | None:
        """Return the valid ranges an answer names, clamped to the document.

        ``None`` means the call failed or the answer had no ranges list.
        """
        try:
            result = self._ensure_client().extract(
                text=user_prompt,
                schema=_LOCATE_SCHEMA,
                system_prompt=system,
                user_prompt=user_prompt,
                suppress_errors=True,
            )
        except APIKeyError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to no targeting
            logger.debug("Page locator LLM call failed: %s", exc)
            return None

        data = result.get("data") if isinstance(result, dict) else None
        ranges = data.get("ranges") if isinstance(data, dict) else None
        if not isinstance(ranges, list):
            return None
        valid = []
        for rng in ranges:
            if not isinstance(rng, dict):
                continue
            start, end = rng.get("start"), rng.get("end")
            if type(start) is int and type(end) is int and start <= end:
                start, end = max(1, start), min(page_total, end)
                if start <= end:
                    valid.append((start, end))
        return valid

    def _confirm_with_llm(self, pages: list[str], candidates: list[int]) -> list[int] | None:
        """Ask which candidate pages hold the section; return them in page order."""
        index_lines = []
        for idx in candidates:
            snippet = " ".join(pages[idx].split())[: self._snippet_chars]
            index_lines.append(f"[page {idx + 1}] {snippet}")
        index_text = "\n\n".join(index_lines)

        user_prompt = (
            f"TARGET SECTION: {self._description}\n\n"
            "Below are excerpts from candidate pages of a long document (page "
            "numbers are 1-indexed and correspond to the real document). Return "
            "the smallest page ranges that fully contain the target section, using "
            "several ranges only when it is split across separate parts, as JSON "
            '{"ranges": [{"start": N, "end": M}]}.\n\n'
            f"CANDIDATE PAGES:\n{index_text}"
        )
        ranges = self._ask_ranges(len(pages), system=_LOCATE_SYSTEM, user_prompt=user_prompt)
        return page_numbers(normalize_page_ranges(ranges)) if ranges else None

    @staticmethod
    def _outline(pages: list[str]) -> str:
        """One line per page, without running headers and footers."""
        lines = [
            [" ".join(line.split()) for line in page.splitlines() if line.strip()] for page in pages
        ]
        counts = Counter(line for page_lines in lines for line in set(page_lines))
        running = {line for line, count in counts.items() if count > max(1, len(pages) / 2)}
        width = max(1, min(_OUTLINE_PAGE_CHARS, _OUTLINE_TOTAL_CHARS // max(1, len(pages))))
        return "\n".join(
            f"[page {number}] "
            + " ".join(line for line in page_lines if line not in running)[:width]
            for number, page_lines in enumerate(lines, start=1)
        )

    def _follow_references(
        self, pages: list[str], chosen: list[int], requested: set[int]
    ) -> tuple[list[int], list[int]]:
        """Add the parts selected pages refer to until none are new or the budget is spent.

        Each pass reads the pages the previous pass added. Referenced parts are
        taken whole in the model's priority order; parts that do not fit the
        budget are returned as omitted. A failed pass marks the result incomplete.
        """
        outline = self._outline(pages)
        chosen_set, omitted = set(chosen), set()
        frontier = chosen
        while frontier and len(chosen_set) < self._max_pages:
            frontier_text = "\n\n".join(f"[page {n}]\n{pages[n - 1]}" for n in frontier)
            user_prompt = (
                f"TARGET SECTION: {self._description}\n\n"
                f"ALREADY SELECTED PAGES: {format_page_ranges(ranges_from_pages(chosen_set))}"
                "\n\nBelow is the full text of newly selected pages, then a one-line "
                "outline of every page (page numbers are 1-indexed and correspond to "
                "the real document). Return the page ranges of the other parts this "
                "text refers to that hold information needed for the target section, "
                "most essential first, as JSON "
                '{"ranges": [{"start": N, "end": M}]}.\n\n'
                f"SELECTED PAGE TEXT:\n{frontier_text}\n\nDOCUMENT OUTLINE:\n{outline}"
            )
            ranges = self._ask_ranges(len(pages), system=_REFERENCE_SYSTEM, user_prompt=user_prompt)
            if ranges is None:
                self.incomplete = True
                break
            added = []
            for start, end in ranges:
                part = [n for n in range(start, end + 1) if n not in requested]
                requested.update(part)
                if len(chosen_set) + len(part) <= self._max_pages:
                    chosen_set.update(part)
                    added.extend(part)
                else:
                    omitted.update(part)
            frontier = sorted(added)
        return sorted(chosen_set), sorted(omitted)

    # -- cache -------------------------------------------------------------

    @staticmethod
    def _cache_path(pdf_path: Path) -> Path:
        return pdf_path.parent / PAGE_CACHE_DIRNAME / f"{pdf_path.stem}.json"

    def _read_cache(self, pdf_path: Path) -> PageSelection | None:
        cache = self._cache_path(pdf_path)
        if not cache.exists():
            return None
        try:
            meta = json.loads(cache.read_text(encoding="utf-8"))
            if not self._cache_matches(pdf_path, meta):
                return None
            ranges = normalize_page_ranges(meta["ranges"])
            omitted = normalize_page_ranges(meta["omitted"])
            page_count = meta["page_count"]
            if type(page_count) is not int or not ranges:
                return None
            if any(end > page_count for _, end in (*ranges, *omitted)):
                return None
            if count_pages(ranges) > self._max_pages:
                return None
        except Exception:  # noqa: BLE001 - a bad cache must never break the run
            return None
        else:
            return PageSelection(ranges, omitted)

    def _write_cache(self, pdf_path: Path, selection: PageSelection, page_count: int) -> None:
        try:
            meta = {
                **self._cache_metadata(pdf_path),
                "ranges": [list(rng) for rng in selection.ranges],
                "omitted": [list(rng) for rng in selection.omitted],
                "page_count": page_count,
                "method": "heuristic+llm",
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
            logger.debug("Page cache write failed for %s", pdf_path, exc_info=True)

    def locate_cached(self, pdf_path: str | Path) -> PageSelection | None:
        """Return a verified selection without parsing or client setup."""
        pdf_path = Path(pdf_path)
        return self._read_cache(pdf_path) if pdf_path.suffix.lower() == ".pdf" else None

    # -- entry point -------------------------------------------------------

    def locate(self, pdf_path: str | Path, pages: list[str] | None = None) -> PageSelection | None:
        """Return the pages for the section and the parts it refers to, or None."""
        pdf_path = Path(pdf_path)

        if pdf_path.suffix.lower() != ".pdf":
            return None
        cached = self.locate_cached(pdf_path)
        if cached is not None:
            logger.debug("Page targeting cache hit for %s", pdf_path.name)
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
            logger.debug("Page targeting: no candidate pages for %s", pdf_path.name)
            return None

        primary = self._confirm_with_llm(pages, candidates)
        self.incomplete = primary is None
        if primary is None:
            return None

        # Keep the first pages of an oversized section so a bad answer can't
        # select the whole book.
        chosen, omitted = self._follow_references(
            pages, primary[: self._max_pages], requested=set(primary)
        )
        selection = PageSelection(
            ranges_from_pages(chosen),
            ranges_from_pages([*primary[self._max_pages :], *omitted]),
        )
        if not self.incomplete:
            self._write_cache(pdf_path, selection, page_count=len(pages))
        return selection
