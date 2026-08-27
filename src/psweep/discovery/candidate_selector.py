"""Per-target candidate selection: draft filtering and recency-based version selection.

After the SerpApi seeker discovers candidates per target, this module decides
which candidates to actually stage for download.  The two core guards are:

1. **Draft filter** – exclude documents whose URL/title/snippet text contains
   signals indicating they are not the current enacted/production version
   (e.g., "draft", "proposed", "preliminary", "superseded").

2. **Recency selection** – within each target's candidate pool, prefer the
   most recent version by parsing effective dates out of the URL filename
   or path segments.  When no date is detectable the candidate is still
   included (date-less candidates rank behind dated ones).

Both guards are applied per-target so that a utility or jurisdiction with
many candidates does not crowd out others, and so that version conflicts
(e.g., April-2024 vs May-2024 tariff summation sheets) resolve to the
single latest copy.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .constants import DOCUMENT_EXTENSIONS
from .models import DiscoveryCandidate
from .urls import normalize_url_text, url_extension


# ---------------------------------------------------------------------------
# Default draft / stale-document signal patterns (applied to URL + title/snippet)
# ---------------------------------------------------------------------------

DEFAULT_DRAFT_PATTERNS: list[str] = [
    r"\bdraft\b",
    r"\bproposed\b",
    r"\bpreliminary\b",
    r"for[\s_\-]?review",
    r"\bmodel[\s_\-]ordinance\b",
    r"\bmodel[\s_\-]code\b",
    r"\btemplate\b",
    r"\bsuperseded\b",
    r"\bwithdrawn\b",
    r"\bobsolete\b",
    r"\barchive[d]?\b",
]

# Canonical document extensions live in constants.DOCUMENT_EXTENSIONS.


# ---------------------------------------------------------------------------
# CandidateSelector
# ---------------------------------------------------------------------------


class CandidateSelector:
    """Selects the best candidate(s) per target from seeker output.

    Parameters
    ----------
    exclude_draft:
        When ``True`` (default), candidates matching any draft pattern in
        their URL/title/snippet text are excluded before recency ranking.
    draft_patterns:
        Override the default draft detection regex patterns.  Each entry is
        a case-insensitive ``re.search`` pattern string.
    """

    def __init__(
        self,
        exclude_draft: bool = True,
        draft_patterns: list[str] | None = None,
        relevance_require_any_terms: list[str] | None = None,
        relevance_require_legal_marker_terms: list[str] | None = None,
        relevance_exclude_any_terms: list[str] | None = None,
        exclude_url_patterns: list[str] | None = None,
        exclude_text_patterns: list[str] | None = None,
        require_supported_document: bool = True,
        max_per_host_per_target: int = 0,
        target_identity_require_any_templates: list[str] | None = None,
        target_identity_require_all_templates: list[str] | None = None,
        target_identity_exclude_any_templates: list[str] | None = None,
    ) -> None:
        self._exclude_draft = exclude_draft
        raw_patterns = (
            draft_patterns
            if draft_patterns is not None
            else DEFAULT_DRAFT_PATTERNS
        )
        self._draft_re: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in raw_patterns
        ]
        self._relevance_require_any_terms = [
            t.strip().lower()
            for t in (relevance_require_any_terms or [])
            if t and t.strip()
        ]
        self._relevance_require_legal_marker_terms = [
            t.strip().lower()
            for t in (relevance_require_legal_marker_terms or [])
            if t and t.strip()
        ]
        self._relevance_exclude_any_terms = [
            t.strip().lower()
            for t in (relevance_exclude_any_terms or [])
            if t and t.strip()
        ]
        self._exclude_url_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (exclude_url_patterns or [])
            if p and p.strip()
        ]
        self._exclude_text_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in (exclude_text_patterns or [])
            if p and p.strip()
        ]
        self._require_supported_document = require_supported_document
        self._max_per_host_per_target = max(0, int(max_per_host_per_target or 0))
        self._target_identity_require_any_templates = [
            t.strip()
            for t in (target_identity_require_any_templates or [])
            if t and t.strip()
        ]
        self._target_identity_require_all_templates = [
            t.strip()
            for t in (target_identity_require_all_templates or [])
            if t and t.strip()
        ]
        self._target_identity_exclude_any_templates = [
            t.strip()
            for t in (target_identity_exclude_any_templates or [])
            if t and t.strip()
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select(
        self,
        candidates_by_target: list[list[DiscoveryCandidate]],
        primary_per_target: int = 1,
        target_contexts: list[dict[str, object]] | None = None,
        include_metrics: bool = False,
    ) -> (
        tuple[list[DiscoveryCandidate], list[str]]
        | tuple[list[DiscoveryCandidate], list[str], list[dict[str, Any]]]
    ):
        """Select up to *primary_per_target* candidates per target.

        Steps per target:
        1. Draft filter (if ``exclude_draft=True``).
        2. Sort by parsed effective date descending (most recent first).
        3. Take the top *primary_per_target* candidates.

        Parameters
        ----------
        candidates_by_target:
            Parallel list where index *i* holds the candidates returned by
            the seeker for target *i*.
        primary_per_target:
            Maximum number of candidates to select for each target.
            Defaults to ``1``.
        target_contexts:
            Optional per-target metadata (parallel to *candidates_by_target*)
            used to enrich selection notes and metrics.
        include_metrics:
            When ``True``, also return the per-target selection metrics.

        Returns
        -------
        tuple
            ``(selected, notes)``, or ``(selected, notes, target_metrics)``
            when ``include_metrics=True``, where:

            - ``selected`` is the flat list of selected candidates, one group
              per target in input order;
            - ``notes`` holds human-readable selection log entries for the
              manifest;
            - ``target_metrics`` holds per-target selection metrics (present
              only when ``include_metrics=True``).
        """
        selected: list[DiscoveryCandidate] = []
        notes: list[str] = []
        target_metrics: list[dict[str, Any]] = []

        for target_idx, target_candidates in enumerate(candidates_by_target):
            original_count = len(target_candidates)
            if not target_candidates:
                notes.append(
                    f"Target {target_idx}: seeker returned 0 candidates; nothing to select."
                )
                target_metrics.append(
                    {
                        "target_index": target_idx,
                        "input_candidates": 0,
                        "selected_candidates": 0,
                        "outcome": "no_candidates",
                    }
                )
                continue

            target_context = {}
            if target_contexts and target_idx < len(target_contexts):
                raw_context = target_contexts[target_idx]
                if isinstance(raw_context, dict):
                    target_context = raw_context

            # 1. Draft / exclusion filters
            if self._exclude_draft:
                filtered = [
                    c for c in target_candidates if not self._is_draft(c)
                ]
                draft_count = original_count - len(filtered)
                if draft_count > 0:
                    notes.append(
                        f"Target {target_idx}: draft filter excluded {draft_count} of "
                        f"{original_count} candidate(s)."
                    )
                target_candidates = filtered

            if self._exclude_url_patterns or self._exclude_text_patterns:
                filtered = [
                    c
                    for c in target_candidates
                    if not self._matches_exclusion_patterns(c)
                ]
                excluded_count = len(target_candidates) - len(filtered)
                if excluded_count > 0:
                    notes.append(
                        f"Target {target_idx}: configured exclusion patterns excluded {excluded_count} of "
                        f"{len(target_candidates)} candidate(s)."
                    )
                target_candidates = filtered

            if not target_candidates:
                notes.append(
                    f"Target {target_idx}: all {original_count} candidate(s) excluded as draft/stale; "
                    "nothing selected."
                )
                target_metrics.append(
                    {
                        "target_index": target_idx,
                        "input_candidates": original_count,
                        "selected_candidates": 0,
                        "outcome": "excluded_draft_or_stale",
                    }
                )
                continue

            # 2. Target identity filter (templated rules from target context)
            if (
                self._target_identity_require_any_templates
                or self._target_identity_require_all_templates
                or self._target_identity_exclude_any_templates
            ):
                identity_filtered = [
                    c
                    for c in target_candidates
                    if self._matches_target_identity(c, target_context)
                ]
                identity_excluded_count = len(target_candidates) - len(
                    identity_filtered
                )
                if identity_excluded_count > 0:
                    notes.append(
                        f"Target {target_idx}: identity filter excluded {identity_excluded_count} of "
                        f"{len(target_candidates)} candidate(s)."
                    )
                target_candidates = identity_filtered

            if not target_candidates:
                notes.append(
                    f"Target {target_idx}: all candidate(s) excluded by identity rules; "
                    "nothing selected."
                )
                target_metrics.append(
                    {
                        "target_index": target_idx,
                        "input_candidates": original_count,
                        "selected_candidates": 0,
                        "outcome": "excluded_identity_rules",
                    }
                )
                continue

            # 3. Relevance filter (strict include/exclude term gates)
            if (
                self._relevance_require_any_terms
                or self._relevance_require_legal_marker_terms
                or self._relevance_exclude_any_terms
            ):
                relevance_filtered = [
                    c
                    for c in target_candidates
                    if self._is_relevant_candidate(c)
                ]
                relevance_excluded_count = len(target_candidates) - len(
                    relevance_filtered
                )
                if relevance_excluded_count > 0:
                    notes.append(
                        f"Target {target_idx}: relevance filter excluded {relevance_excluded_count} of "
                        f"{len(target_candidates)} candidate(s)."
                    )
                target_candidates = relevance_filtered

            if not target_candidates:
                notes.append(
                    f"Target {target_idx}: all candidate(s) excluded by relevance rules; "
                    "nothing selected."
                )
                target_metrics.append(
                    {
                        "target_index": target_idx,
                        "input_candidates": original_count,
                        "selected_candidates": 0,
                        "outcome": "excluded_relevance_rules",
                    }
                )
                continue

            # 4. Supported-document filter
            if self._require_supported_document:
                supported_only = [
                    c
                    for c in target_candidates
                    if self._is_supported_document_url(c)
                ]
                unsupported_excluded_count = len(target_candidates) - len(
                    supported_only
                )
                if unsupported_excluded_count > 0:
                    notes.append(
                        f"Target {target_idx}: supported-document filter excluded {unsupported_excluded_count} of "
                        f"{len(target_candidates)} candidate(s)."
                    )
                target_candidates = supported_only

            if not target_candidates:
                notes.append(
                    f"Target {target_idx}: all candidate(s) excluded as unsupported document type; "
                    "nothing selected."
                )
                target_metrics.append(
                    {
                        "target_index": target_idx,
                        "input_candidates": original_count,
                        "selected_candidates": 0,
                        "outcome": "excluded_unsupported_document",
                    }
                )
                continue

            # 5. Recency sort (latest first; undated candidates trail dated ones)
            dated: list[tuple[DiscoveryCandidate, date | None]] = [
                (c, self._parse_date(c)) for c in target_candidates
            ]
            dated.sort(
                key=lambda item: (
                    not self._is_supported_document_url(
                        item[0]
                    ),  # supported docs first
                    item[1]
                    is None,  # False (dated) sorts before True (undated)
                    -(
                        item[1].toordinal() if item[1] else 0
                    ),  # more recent first
                )
            )

            # 6. Take top primary_per_target with optional per-host diversity cap
            top_items = self._take_top_with_host_cap(
                dated, primary_per_target
            )
            top_candidates = [c for c, _ in top_items]
            if target_context:
                for c in top_candidates:
                    c.target_metadata = dict(target_context)
            selected.extend(top_candidates)

            # Build a concise log line
            date_info = [str(d) for _, d in top_items if d is not None]
            if date_info:
                notes.append(
                    f"Target {target_idx}: selected {len(top_candidates)} of "
                    f"{len(target_candidates)} candidate(s); "
                    f"effective date(s): {', '.join(date_info)}."
                )
            else:
                notes.append(
                    f"Target {target_idx}: selected {len(top_candidates)} of "
                    f"{len(target_candidates)} candidate(s) (no effective date detected in URL)."
                )
            if self._max_per_host_per_target > 0:
                notes.append(
                    f"Target {target_idx}: per-host cap={self._max_per_host_per_target} "
                    f"applied to selected candidates."
                )

            target_metrics.append(
                {
                    "target_index": target_idx,
                    "input_candidates": original_count,
                    "selected_candidates": len(top_candidates),
                    "outcome": "selected",
                }
            )

        if include_metrics:
            return selected, notes, target_metrics
        return selected, notes

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_draft(self, candidate: DiscoveryCandidate) -> bool:
        """Return ``True`` if candidate content matches a draft pattern.

        Normalises path separators and underscores to spaces before matching so
        that a word-boundary ``draft`` pattern catches ``some_draft_doc.pdf`` as
        well as ``some draft doc.pdf``.
        """
        combined = re.sub(
            r"[_\-/]", " ", self._candidate_artifact_text(candidate)
        )
        return any(rx.search(combined) for rx in self._draft_re)

    def _take_top_with_host_cap(
        self,
        dated_candidates: list[tuple[DiscoveryCandidate, date | None]],
        primary_per_target: int,
    ) -> list[tuple[DiscoveryCandidate, date | None]]:
        """Select top-ranked candidates, optionally capping picks per host."""
        if self._max_per_host_per_target <= 0:
            return dated_candidates[:primary_per_target]

        selected: list[tuple[DiscoveryCandidate, date | None]] = []
        per_host_counts: dict[str, int] = {}
        for candidate, parsed_date in dated_candidates:
            host = urlparse(candidate.url or "").netloc.lower()
            host_key = host or "__unknown_host__"
            used = per_host_counts.get(host_key, 0)
            if used >= self._max_per_host_per_target:
                continue
            selected.append((candidate, parsed_date))
            per_host_counts[host_key] = used + 1
            if len(selected) >= primary_per_target:
                break
        return selected

    def _matches_exclusion_patterns(self, candidate: DiscoveryCandidate) -> bool:
        """Return True when configured URL/text exclusion patterns match."""
        if self._exclude_url_patterns:
            url_text = self._candidate_url_text(candidate)
            if any(rx.search(url_text) for rx in self._exclude_url_patterns):
                return True

        if self._exclude_text_patterns:
            text = self._candidate_artifact_text(candidate)
            if any(rx.search(text) for rx in self._exclude_text_patterns):
                return True
        return False

    def _is_relevant_candidate(self, candidate: DiscoveryCandidate) -> bool:
        """Return True when candidate text passes required/excluded relevance terms."""
        text = self._candidate_artifact_text(candidate)
        url_text = self._candidate_url_text(candidate)

        if self._relevance_exclude_any_terms:
            for term in self._relevance_exclude_any_terms:
                if term in text or term in url_text:
                    return False

        if self._relevance_require_any_terms:
            if not any(
                term in text for term in self._relevance_require_any_terms
            ):
                return False

        if self._relevance_require_legal_marker_terms:
            if not any(
                term in url_text
                for term in self._relevance_require_legal_marker_terms
            ):
                return False

        return True

    def _matches_target_identity(
        self,
        candidate: DiscoveryCandidate,
        target_context: dict[str, object],
    ) -> bool:
        """Return True when candidate matches templated target identity rules."""
        if not (
            self._target_identity_require_any_templates
            or self._target_identity_require_all_templates
            or self._target_identity_exclude_any_templates
        ):
            return True

        normalized_context = self._normalize_template_context(target_context)
        text = self._candidate_artifact_text(candidate)
        text_compact = re.sub(r"[^a-z0-9]+", "", text)

        require_any_terms = self._resolve_templates(
            self._target_identity_require_any_templates,
            normalized_context,
        )
        require_all_terms = self._resolve_templates(
            self._target_identity_require_all_templates,
            normalized_context,
        )
        exclude_any_terms = self._resolve_templates(
            self._target_identity_exclude_any_templates,
            normalized_context,
        )

        if require_any_terms and not any(
            self._text_matches_term(text, text_compact, term)
            for term in require_any_terms
        ):
            return False
        if require_all_terms and not all(
            self._text_matches_term(text, text_compact, term)
            for term in require_all_terms
        ):
            return False
        if exclude_any_terms and any(
            self._text_matches_term(text, text_compact, term)
            for term in exclude_any_terms
        ):
            return False
        return True

    @staticmethod
    def _text_matches_term(text: str, text_compact: str, term: str) -> bool:
        """Match terms against candidate text with punctuation-insensitive fallback."""
        if term in text:
            return True

        compact_term = re.sub(r"[^a-z0-9]+", "", term)
        if compact_term and compact_term in text_compact:
            return True
        return False

    @staticmethod
    def _normalize_template_context(
        target_context: dict[str, object],
    ) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in (target_context or {}).items():
            if value is None:
                continue
            if isinstance(value, str):
                cleaned = value.strip().lower()
                if cleaned:
                    normalized[str(key)] = cleaned
                continue
            if isinstance(value, (int, float, bool)):
                normalized[str(key)] = str(value).strip().lower()
        return normalized

    @staticmethod
    def _resolve_templates(
        templates: list[str], target_context: dict[str, str]
    ) -> list[str]:
        """Render simple {field} templates into lowercase search terms."""
        if not templates:
            return []

        terms: list[str] = []
        for raw_template in templates:
            template = (raw_template or "").strip().lower()
            if not template:
                continue

            rendered = template
            for placeholder in re.findall(r"\{([a-zA-Z0-9_]+)\}", template):
                replacement = target_context.get(placeholder, "")
                rendered = rendered.replace(
                    "{" + placeholder + "}", replacement
                )

            cleaned = re.sub(r"\s+", " ", rendered).strip()
            if cleaned and "{" not in cleaned and "}" not in cleaned:
                terms.append(cleaned)

        return terms

    @staticmethod
    def _candidate_artifact_text(candidate: DiscoveryCandidate) -> str:
        """Candidate text for filtering/matching: URL + title + snippet only.

        Excludes ``reasons`` because seeker reasons include rendered query text,
        which can otherwise make relevance/exclusion term gates pass trivially.
        """
        parts = [candidate.url or ""]
        if candidate.title:
            parts.append(candidate.title)
        if candidate.snippet:
            parts.append(candidate.snippet)
        return normalize_url_text(" ".join(parts))

    @staticmethod
    def _candidate_url_text(candidate: DiscoveryCandidate) -> str:
        return normalize_url_text(candidate.url or "")

    @staticmethod
    def _parse_date(candidate: DiscoveryCandidate) -> date | None:
        """Extract the most specific effective date from the candidate URL filename.

        Tries the following sub-patterns in order of specificity; returns the
        first successful parse or ``None``.

        Patterns tried (all from the URL *filename* component):
        - ``MM.DD.YYYY`` / ``MM-DD-YYYY`` / ``MM_DD_YYYY`` (US date)
        - ``YYYY-MM-DD`` / ``YYYY_MM_DD`` (ISO date)
        - ``YYYY`` (year-only, treated as Jan 1 of that year)
        """
        raw_url = candidate.url or ""
        try:
            parsed = urlparse(raw_url)
            filename = Path(parsed.path).name
        except Exception:
            filename = raw_url

        # US date: MM.DD.YYYY or MM-DD-YYYY or MM_DD_YYYY or MM/DD/YYYY
        m = re.search(r"(\d{1,2})[.\-_/](\d{1,2})[.\-_/](20\d{2})", filename)
        if m:
            try:
                return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                pass

        # ISO date: YYYY-MM-DD or YYYY_MM_DD
        m = re.search(r"(20\d{2})[_\-](\d{2})[_\-](\d{2})", filename)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass

        # Named month: Apr_2024, April-2024, apr2024, etc.
        _MONTHS = {
            "jan": 1,
            "feb": 2,
            "mar": 3,
            "apr": 4,
            "may": 5,
            "jun": 6,
            "jul": 7,
            "aug": 8,
            "sep": 9,
            "oct": 10,
            "nov": 11,
            "dec": 12,
        }
        m = re.search(
            r"(?:^|[^a-zA-Z])(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
            r"[a-z]*[\s_\-]?(20\d{2})(?!\d)",
            filename,
            re.IGNORECASE,
        )
        if m:
            month = _MONTHS.get(m.group(1).lower()[:3])
            if month:
                try:
                    return date(int(m.group(2)), month, 1)
                except ValueError:
                    pass

        # Year-only fallback: e.g. "ordinance_2023.pdf", look for 20xx in name
        m = re.search(r"(?<!\d)(20\d{2})(?!\d)", filename)
        if m:
            try:
                return date(int(m.group(1)), 1, 1)
            except ValueError:
                pass

        return None

    @staticmethod
    def _is_supported_document_url(candidate: DiscoveryCandidate) -> bool:
        """Return True when URL path ends with a supported document extension."""
        return url_extension(candidate.url or "") in DOCUMENT_EXTENSIONS
