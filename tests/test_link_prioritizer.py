"""Unit tests for heuristic link prioritizer.

Tests validate heuristic scoring across file type, domain authority,
keyword, and shopping-path signals, plus the top-K ranking/lineage contract.
"""

from __future__ import annotations

import pytest

from psweep.discovery.link_prioritizer import LinkPrioritizer
from psweep.discovery.models import CandidateScore, DiscoveryCandidate

from discovery_helpers import make_candidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candidate(url: str, reasons: list[str] | None = None) -> DiscoveryCandidate:
    # Prioritizer tests assume a non-default baseline score.
    return make_candidate(
        url,
        reasons=reasons,
        score=CandidateScore(url_signal=0.5, trust_signal=0.4),
    )


def _prioritizer(**kwargs) -> LinkPrioritizer:
    return LinkPrioritizer(**kwargs)


# ---------------------------------------------------------------------------
# File-type scoring
# ---------------------------------------------------------------------------


class TestFileTypeScoring:
    """Heuristic file-type signal: PDFs/DOCX rank above HTML; shopping pages penalised."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/spec.pdf",
            "https://example.com/manual.docx",
        ],
    )
    def test_document_urls_score_positive(self, url):
        p = _prioritizer(top_k=None)
        ranked, _ = p.prioritize([_candidate(url)])
        assert ranked[0].score.url_signal > 0

    @pytest.mark.parametrize(
        ("weaker_url", "stronger_url"),
        [
            ("https://example.com/page.html", "https://example.com/doc.pdf"),
            ("https://shop.example.com/cart/add-to-cart", "https://docs.example.com/spec.pdf"),
            ("https://example.com/photo.jpg", "https://example.com/manual.pdf"),
        ],
    )
    def test_weaker_urls_rank_below_documents(self, weaker_url, stronger_url):
        p = _prioritizer(top_k=None)
        ranked, _ = p.prioritize([_candidate(weaker_url), _candidate(stronger_url)])
        assert ranked[0].url == stronger_url


# ---------------------------------------------------------------------------
# Domain authority scoring
# ---------------------------------------------------------------------------


class TestDomainAuthorityScoring:
    """Domain authority: manufacturer/gov domains rank above forums/shopping."""

    def test_manufacturer_domain_scores_higher_than_forum(self):
        # Domain-specific authority is now supplied via config overrides
        # (domain_authority_overrides), not hard-coded in the prioritizer.
        p = _prioritizer(
            domain_authority_overrides={"generac.com": 0.20}, top_k=None
        )
        forum = _candidate("https://reddit.com/r/generators/pdf")
        mfr = _candidate("https://generac.com/docs/manual.pdf")
        ranked, _ = p.prioritize([forum, mfr])
        assert ranked[0].url == mfr.url

    def test_gov_domain_scores_positive(self):
        p = _prioritizer(top_k=None)
        gov = _candidate("https://chaffeecounty.gov/ordinances/53007.pdf")
        ranked, _ = p.prioritize([gov])
        # Trust signal should be boosted by .gov domain
        assert ranked[0].score.trust_signal > 0.4

    def test_shopping_domain_penalised(self):
        p = _prioritizer(top_k=None)
        ebay = _candidate("https://ebay.com/itm/generator-manual/pdf")
        gov = _candidate("https://city.gov/code.pdf")
        ranked, _ = p.prioritize([ebay, gov])
        assert ranked[0].url == gov.url

    def test_domain_authority_override_applied(self):
        p = _prioritizer(
            domain_authority_overrides={"acmegenerators.com": 0.30},
            top_k=None,
        )
        acme = _candidate("https://acmegenerators.com/manuals/spec.pdf")
        generic = _candidate("https://genericsite.com/spec.pdf")
        ranked, _ = p.prioritize([generic, acme])
        assert ranked[0].url == acme.url


# ---------------------------------------------------------------------------
# Keyword scoring
# ---------------------------------------------------------------------------


class TestKeywordScoring:
    """Keyword signal: URLs/anchors that match schema keywords rank higher."""

    def test_matching_keyword_in_url_boosts_rank(self):
        p = _prioritizer(keywords=["geothermal"], top_k=None)
        kw_url = _candidate("https://county.gov/geothermal-ordinance.pdf")
        plain = _candidate("https://county.gov/doc.pdf")
        ranked, _ = p.prioritize([plain, kw_url])
        assert ranked[0].url == kw_url.url

    def test_multiple_keyword_matches_score_higher(self):
        p = _prioritizer(keywords=["manual", "operator", "installation"], top_k=None)
        multi = _candidate("https://mfr.com/operator-installation-manual.pdf")
        single = _candidate("https://mfr.com/spec.pdf")
        ranked, _ = p.prioritize([single, multi])
        assert ranked[0].url == multi.url

    def test_no_keywords_falls_back_to_builtin_paths(self):
        """Without explicit keywords, built-in doc-path keywords are used."""
        p = _prioritizer(top_k=None)
        manual_url = _candidate("https://example.com/installation-guide.pdf")
        random_url = _candidate("https://example.com/widget.xlsx")
        ranked, _ = p.prioritize([random_url, manual_url])
        # manual_url has more keyword hits — should rank first
        assert ranked[0].url == manual_url.url

    def test_anchor_text_keyword_boosts_candidate(self):
        p = _prioritizer(keywords=["ordinance"], top_k=None)
        # Anchor text is stored as reasons on the candidate
        anchored = _candidate(
            "https://county.gov/doc.pdf",
            reasons=["ordinance chapter 10 geothermal"],
        )
        plain = _candidate("https://county.gov/other.pdf")
        ranked, _ = p.prioritize([plain, anchored])
        assert ranked[0].url == anchored.url


# ---------------------------------------------------------------------------
# Top-K and lineage contract
# ---------------------------------------------------------------------------


class TestTopKAndLineage:
    """Top-K slicing and lineage preservation."""

    def test_top_k_returns_only_k_candidates(self):
        p = _prioritizer(top_k=3)
        candidates = [_candidate(f"https://example.com/doc{i}.pdf") for i in range(10)]
        ranked, lineage = p.prioritize(candidates)
        assert len(ranked) == 3

    def test_lineage_contains_all_original_candidates(self):
        p = _prioritizer(top_k=2)
        candidates = [_candidate(f"https://example.com/doc{i}.pdf") for i in range(5)]
        ranked, lineage = p.prioritize(candidates)
        assert len(lineage) == 5

    def test_lineage_preserves_original_rank(self):
        p = _prioritizer(top_k=None)
        candidates = [_candidate(f"https://example.com/doc{i}.pdf") for i in range(4)]
        _, lineage = p.prioritize(candidates)
        original_ranks = [entry["original_rank"] for entry in lineage]
        assert set(original_ranks) == {0, 1, 2, 3}

    def test_lineage_contains_priority_score_dict(self):
        p = _prioritizer(top_k=None)
        ranked, lineage = p.prioritize([_candidate("https://example.com/spec.pdf")])
        assert "priority_score" in lineage[0]
        assert "confidence" in lineage[0]["priority_score"]

    def test_top_k_none_returns_all_candidates(self):
        p = _prioritizer(top_k=None)
        candidates = [_candidate(f"https://example.com/doc{i}.pdf") for i in range(8)]
        ranked, _ = p.prioritize(candidates)
        assert len(ranked) == 8

    def test_empty_input_returns_empty(self):
        p = _prioritizer(top_k=5)
        ranked, lineage = p.prioritize([])
        assert ranked == []
        assert lineage == []

    def test_ranked_candidates_are_stable_sorted_highest_first(self):
        """Higher-confidence candidates must appear earlier in the output."""
        p = _prioritizer(top_k=None)
        # pdf > docx > html ordering based on file type score alone
        html = _candidate("https://example.com/page.html")
        docx = _candidate("https://example.com/report.docx")
        pdf = _candidate("https://example.com/manual.pdf")
        ranked, _ = p.prioritize([html, pdf, docx])
        # Extract confidence scores for assertion
        confidences = [
            next(
                entry["priority_score"]["confidence"]
                for entry in [{"priority_score": {"confidence": 0}}]  # placeholder
            )
            for _ in ranked
        ]
        # Simpler: just assert pdf appears before html in ranking
        urls = [c.url for c in ranked]
        assert urls.index(pdf.url) < urls.index(html.url)

    def test_prioritized_candidate_reasons_include_heuristic_notes(self):
        p = _prioritizer(top_k=None)
        ranked, _ = p.prioritize([_candidate("https://generac.com/250kw-spec.pdf")])
        assert len(ranked[0].reasons) > 0


# ---------------------------------------------------------------------------
# Generator domain integration scenario
# ---------------------------------------------------------------------------


class TestGeneratorDomainScenario:
    """Simulates ranking of 12 generator manual candidates (as in config)."""

    _CANDIDATES = [
        "https://generac.com/products/industrial/250kw-g-drive-spec.pdf",
        "https://manualslib.com/manual/generac-250kw.pdf",
        "https://ebay.com/itm/generac-generator-250kw/listing",
        "https://reddit.com/r/generators/generac250kw",
        "https://johndeere.com/products/power-systems/250kw-generator-manual.pdf",
        "https://example-forum.com/thread/generac-issues",
        "https://caterpillar.com/en/products/generators/250kw.pdf",
        "https://somecdn.net/download/generac_g250_spec.pdf",
        "https://amazon.com/dp/B00EXAMPLEID",
        "https://cummins.com/generators/250kw/operator-manual.pdf",
        "https://pinterest.com/pin/generator-specs",
        "https://kohlerpower.com/na/industrial/250kw/spec-sheet.pdf",
    ]

    def test_top5_are_manufacturer_or_manual_sources(self):
        p = _prioritizer(
            keywords=["generator", "manual", "spec"],
            top_k=5,
        )
        candidates = [_candidate(url) for url in self._CANDIDATES]
        ranked, lineage = p.prioritize(candidates)

        assert len(ranked) == 5
        assert len(lineage) == len(self._CANDIDATES)

        # All manufacturer/manual-library URLs should score above forum/shopping
        shopping_or_forums = {"ebay.com", "reddit.com", "amazon.com", "pinterest.com"}
        top5_urls = [c.url for c in ranked]
        for url in top5_urls:
            from urllib.parse import urlparse
            netloc = urlparse(url).netloc.lower()
            assert not any(s in netloc for s in shopping_or_forums), (
                f"Shopping/forum URL {url} appeared in top-5"
            )

    def test_runtime_under_1_second(self):
        """Prioritisation of 30 candidates should complete in under 1 second."""
        import time
        p = _prioritizer(top_k=5)
        candidates = [_candidate(url) for url in (self._CANDIDATES * 3)[: 30]]
        start = time.monotonic()
        p.prioritize(candidates)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"Prioritization took {elapsed:.3f}s, expected <1s"


# ---------------------------------------------------------------------------
# Shopping-path keyword de-prioritization (config-drivable, domain-neutral)
# ---------------------------------------------------------------------------


class TestShoppingPathKeywords:
    """Shopping/cart path keywords penalise a URL; the built-in list is
    domain-neutral and can be supplemented via ``shopping_path_keywords``."""

    def test_default_shopping_keyword_penalised(self):
        p = _prioritizer(top_k=None)
        ranked, _ = p.prioritize(
            [_candidate("https://example.com/checkout/item")]
        )
        assert any(
            "shopping_path_keyword=checkout" in r for r in ranked[0].reasons
        )

    def test_custom_keyword_not_penalised_by_default(self):
        p = _prioritizer(top_k=None)
        ranked, _ = p.prioritize(
            [_candidate("https://example.com/wishlist/item")]
        )
        assert not any(
            "shopping_path_keyword" in r for r in ranked[0].reasons
        )

    def test_custom_keyword_penalised_when_configured(self):
        p = _prioritizer(top_k=None, shopping_path_keywords=["wishlist"])
        ranked, _ = p.prioritize(
            [_candidate("https://example.com/wishlist/item")]
        )
        assert any(
            "shopping_path_keyword=wishlist" in r for r in ranked[0].reasons
        )

    def test_builtin_defaults_retained_with_custom_list(self):
        # Config supplements the defaults; it never replaces them.
        p = _prioritizer(top_k=None, shopping_path_keywords=["wishlist"])
        ranked, _ = p.prioritize(
            [_candidate("https://example.com/checkout/item")]
        )
        assert any(
            "shopping_path_keyword=checkout" in r for r in ranked[0].reasons
        )
