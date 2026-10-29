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

def _candidate(
    url: str, reasons: list[str] | None = None, **extra: object
) -> DiscoveryCandidate:
    # Prioritizer tests assume a non-default baseline score.
    return make_candidate(
        url,
        reasons=reasons,
        score=CandidateScore(url_signal=0.5, trust_signal=0.4),
        **extra,
    )


def _prioritizer(**kwargs) -> LinkPrioritizer:
    return LinkPrioritizer(**kwargs)


# ---------------------------------------------------------------------------
# File-type scoring
# ---------------------------------------------------------------------------


class TestFileTypeScoring:
    """Heuristic file-type signal: PDFs/DOCX rank above HTML; shopping pages penalised."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://example.com/spec.pdf", 0.40),
            ("https://example.com/manual.docx", 0.35),
        ],
    )
    def test_document_urls_score_positive(self, url, expected):
        p = _prioritizer(top_k=None)
        ranked, lineage = p.prioritize([_candidate(url)])
        assert ranked[0].score.url_signal > 0
        assert lineage[0]["priority_score"]["file_type_score"] == expected

    def test_file_authority_keyword_aggregate_weights_are_preserved(self):
        p = _prioritizer(
            keywords=["sensor"],
            domain_authority_overrides={".gov": 0.15},
            top_k=None,
        )
        _, lineage = p.prioritize([
            _candidate("https://example.gov/a.pdf", title="Sensor")
        ])
        score = lineage[0]["priority_score"]
        assert score["file_type_score"] == 0.40
        assert score["domain_authority_score"] == 0.15
        assert score["keyword_score"] == 0.30
        assert score["confidence"] == 0.7525

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
    """Authority signals come only from explicitly configured host patterns."""

    def test_manufacturer_domain_scores_higher_than_forum(self):
        p = _prioritizer(
            domain_authority_overrides={
                "generac.com": 0.20,
                "reddit.com": -0.15,
            },
            top_k=None,
        )
        forum = _candidate("https://reddit.com/spec.pdf")
        mfr = _candidate("https://generac.com/spec.pdf")
        ranked, lineage = p.prioritize([forum, mfr])
        assert ranked[0].url == mfr.url
        assert lineage[0]["priority_score"]["domain_authority_score"] == 0.20
        assert lineage[1]["priority_score"]["domain_authority_score"] == -0.15

    def test_configured_gov_domain_scores_positive(self):
        p = _prioritizer(
            domain_authority_overrides={".gov": 0.15}, top_k=None
        )
        gov = _candidate("https://chaffeecounty.gov/ordinances/53007.pdf")
        ranked, lineage = p.prioritize([gov])
        assert ranked[0].score.trust_signal == pytest.approx(0.475)
        assert lineage[0]["priority_score"]["domain_authority_score"] == 0.15

    def test_configured_shopping_domain_penalised(self):
        p = _prioritizer(
            domain_authority_overrides={"ebay.com": -0.20, ".gov": 0.15},
            top_k=None,
        )
        ebay = _candidate("https://ebay.com/spec.pdf")
        gov = _candidate("https://city.gov/spec.pdf")
        ranked, lineage = p.prioritize([ebay, gov])
        assert ranked[0].url == gov.url
        assert ranked[1].score.trust_signal == pytest.approx(0.30)
        assert lineage[1]["priority_score"]["domain_authority_score"] == -0.20

    def test_domain_authority_override_applied(self):
        p = _prioritizer(
            domain_authority_overrides={"acmegenerators.com": 0.30},
            top_k=None,
        )
        acme = _candidate("https://acmegenerators.com/spec.pdf")
        generic = _candidate("https://genericsite.com/spec.pdf")
        ranked, _ = p.prioritize([generic, acme])
        assert ranked[0].url == acme.url

    @pytest.mark.parametrize(
        "host",
        [
            "county.gov", "college.edu", "ebay.com", "amazon.com",
            "etsy.com", "aliexpress.com", "reddit.com", "quora.com",
            "answers.com", "pinterest.com", "slideshare.net", "generac.com",
        ],
    )
    def test_unconfigured_hosts_have_no_authority_adjustment(self, host):
        p = _prioritizer(top_k=None)
        ranked, lineage = p.prioritize([_candidate(f"https://{host}/spec.pdf")])
        assert lineage[0]["priority_score"]["domain_authority_score"] == 0.0
        assert ranked[0].score.trust_signal == 0.4
        assert not any("domain_authority:" in r for r in ranked[0].reasons)

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://example.com/spec.pdf", 0.2),
            ("https://docs.example.com/spec.pdf", 0.2),
            ("https://EXAMPLE.COM.:443/spec.pdf", 0.2),
            ("https://example.com.evil.test/spec.pdf", 0.0),
            ("https://notexample.com/spec.pdf", 0.0),
            ("https://example.com@evil.test/spec.pdf", 0.0),
            ("https://evil.test/example.com/spec.pdf", 0.0),
        ],
    )
    def test_host_patterns_match_only_exact_host_or_subdomain(
        self, url, expected
    ):
        p = _prioritizer(
            domain_authority_overrides={"example.com": 0.2}, top_k=None
        )
        _, lineage = p.prioritize([_candidate(url)])
        authority = lineage[0]["priority_score"]["domain_authority_score"]
        assert authority == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://county.gov/spec.pdf", 0.15),
            ("https://docs.county.gov/spec.pdf", 0.15),
            ("https://college.edu/spec.pdf", 0.1),
            ("https://county.gov.evil.test/spec.pdf", 0.0),
            ("https://county.government/spec.pdf", 0.0),
            ("https://college.edu@evil.test/spec.pdf", 0.0),
            ("https://notgov/spec.pdf", 0.0),
            ("https://evil.test/spec.pdf?host=county.gov", 0.0),
        ],
    )
    def test_leading_dot_patterns_match_safe_hostname_suffixes(
        self, url, expected
    ):
        p = _prioritizer(
            domain_authority_overrides={".gov": 0.15, ".edu": 0.1}, top_k=None
        )
        _, lineage = p.prioritize([_candidate(url)])
        authority = lineage[0]["priority_score"]["domain_authority_score"]
        assert authority == expected

    def test_unrelated_host_has_no_configured_authority_boost(self):
        p = _prioritizer(
            domain_authority_overrides={"example.com": 0.2}, top_k=None
        )
        _, lineage = p.prioritize([_candidate("https://elsewhere.gov/spec.pdf")])
        assert lineage[0]["priority_score"]["domain_authority_score"] == 0.0

    def test_overlapping_configured_authority_scores_remain_clamped(self):
        p = _prioritizer(
            domain_authority_overrides={".gov": 0.3, "county.gov": 0.3},
            top_k=None,
        )
        _, lineage = p.prioritize([_candidate("https://county.gov/spec.pdf")])
        assert lineage[0]["priority_score"]["domain_authority_score"] == 0.5


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

    def test_no_keywords_falls_back_to_generic_document_paths(self):
        p = _prioritizer(top_k=None)
        guide = _candidate("https://example.com/guide.pdf")
        plain = _candidate("https://example.com/widget.pdf")
        ranked, lineage = p.prioritize([plain, guide])
        assert ranked[0].url == guide.url
        assert lineage[0]["priority_score"]["keyword_score"] == 0.3
        assert lineage[1]["priority_score"]["keyword_score"] == 0.0

    @pytest.mark.parametrize("field", ["title", "snippet"])
    def test_source_text_keyword_boosts_candidate(self, field):
        p = _prioritizer(keywords=["ordinance"], top_k=None)
        anchored = _candidate(
            "https://county.gov/a.pdf",
            **{field: "ordinance chapter 10 geothermal"},
        )
        plain = _candidate("https://county.gov/b.pdf")
        ranked, lineage = p.prioritize([plain, anchored])
        assert ranked[0].url == anchored.url
        assert lineage[0]["priority_score"]["keyword_score"] == 0.3
        assert lineage[1]["priority_score"]["keyword_score"] == 0.0

    def test_reason_only_keywords_do_not_boost_candidate(self):
        p = _prioritizer(keywords=["ordinance"], top_k=None)
        plain = _candidate("https://example.com/a.pdf")
        query_reason = _candidate(
            "https://example.com/b.pdf",
            reasons=["SerpApi query: ordinance download guide report pdf"],
        )
        ranked, lineage = p.prioritize([plain, query_reason])
        assert [c.url for c in ranked] == [plain.url, query_reason.url]
        assert all(
            e["priority_score"]["keyword_score"] == 0.0 for e in lineage
        )
        assert query_reason.reasons[0] in ranked[1].reasons

    def test_configured_keywords_do_not_merge_generic_fallback(self):
        p = _prioritizer(keywords=["sensor"], top_k=None)
        sensor = _candidate("https://example.com/sensor.pdf")
        report = _candidate("https://example.com/download-guide-report.pdf")
        ranked, lineage = p.prioritize([report, sensor])
        assert ranked[0].url == sensor.url
        assert lineage[1]["priority_score"]["keyword_score"] == 0.0

    def test_reason_only_file_type_hint_does_not_boost_candidate(self):
        p = _prioritizer(top_k=None)
        _, lineage = p.prioritize([
            _candidate("https://example.com/a", reasons=["query: pdf manual"])
        ])
        assert lineage[0]["priority_score"]["file_type_score"] == 0.0

    @pytest.mark.parametrize("field", ["title", "snippet"])
    def test_source_file_type_hint_boosts_candidate(self, field):
        p = _prioritizer(top_k=None)
        _, lineage = p.prioritize([
            _candidate("https://example.com/a", **{field: "PDF copy"})
        ])
        assert lineage[0]["priority_score"]["file_type_score"] == 0.40


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
        ranked, lineage = p.prioritize([html, pdf, docx])
        confidences = [
            entry["priority_score"]["confidence"] for entry in lineage
        ]
        assert confidences == sorted(confidences, reverse=True)
        urls = [c.url for c in ranked]
        assert urls.index(pdf.url) < urls.index(html.url)

    def test_prioritized_candidate_reasons_include_heuristic_notes(self):
        p = _prioritizer(top_k=None)
        ranked, _ = p.prioritize([_candidate("https://generac.com/250kw-spec.pdf")])
        assert len(ranked[0].reasons) > 0

    def test_replaced_candidate_preserves_source_and_validation_metadata(self):
        candidate = _candidate(
            "https://example.com/a.pdf",
            reasons=["source evidence"],
            title="Sensor PDF",
            snippet="Download the sensor document",
            target_metadata={"asset": "sensor"},
            status="validated",
            mime_type="application/pdf",
            extension=".pdf",
            canonical_url="https://example.com/canonical.pdf",
            content_hash="abc123",
        )
        ranked, _ = _prioritizer(top_k=None).prioritize([candidate])
        result = ranked[0]
        assert result is not candidate
        assert result.title == candidate.title
        assert result.snippet == candidate.snippet
        assert result.target_metadata == candidate.target_metadata
        assert result.source == candidate.source
        assert result.status == candidate.status
        assert result.mime_type == candidate.mime_type
        assert result.extension == candidate.extension
        assert result.canonical_url == candidate.canonical_url
        assert result.content_hash == candidate.content_hash
        assert result.score.weights == candidate.score.weights
        assert candidate.reasons == ["source evidence"]


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
            domain_authority_overrides={
                "generac.com": 0.20,
                "johndeere.com": 0.20,
                "caterpillar.com": 0.20,
                "cummins.com": 0.20,
                "kohlerpower.com": 0.20,
                "ebay.com": -0.20,
                "reddit.com": -0.15,
                "amazon.com": -0.15,
                "pinterest.com": -0.20,
            },
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
    """Only configured, boundary-matched shopping path terms penalise URLs."""

    @pytest.mark.parametrize(
        "path", ["checkout/item", "shop/item", "wishlist/item"]
    )
    def test_shopping_paths_not_penalised_by_default(self, path):
        p = _prioritizer(top_k=None)
        ranked, lineage = p.prioritize([_candidate(f"https://example.com/{path}")])
        assert not any("shopping_path_keyword" in r for r in ranked[0].reasons)
        assert lineage[0]["priority_score"]["file_type_score"] == 0.0

    def test_configured_keyword_penalised(self):
        p = _prioritizer(top_k=None, shopping_path_keywords=["wishlist"])
        ranked, lineage = p.prioritize(
            [_candidate("https://example.com/wishlist/item")]
        )
        assert any(
            "shopping_path_keyword=wishlist" in r for r in ranked[0].reasons
        )
        assert lineage[0]["priority_score"]["file_type_score"] == -0.10

    def test_custom_list_does_not_add_implicit_keywords(self):
        p = _prioritizer(top_k=None, shopping_path_keywords=["wishlist"])
        ranked, lineage = p.prioritize(
            [_candidate("https://example.com/checkout/item")]
        )
        assert not any("shopping_path_keyword" in r for r in ranked[0].reasons)
        assert lineage[0]["priority_score"]["file_type_score"] == 0.0

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://example.com/shop/item", -0.10),
            ("https://example.com/SHOP/item", -0.10),
            ("https://example.com/web-shop/item", -0.10),
            ("https://example.com/shop_item", -0.10),
            ("https://example.com/%73hop/item", -0.10),
            ("https://example.com/workshop/item", 0.0),
            ("https://example.com/shopping/item", 0.0),
            ("https://shop.example.com/item", 0.0),
            ("https://example.com/item?q=shop", 0.0),
            ("https://example.com/item#shop", 0.0),
        ],
    )
    def test_path_token_boundaries_prevent_shopping_false_positives(
        self, url, expected
    ):
        p = _prioritizer(top_k=None, shopping_path_keywords=["shop"])
        _, lineage = p.prioritize([_candidate(url)])
        assert lineage[0]["priority_score"]["file_type_score"] == expected

    def test_multi_token_path_keyword_is_matched_literally(self):
        p = _prioritizer(top_k=None, shopping_path_keywords=["add-to-cart"])
        _, lineage = p.prioritize([
            _candidate("https://example.com/add-to-cart/item"),
            _candidate("https://example.com/notadd-to-cartoon/item"),
        ])
        scores = {
            e["url"]: e["priority_score"]["file_type_score"] for e in lineage
        }
        assert scores["https://example.com/add-to-cart/item"] == -0.10
        assert scores["https://example.com/notadd-to-cartoon/item"] == 0.0

    def test_direct_document_file_scores_are_preserved(self):
        p = _prioritizer(top_k=None, shopping_path_keywords=["shop"])
        _, lineage = p.prioritize([_candidate("https://example.com/shop/spec.pdf")])
        assert lineage[0]["priority_score"]["file_type_score"] == 0.40
