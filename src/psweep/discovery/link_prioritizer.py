"""Heuristic link prioritizer for discovery candidate ranking.

Reduces web discovery noise by scoring and ranking candidate URLs before
download, enabling 1000+ link automation without manual selection.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace, field
from typing import Any
from urllib.parse import unquote, urlparse

from .models import DiscoveryCandidate, CandidateScore


# ---------------------------------------------------------------------------
# Score constants
# ---------------------------------------------------------------------------

# File type scores (applied to URL extension / anchor text clues)
_FILE_TYPE_SCORES: dict[str, float] = {
    ".pdf": 0.40,
    ".docx": 0.35,
    ".doc": 0.30,
    ".xlsx": 0.25,
    ".xls": 0.20,
    ".csv": 0.15,
    ".txt": 0.10,
    # HTML-derived: slightly negative to deprioritize in favour of direct docs
    ".html": -0.05,
    ".htm": -0.05,
    # Clearly not document material
    ".jpg": -0.20,
    ".jpeg": -0.20,
    ".png": -0.20,
    ".gif": -0.20,
    ".mp4": -0.30,
    ".zip": -0.15,
    ".exe": -0.30,
}

# Generic document-discovery fallback used only when no ranking keywords
# are supplied. Explicit domain keywords replace it.
_DOCUMENT_PATH_KEYWORDS: list[str] = [
    "document",
    "download",
    "guide",
    "report",
]

# Penalty applied when a candidate URL path looks like a shopping/cart page.
_SHOPPING_PATH_PENALTY: float = -0.10


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PriorityScore:
    """Detailed breakdown of a heuristic priority assessment."""

    file_type_score: float = 0.0
    domain_authority_score: float = 0.0
    keyword_score: float = 0.0
    confidence: float = 0.0  # 0-1, normalised aggregate

    def to_dict(self) -> dict[str, Any]:
        """Serialize the priority score components to a rounded plain dict."""
        return {
            "file_type_score": round(self.file_type_score, 4),
            "domain_authority_score": round(self.domain_authority_score, 4),
            "keyword_score": round(self.keyword_score, 4),
            "confidence": round(self.confidence, 4),
        }


@dataclass
class PrioritizedCandidate:
    """An discovery candidate augmented with heuristic priority metadata."""

    candidate: DiscoveryCandidate
    priority_score: PriorityScore
    priority_reasons: list[str] = field(default_factory=list)
    original_rank: int = 0  # position before prioritization (lineage)

    def to_lineage_dict(self) -> dict[str, Any]:
        """Serialize this candidate's priority metadata for manifest lineage."""
        return {
            "url": self.candidate.url,
            "original_rank": self.original_rank,
            "priority_score": self.priority_score.to_dict(),
            "priority_reasons": self.priority_reasons,
        }


# ---------------------------------------------------------------------------
# LinkPrioritizer
# ---------------------------------------------------------------------------


