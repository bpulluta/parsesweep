"""Heuristic link prioritizer for discovery candidate ranking.

Reduces web discovery noise by scoring and ranking candidate URLs before
download, enabling 1000+ link automation without manual selection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

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

# Domain pattern → authority adjustment (additive, clamped later).
# Patterns are lowercased substring matches against the netloc.
#
# These are DOMAIN-NEUTRAL defaults only: official-source boosts (.gov/.edu)
# and universally low-value sources (forums / marketplaces) for document
# discovery. Domain-specific authority (e.g. manufacturer or vendor sites)
# belongs in that domain's config via `link_prioritization.domain_scores`,
# which is applied identically alongside these defaults.
_DOMAIN_AUTHORITY_PATTERNS: list[tuple[str, float]] = [
    # Government and official sources → positive
    (".gov", 0.15),
    (".edu", 0.10),
    # Forums and marketplaces → negative
    ("ebay.com", -0.20),
    ("amazon.com", -0.15),
    ("etsy.com", -0.20),
    ("aliexpress.com", -0.20),
    ("reddit.com", -0.15),
    ("quora.com", -0.15),
    ("answers.com", -0.15),
    ("pinterest.com", -0.20),
    ("slideshare.net", -0.05),
]

# Power-class URL patterns: match numeric kW values in URL path
# e.g. "200kw", "200-kw", "250kva" etc.
_POWER_CLASS_URL_RE = re.compile(
    r"\b(\d{2,4})\s*[-_]?\s*k[wv][aA]?\b", re.IGNORECASE
)

# Generic, domain-neutral "this path looks like a document" keywords used only
# when a domain supplies no `link_prioritization.keywords` of its own. Domain
# vocabulary (e.g. manual/spec/datasheet, or ordinance/permit/tariff) belongs
# in that domain's config, where it is merged with these defaults.
_DOCUMENT_PATH_KEYWORDS: list[str] = [
    "document",
    "download",
    "guide",
    "report",
]

# Shopping-page path keywords → negative signal
_SHOPPING_PATH_KEYWORDS: list[str] = [
    "cart",
    "checkout",
    "buy-now",
    "add-to-cart",
    "shop",
    "store",
    "product-listing",
    "category",
    "search?",
    "q=",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PriorityScore:
    """Detailed breakdown of a heuristic priority assessment."""

    file_type_score: float = 0.0
    domain_authority_score: float = 0.0
    keyword_score: float = 0.0
    power_class_score: float = 0.0
    confidence: float = 0.0  # 0-1, normalised aggregate

    def to_dict(self) -> dict[str, Any]:
        """Serialize the priority score components to a rounded plain dict."""
        return {
            "file_type_score": round(self.file_type_score, 4),
            "domain_authority_score": round(self.domain_authority_score, 4),
            "keyword_score": round(self.keyword_score, 4),
            "power_class_score": round(self.power_class_score, 4),
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

    Scores candidates before download using file-type, domain authority,
    keyword, and power-class signals.  Returns top-K ranked candidates and
    a full lineage list so nothing is permanently discarded.

    Parameters
    ----------
    keywords:
        Domain/query keywords used to boost URLs that contain them.
        Sourced from schema keywords or config.
    domain_authority_overrides:
        Extra ``{domain_substring: score_adjustment}`` entries that
        supplement the built-in authority table.
    top_k:
        Maximum number of candidates to surface after ranking. ``None``
        returns all candidates in ranked order.
    power_range_kw:
        Optional ``(min_kw, max_kw)`` tuple.  URLs containing a numeric kW
        value inside this range receive a bonus; values outside the range
        receive a penalty.
    """

    def __init__(
        self,
        keywords: list[str] | None = None,
        domain_authority_overrides: dict[str, float] | None = None,
        top_k: int | None = 5,
        power_range_kw: tuple[float, float] | None = None,
    ) -> None:
        self._keywords: list[str] = [
            k.lower().strip() for k in (keywords or []) if k.strip()
        ]
        self._authority_extra: list[tuple[str, float]] = list(
            (k.lower(), float(v))
            for k, v in (domain_authority_overrides or {}).items()
        )
        self._top_k = top_k
        self._power_range_kw = power_range_kw

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
                DiscoveryCandidate(
                    url=c.url,
                    source=c.source,
                    score=updated_score,
                    reasons=updated_reasons,
                    status=c.status,
                    mime_type=c.mime_type,
                    extension=c.extension,
                    canonical_url=c.canonical_url,
                    content_hash=c.content_hash,
                    target_metadata=c.target_metadata,
                )
            )

        return ranked_top_k, lineage

    # ------------------------------------------------------------------
    # Scoring internals
    # ------------------------------------------------------------------

    def _score(
        self, candidate: DiscoveryCandidate
    ) -> tuple[PriorityScore, list[str]]:
        url = candidate.url or ""
        anchor = " ".join(candidate.reasons or []).lower()
        reasons: list[str] = []

        file_type_score = self._score_file_type(url, anchor, reasons)
        domain_authority_score = self._score_domain_authority(url, reasons)
        keyword_score = self._score_keywords(url, anchor, reasons)
        power_class_score = self._score_power_class(url, anchor, reasons)

        # Weighted aggregate before normalisation
        raw = (
            file_type_score * 0.35
            + domain_authority_score * 0.25
            + keyword_score * 0.25
            + power_class_score * 0.15
        )
        confidence = max(0.0, min(1.0, 0.5 + raw))

        ps = PriorityScore(
            file_type_score=file_type_score,
            domain_authority_score=domain_authority_score,
            keyword_score=keyword_score,
            power_class_score=power_class_score,
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

        # Check for shopping path → negative signal
        for kw in _SHOPPING_PATH_KEYWORDS:
            if kw in lower_url:
                reasons.append(f"file_type:shopping_path_keyword={kw} (-0.10)")
                return -0.10

        return 0.0

    def _score_domain_authority(self, url: str, reasons: list[str]) -> float:
        """Return domain authority adjustment, clamped to [-0.5, 0.5]."""
        try:
            netloc = urlparse(url).netloc.lower()
        except Exception:
            return 0.0

        total = 0.0
        matched: list[str] = []

        # Built-in patterns
        for pattern, adj in _DOMAIN_AUTHORITY_PATTERNS:
            if pattern in netloc:
                total += adj
                matched.append(f"{pattern}={adj:+.2f}")

        # User-supplied overrides
        for pattern, adj in self._authority_extra:
            if pattern in netloc:
                total += adj
                matched.append(f"{pattern}={adj:+.2f}(override)")

        clamped = max(-0.5, min(0.5, total))
        if matched:
            reasons.append(f"domain_authority:{','.join(matched)}")
        return clamped

    def _score_keywords(
        self, url: str, anchor: str, reasons: list[str]
    ) -> float:
        """Return keyword signal in [0, 1] based on combined URL + anchor matches."""
        if not self._keywords:
            # Fall back to built-in document path keywords
            active_keywords = _DOCUMENT_PATH_KEYWORDS
        else:
            active_keywords = self._keywords + _DOCUMENT_PATH_KEYWORDS

        lower_url = url.lower()
        combined = lower_url + " " + anchor

        hit_count = sum(1 for kw in active_keywords if kw in combined)
        if hit_count == 0:
            return 0.0

        # Log-scale: 1 hit → ~0.3; 3 hits → ~0.6; 6+ hits → ~0.9
        import math

        score = min(1.0, 0.3 * math.log1p(hit_count) / math.log1p(1))
        reasons.append(f"keywords:{hit_count}_match(s) (+{score:.2f})")
        return round(score, 4)

    def _score_power_class(
        self, url: str, anchor: str, reasons: list[str]
    ) -> float:
        """Return power-class URL bonus/penalty.

        If ``power_range_kw`` is set, URLs matching a value inside the range
        receive +0.15; outside the range receive -0.10.  If no range is set,
        any numeric kW value earns a modest +0.10.
        """
        lower_url = url.lower()
        combined = lower_url + " " + anchor
        matches = _POWER_CLASS_URL_RE.findall(combined)
        if not matches:
            return 0.0

        values = [float(m) for m in matches]

        if self._power_range_kw is None:
            # Generic: any power-class mention is a good signal
            reasons.append(f"power_class:kw_in_url={values} (+0.10)")
            return 0.10

        lo, hi = self._power_range_kw
        in_range = any(lo <= v <= hi for v in values)
        if in_range:
            reasons.append(
                f"power_class:kw_in_range[{lo},{hi}]={values} (+0.15)"
            )
            return 0.15
        else:
            reasons.append(
                f"power_class:kw_out_of_range[{lo},{hi}]={values} (-0.10)"
            )
            return -0.10

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