class LinkPrioritizer:
    """Heuristic ranker for discovery candidate URLs.

    Scores source URL/title/snippet evidence before download using file
    type and configured authority/keyword signals. Returns top-K ranked
    candidates and full lineage so nothing is permanently discarded.

    Parameters
    ----------
    keywords:
        Domain/query keywords used to boost URLs that contain them.
        Sourced from schema keywords or config.
    domain_authority_overrides:
        Configured ``{host_pattern: score_adjustment}`` entries from
        ``link_prioritization.domain_scores``. Host keys match the host
        and its subdomains; leading-dot keys match hostname suffixes.
        No authority adjustments apply unless configured.
    top_k:
        Maximum number of candidates to surface after ranking. ``None``
        returns all candidates in ranked order.
    shopping_path_keywords:
        Configured shopping/cart terms matched literally at token
        boundaries in decoded URL paths. Empty or omitted lists apply
        no shopping penalties.
    """

    def __init__(
        self,
        keywords: list[str] | None = None,
        domain_authority_overrides: dict[str, float] | None = None,
        top_k: int | None = 5,
        shopping_path_keywords: list[str] | None = None,
    ) -> None:
        self._keywords: list[str] = [
            k.lower().strip() for k in (keywords or []) if k.strip()
        ]
        self._authority_patterns: list[tuple[str, float]] = [
            (k.lower().strip().rstrip("."), float(v))
            for k, v in (domain_authority_overrides or {}).items()
            if k.strip().rstrip(".")
        ]
        self._top_k = top_k
        self._shopping_patterns: list[tuple[str, re.Pattern[str]]] = [
            (term, re.compile(rf"(?<![^\W_]){re.escape(term)}(?![^\W_])"))
            for term in dict.fromkeys(
                k.lower().strip()
                for k in (shopping_path_keywords or [])
                if k.strip()
            )
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def prioritize(
        self,
        candidates: list[DiscoveryCandidate],
    ) -> tuple[list[DiscoveryCandidate], list[dict[str, Any]]]:
        """Rank candidates by heuristic score and return top-K.

        Parameters
        ----------
        candidates:
            Input candidates (from seeker or digger stage).

        Returns
        -------
        ranked_top_k:
            Top-K candidates sorted by heuristic confidence descending,
            with updated ``score.url_signal`` and ``score.anchor_signal``
            reflecting the priority assessment.
        lineage:
            Full list of all candidates (including those outside top-K) with
            their priority scores and original ranks, preserving provenance.
        """
        if not candidates:
            return [], []

        prioritized: list[PrioritizedCandidate] = []
        for original_rank, candidate in enumerate(candidates):
            ps, reasons = self._score(candidate)
            prioritized.append(
                PrioritizedCandidate(
                    candidate=candidate,
                    priority_score=ps,
                    priority_reasons=reasons,
                    original_rank=original_rank,
                )
            )

        # Stable sort: highest confidence first; ties preserve original order
        prioritized.sort(
            key=lambda p: p.priority_score.confidence, reverse=True
        )

        # Build lineage before slicing
        lineage = [p.to_lineage_dict() for p in prioritized]

        # Slice top-K
        top_slice = (
            prioritized if self._top_k is None else prioritized[: self._top_k]
        )

        # Propagate heuristic signals back into candidate's CandidateScore.
        # Ranking order is driven by priority_score.confidence (above), so
        # this only affects the downstream acceptance/observability score.
        ranked_top_k: list[DiscoveryCandidate] = []
        for pc in top_slice:
            c = pc.candidate
            # Take the stronger of the candidate's baseline url_signal (e.g. a
            # seeker result's inherent confidence) or the heuristic file-type +
            # keyword signal, so prioritization refines rather than discards the
            # base. Ranking is unaffected (it uses priority_score).
            blended_url_signal = max(
                0.0,
                min(
                    1.0,
                    max(
                        c.score.url_signal,
                        pc.priority_score.file_type_score
                        + pc.priority_score.keyword_score * 0.5,
                    ),
                ),
            )
            blended_anchor_signal = max(
                0.0, min(1.0, abs(pc.priority_score.domain_authority_score))
            )
            updated_score = CandidateScore(
                url_signal=blended_url_signal,
                anchor_signal=blended_anchor_signal,
                content_signal=c.score.content_signal,
                trust_signal=max(
                    0.0,
                    min(
                        1.0,
                        c.score.trust_signal
                        + pc.priority_score.domain_authority_score * 0.5,
                    ),
                ),
                weights=c.score.weights,
            )
            updated_reasons = list(c.reasons) + pc.priority_reasons
            ranked_top_k.append(
                replace(c, score=updated_score, reasons=updated_reasons)
            )

        return ranked_top_k, lineage

    # ------------------------------------------------------------------
    # Scoring internals
    # ------------------------------------------------------------------

    def _score(
        self, candidate: DiscoveryCandidate
    ) -> tuple[PriorityScore, list[str]]:
        url = candidate.url or ""
        # Reasons hold query/heuristic notes, not source evidence.
        # Ranking on them would reward every result for its query.
        source_text = " ".join(
            text for text in (candidate.title, candidate.snippet) if text
        ).lower()
        reasons: list[str] = []

        file_type_score = self._score_file_type(url, source_text, reasons)
        domain_authority_score = self._score_domain_authority(url, reasons)
        keyword_score = self._score_keywords(url, source_text, reasons)

        # Weighted aggregate before normalisation. Weights keep their relative
        # importance (file type dominates, then authority and keywords equally).
        raw = (
            file_type_score * 0.35
            + domain_authority_score * 0.25
            + keyword_score * 0.25
        )
        confidence = max(0.0, min(1.0, 0.5 + raw))

        ps = PriorityScore(
            file_type_score=file_type_score,
            domain_authority_score=domain_authority_score,
            keyword_score=keyword_score,
            confidence=confidence,
        )
        return ps, reasons

    def _score_file_type(
        self, url: str, anchor: str, reasons: list[str]
    ) -> float:
        """Return file type signal in [-1, 1] range."""
        lower_url = url.lower()

        # Check URL path for extension
        ext = self._extract_extension(lower_url)
        if ext and ext in _FILE_TYPE_SCORES:
            score = _FILE_TYPE_SCORES[ext]
            if score > 0:
                reasons.append(f"file_type:url_ext={ext} (+{score:.2f})")
            elif score < 0:
                reasons.append(f"file_type:url_ext={ext} ({score:.2f})")
            return score

        # Check anchor text for file-type clues
        for ext_hint, score in _FILE_TYPE_SCORES.items():
            if ext_hint.lstrip(".") in anchor:
                if score > 0:
                    reasons.append(
                        f"file_type:anchor_hint={ext_hint} (+{score:.2f})"
                    )
                return score

        # Match path tokens, not host/query text.
        if not self._shopping_patterns:
            return 0.0
        try:
            path = unquote(urlparse(url).path).lower()
        except ValueError:
            return 0.0
        for term, pattern in self._shopping_patterns:
            if pattern.search(path):
                reasons.append(
                    f"file_type:shopping_path_keyword={term} "
                    f"({_SHOPPING_PATH_PENALTY:.2f})"
                )
                return _SHOPPING_PATH_PENALTY

        return 0.0

    def _score_domain_authority(self, url: str, reasons: list[str]) -> float:
        """Return domain authority adjustment, clamped to [-0.5, 0.5]."""
        try:
            host = (urlparse(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return 0.0

        total = 0.0
        matched: list[str] = []

        for pattern, adj in self._authority_patterns:
            matches = (
                host.endswith(pattern)
                if pattern.startswith(".")
                else host == pattern or host.endswith("." + pattern)
            )
            if matches:
                total += adj
                matched.append(f"{pattern}={adj:+.2f}")

        clamped = max(-0.5, min(0.5, total))
        if matched:
            reasons.append(f"domain_authority:{','.join(matched)}")
        return clamped

    def _score_keywords(
        self, url: str, anchor: str, reasons: list[str]
    ) -> float:
        """Score source evidence with keywords or a generic fallback."""
        active_keywords = self._keywords or _DOCUMENT_PATH_KEYWORDS
        combined = unquote(url).lower() + " " + anchor

        hit_count = sum(1 for kw in active_keywords if kw in combined)
        if hit_count == 0:
            return 0.0

        # Log-scale: 1 hit → ~0.3; 3 hits → ~0.6; 6+ hits → ~0.9
        score = min(1.0, 0.3 * math.log1p(hit_count) / math.log1p(1))
        reasons.append(f"keywords:{hit_count}_match(s) (+{score:.2f})")
        return round(score, 4)

    @staticmethod
    def _extract_extension(url_lower: str) -> str | None:
        """Extract lowercase file extension from a URL path."""
        try:
            path = urlparse(url_lower).path
            if "." in path:
                part = path.rsplit(".", 1)[-1]
                ext = "." + part.split("?")[0].split("#")[0]
                if 2 <= len(ext) <= 6:
                    return ext
        except Exception:
            pass
        return None
