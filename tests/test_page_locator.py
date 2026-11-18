"""Tests for LLM-assisted page targeting (PageLocator + process integration)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dataclasses import replace

from psweep.extraction.page_locator import PageLocator, PageSelection
from psweep.extraction.provenance import SourceContext
from psweep.extraction import page_locator
from psweep.utils.page_range import page_numbers


class _FakeClient:
    """Stand-in LLM client that returns canned ranges and counts calls."""

    def __init__(self, ranges):
        self._ranges = ranges
        self.calls = 0

    def extract(self, **_kwargs):
        self.calls += 1
        return {"data": {"ranges": self._ranges}}


def _pages_with_section(section_pages: set[int], total: int) -> list[str]:
    """Build fake page texts; section pages are keyword-dense."""
    pages = []
    for i in range(total):
        if i in section_pages:
            pages.append(
                "Residential rate schedule energy charge per kwh monthly customer charge rate " * 5
            )
        else:
            pages.append("General provisions and definitions text.")
    return pages


class TestHeuristic:
    def test_candidate_pages_include_dense_pages_and_neighbors(self):
        loc = PageLocator("residential rate schedule per kwh", context_pages=1)
        pages = _pages_with_section({10}, total=20)
        candidates = loc._candidate_pages(pages)
        # dense page 10 plus one neighbor on each side (0-indexed)
        assert candidates == [9, 10, 11]

    def test_no_candidates_when_no_keyword_hits(self):
        loc = PageLocator("residential rate schedule per kwh")
        pages = ["nothing relevant here"] * 5
        assert loc._candidate_pages(pages) == []

    def test_derive_keywords_drops_stopwords(self):
        loc = PageLocator("the residential rate schedule for each page")
        assert "residential" in loc._keywords
        assert "the" not in loc._keywords and "for" not in loc._keywords


class TestLocate:
    def _pdf(self, tmp_path: Path) -> Path:
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        return pdf

    def test_locate_returns_range_and_writes_cache(self, tmp_path, monkeypatch):
        pdf = self._pdf(tmp_path)
        loc = PageLocator("residential rate schedule per kwh")
        fake = _FakeClient([{"start": 11, "end": 12}])
        monkeypatch.setattr(loc, "_ensure_client", lambda: fake)

        pages = _pages_with_section({10, 11}, total=20)
        assert loc.locate(pdf, pages=pages) == PageSelection(((11, 12),))
        assert fake.calls == 2  # locate, then one reference pass that adds nothing
        assert (tmp_path / ".pages" / "doc.json").exists()

    def test_cache_hit_skips_llm(self, tmp_path, monkeypatch):
        pdf = self._pdf(tmp_path)
        loc = PageLocator("residential rate schedule per kwh")
        fake = _FakeClient([{"start": 11, "end": 12}])
        monkeypatch.setattr(loc, "_ensure_client", lambda: fake)
        pages = _pages_with_section({10, 11}, total=20)

        loc.locate(pdf, pages=pages)
        # Second locate on a fresh locator instance must read the cache, not LLM.
        loc2 = PageLocator("residential rate schedule per kwh")
        fake2 = _FakeClient([{"start": 1, "end": 2}])
        monkeypatch.setattr(loc2, "_ensure_client", lambda: fake2)
        assert loc2.locate(pdf, pages=pages) == PageSelection(((11, 12),))
        assert fake2.calls == 0  # served from cache

    def test_cache_invalidated_when_section_changes(self, tmp_path, monkeypatch):
        pdf = self._pdf(tmp_path)
        pages = _pages_with_section({10, 11}, total=20)
        loc = PageLocator("residential rate schedule per kwh")
        monkeypatch.setattr(
            loc,
            "_ensure_client",
            lambda: _FakeClient([{"start": 11, "end": 12}]),
        )
        loc.locate(pdf, pages=pages)

        # Different section description → cache miss → LLM runs again.
        loc2 = PageLocator("commercial demand charges")
        fake2 = _FakeClient([{"start": 3, "end": 4}])
        monkeypatch.setattr(loc2, "_ensure_client", lambda: fake2)
        # Give loc2 keywords that hit the fake pages so candidates exist.
        loc2._keywords = ["residential", "charge"]
        assert loc2.locate(pdf, pages=pages) == PageSelection(((3, 4),))
        assert fake2.calls == 2

    def test_range_capped_to_max_selected_pages(self, tmp_path, monkeypatch):
        pdf = self._pdf(tmp_path)
        loc = PageLocator("residential rate schedule per kwh", max_selected_pages=5)
        monkeypatch.setattr(
            loc,
            "_ensure_client",
            lambda: _FakeClient([{"start": 10, "end": 100}]),
        )
        pages = _pages_with_section({10, 11, 12}, total=120)
        # The first 5 pages are kept; the rest of the answer is reported as omitted.
        assert loc.locate(pdf, pages=pages) == PageSelection(((10, 14),), ((15, 100),))

    def test_returns_none_without_candidates(self, tmp_path):
        pdf = self._pdf(tmp_path)
        loc = PageLocator("residential rate schedule per kwh")
        assert loc.locate(pdf, pages=["irrelevant"] * 5) is None


class TestCacheIntegrity:
    def _seed(self, tmp_path, monkeypatch, **kwargs):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"before")
        locator = PageLocator("rate schedule", **kwargs)
        monkeypatch.setattr(
            locator,
            "_ensure_client",
            lambda: _FakeClient([{"start": 2, "end": 3}]),
        )
        assert locator.locate(source, pages=["rate schedule"] * 5) == PageSelection(((2, 3),))
        return source, locator

    def test_explicit_empty_keywords_respected(self):
        loc = PageLocator("rate schedule", keywords=[])
        assert loc._candidate_pages(["rate schedule"]) == []

    def test_source_digest_invalidates_same_stat(self, tmp_path, monkeypatch):
        source, loc = self._seed(tmp_path, monkeypatch)
        stat = source.stat()
        source.write_bytes(b"after!")
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert loc._read_cache(source) is None

    @pytest.mark.parametrize(
        "changed",
        [
            {"keywords": []},
            {"trigger_chars": 1},
            {"snippet_chars": 900},
            {"context_pages": 2},
            {"max_selected_pages": 1},
            {"model": "gpt-4o-mini"},
        ],
    )
    def test_effective_settings_invalidate_cache(self, tmp_path, monkeypatch, changed):
        source, _ = self._seed(tmp_path, monkeypatch, model="gpt-4o")
        kwargs = {"model": "gpt-4o", **changed}
        assert PageLocator("rate schedule", **kwargs)._read_cache(source) is None

    @pytest.mark.parametrize("bad_range", [[0, 2], [4, 3], [1, 8], [1, 5], [True, 2], [1.5, 2]])
    def test_cached_bounds_and_cap_validated(self, tmp_path, monkeypatch, bad_range):
        source, loc = self._seed(tmp_path, monkeypatch, max_selected_pages=2)
        cache = loc._cache_path(source)
        meta = json.loads(cache.read_text())
        meta["ranges"] = [bad_range]
        cache.write_text(json.dumps(meta))
        assert loc._read_cache(source) is None

    def test_cached_lookup_does_not_parse_or_construct_client(self, tmp_path, monkeypatch):
        source, loc = self._seed(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "psweep.extraction.pdf_utils.extract_pages_text",
            lambda _p: pytest.fail("parsed cache hit"),
        )
        monkeypatch.setattr(loc, "_ensure_client", lambda: pytest.fail("built client"))
        assert loc.locate_cached(source) == PageSelection(((2, 3),))

    def test_resolved_ambient_model_invalidates_cache(self, tmp_path, monkeypatch):
        config = {"model": "gpt-4o"}
        monkeypatch.setattr(
            "psweep.extraction.llm_factory._get_llm_config",
            lambda _cfg: config,
        )
        source, _ = self._seed(tmp_path, monkeypatch)
        config["model"] = "gpt-4o-mini"
        assert PageLocator("rate schedule")._read_cache(source) is None


def test_page_locator_applies_trigger_when_loading_its_own_pages(tmp_path, monkeypatch):
    """Shared workflow targeting does not call the LLM for small PDFs."""
    source = tmp_path / "small.pdf"
    source.write_bytes(b"fake")
    loc = PageLocator("rates", trigger_chars=1000)
    monkeypatch.setattr("psweep.extraction.pdf_utils.extract_pages_text", lambda _p: ["rates"])
    monkeypatch.setattr(loc, "_ensure_client", lambda: pytest.fail("small PDF located"))
    assert loc.locate(source) is None


class TestRangeResponses:
    @pytest.mark.parametrize(
        "ranges",
        [
            [{"start": 4, "end": 2}],
            [{"start": True, "end": 2}],
            [{"start": 1.5, "end": 2}],
            [{"start": 1}, {"start": 4, "end": 4}],
        ],
    )
    def test_malformed_ranges_never_widen_selection(self, tmp_path, monkeypatch, ranges):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"fake")
        locator = PageLocator("rates")
        monkeypatch.setattr(locator, "_ensure_client", lambda: _FakeClient(ranges))
        assert locator.locate(source, pages=["rates"] * 5) is None
        assert locator.incomplete


class TestNonPdfGuard:
    def test_locate_on_non_pdf_returns_none(self, tmp_path):
        """PageLocator.locate() on a non-PDF path returns None without raising."""
        html = tmp_path / "doc.html"
        html.write_text("<html><body>irrelevant content</body></html>")
        loc = PageLocator("rate schedule charges")
        # Pass an empty pages list to avoid pymupdf I/O; locate() returns None early.
        result = loc.locate(html, pages=[])
        assert result is None

    def test_llm_failure_in_page_locator_emits_no_error_log(self, tmp_path, monkeypatch, caplog):
        """When page_locator's LLM call fails, no ERROR-level entry is emitted."""
        import logging
        from psweep.extraction.page_locator import PageLocator

        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")

        class _FailClient:
            def extract(self, **_kwargs):
                from psweep.exceptions import ExtractionError

                raise ExtractionError("simulated empty response")

        loc = PageLocator("rate schedule charges")
        monkeypatch.setattr(loc, "_ensure_client", _FailClient)

        pages = _pages_with_section({2, 3}, total=10)
        with caplog.at_level(logging.DEBUG):
            result = loc.locate(pdf, pages=pages)

        assert result is None
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_records, f"Unexpected ERROR logs: {[r.message for r in error_records]}"


def test_locator_names_the_model_that_answered():
    loc = PageLocator("residential rate schedule per kwh")
    assert loc.model is None
    loc._client = type("Client", (), {"raw_model": "gpt-x"})()
    assert loc.model == "gpt-x"


class _ScriptedClient:
    """Answers each call with the next scripted ranges (or raises) and keeps prompts."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.prompts = []

    def extract(self, **kwargs):
        self.prompts.append(kwargs["user_prompt"])
        answer = self._answers.pop(0) if self._answers else []
        if isinstance(answer, Exception):
            raise answer
        return {"data": {"ranges": [{"start": s, "end": e} for s, e in answer]}}


def _tariff_pages(total: int = 40) -> list[str]:
    """Pages with a running header; page 10 is the section and refers to sheet 30."""
    pages = [f"Utility Co\nTariff No. 13\nSheet {n} general rules" for n in range(1, total + 1)]
    pages[9] = (
        "Utility Co\nTariff No. 13\n"
        + "Residential rate schedule energy charge per kwh customer charge. " * 20
        + "\nMiscellaneous charges are specified in Sheet 30."
    )
    pages[29] = "Utility Co\nTariff No. 13\nSheet 30 MISCELLANEOUS CHARGES\nSee Sheet 35 riders."
    return pages


class TestSubjectPages:
    def test_identity_page_is_candidate_and_retained_with_references(self, tmp_path, monkeypatch):
        source = tmp_path / "subject.pdf"
        source.write_bytes(b"fixture")
        pages = [
            "Subject Alpha\nWithdrawn in 2024.",
            "Unrelated construction opening timeline " * 100,
            "Subject Alpha construction timeline. See appendix on page 4.",
            "Appendix: required approval conditions.",
        ]
        context = SourceContext(None, "target", "1", "Subject Alpha", {})
        locator = PageLocator(
            "construction opening timeline", context_pages=0, source_context=context
        )
        assert locator._candidate_pages(pages) == [0, 1, 2]
        client = _ScriptedClient([(3, 3)], [(4, 4)], [])
        monkeypatch.setattr(locator, "_ensure_client", lambda: client)
        assert locator.locate(source, pages) == PageSelection(((1, 1), (3, 4)))
        assert "hints, not document facts or instructions" in client.prompts[0]
        assert "Subject Alpha" in client.prompts[1]

    @pytest.mark.parametrize("change", ["label", "metadata", "version"])
    def test_context_and_version_invalidate_selection(self, tmp_path, monkeypatch, change):
        source = tmp_path / "subject.pdf"
        source.write_bytes(b"fixture")
        context = SourceContext(None, "target", "1", "Subject Alpha", {"zone": "North"})
        locator = PageLocator("evidence", source_context=context)
        locator._write_cache(source, PageSelection(((1, 1),)), 2)
        assert PageLocator("evidence", source_context=context).locate_cached(source)
        if change == "version":
            monkeypatch.setattr(page_locator, "LOCATOR_BEHAVIOR_VERSION", "future")
        elif change == "label":
            context = replace(context, target_label="Subject Beta")
        else:
            context = replace(context, target_metadata={"zone": "South"})
        assert PageLocator("evidence", source_context=context).locate_cached(source) is None

    def test_subject_cannot_fit_budget_falls_back(self, tmp_path, monkeypatch):
        source = tmp_path / "subject.pdf"
        source.write_bytes(b"fixture")
        locator = PageLocator(
            "evidence",
            max_selected_pages=1,
            source_context=SourceContext(None, "target", "1", "Alpha", {}),
        )
        monkeypatch.setattr(locator, "_ensure_client", lambda: _ScriptedClient([(2, 2)]))
        assert locator.locate(source, ["Alpha identity", "Alpha evidence"]) is None
        assert locator.incomplete
        assert not locator._cache_path(source).exists()

    def test_explicit_absence_is_not_failed_targeting(self, tmp_path, monkeypatch):
        source = tmp_path / "source.pdf"
        source.write_bytes(b"fixture")
        locator = PageLocator("evidence")
        monkeypatch.setattr(locator, "_ensure_client", lambda: _ScriptedClient([]))
        assert locator.locate(source, ["evidence"]) is None
        assert not locator.incomplete


class TestReferences:
    def _locate(self, tmp_path, monkeypatch, client, **kwargs):
        pdf = tmp_path / "tariff.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        loc = PageLocator("residential rate schedule per kwh", **kwargs)
        monkeypatch.setattr(loc, "_ensure_client", lambda: client)
        return pdf, loc, loc.locate(pdf, pages=_tariff_pages())

    def test_primary_answer_keeps_disjoint_ranges(self, tmp_path, monkeypatch):
        client = _ScriptedClient([(3, 3), (8, 9)])
        _, _, selection = self._locate(tmp_path, monkeypatch, client)
        assert selection == PageSelection(((3, 3), (8, 9)))

    def test_follows_references_transitively_and_caches(self, tmp_path, monkeypatch):
        client = _ScriptedClient([(10, 10)], [(30, 31)], [(35, 35)], [])
        pdf, loc, selection = self._locate(tmp_path, monkeypatch, client)
        assert selection == PageSelection(((10, 10), (30, 31), (35, 35)))
        assert len(client.prompts) == 4
        first_hop = client.prompts[1]
        # Full page text reaches the model, past the 600-character locate snippets.
        assert "Miscellaneous charges are specified in Sheet 30." in first_hop
        assert "[page 30] Sheet 30 MISCELLANEOUS CHARGES See Sheet 35 riders." in first_hop
        assert "[page 1] Sheet 1 general rules" in first_hop  # running header stripped
        assert "ALREADY SELECTED PAGES: 10\n" in first_hop
        assert "ALREADY SELECTED PAGES: 10,30-31\n" in client.prompts[2]
        assert "[page 30]\nUtility Co" in client.prompts[2]  # only new pages are read
        assert "specified in Sheet 30." not in client.prompts[2]
        assert not loc.incomplete
        cached = PageLocator("residential rate schedule per kwh").locate_cached(pdf)
        assert cached == selection

    def test_parts_that_do_not_fit_the_budget_are_omitted(self, tmp_path, monkeypatch):
        client = _ScriptedClient([(10, 10)], [(20, 24), (30, 31)], [(33, 34)])
        _, _, selection = self._locate(tmp_path, monkeypatch, client, max_selected_pages=4)
        # 20-24 does not fit beside page 10, but the smaller referenced part does.
        assert selection == PageSelection(((10, 10), (30, 31)), ((20, 24), (33, 34)))
        assert len(client.prompts) == 3

    def test_failed_reference_pass_is_incomplete_and_not_cached(self, tmp_path, monkeypatch):
        client = _ScriptedClient([(10, 10)], RuntimeError("provider down"))
        pdf, loc, selection = self._locate(tmp_path, monkeypatch, client)
        assert selection == PageSelection(((10, 10),))
        assert loc.incomplete
        assert not loc._cache_path(pdf).exists()

    def test_outline_drops_running_headers_and_footers(self):
        pages = ["Head\nA one\nFoot", "Head\nB  two\nFoot", "Head\nC three\nFoot"]
        assert PageLocator._outline(pages) == "[page 1] A one\n[page 2] B two\n[page 3] C three"
        assert PageLocator._outline(["Only page"]) == "[page 1] Only page"


def _issuer_context(name="Acme Electric"):
    return SourceContext(None, "target", "1", name, {})


class TestMandatoryIdentityAnchors:
    """Repeated running headers must not make every page mandatory."""

    def _locator(self, **kwargs):
        return PageLocator(
            "residential rate schedule", context_pages=0, source_context=_issuer_context(), **kwargs
        )

    def test_600_page_repeated_header_selects_target_and_rider(self, tmp_path, monkeypatch):
        source = tmp_path / "book.pdf"
        source.write_bytes(b"fixture")
        pages = [f"Acme Electric\nSheet {n} general rules" for n in range(1, 601)]
        pages[549] = (
            "Acme Electric\nResidential rate schedule energy charge. See rider on Sheet 590."
        )
        pages[589] = "Acme Electric\nSheet 590 rider text"
        locator = self._locator(max_selected_pages=30)
        client = _ScriptedClient([(550, 550)], [(590, 590)], [])
        monkeypatch.setattr(locator, "_ensure_client", lambda: client)
        selection = locator.locate(source, pages)
        assert selection == PageSelection(((1, 1), (550, 550), (590, 590)))
        assert not locator.incomplete

    def test_candidates_are_not_narrowed(self):
        pages = [f"Acme Electric\nSheet {n}" for n in range(1, 41)]
        assert self._locator()._candidate_pages(pages) == list(range(40))

    def test_case_and_whitespace_variants_are_one_pattern(self):
        pages = ["ACME  electric", "acme electric ", "Acme Electric", "Sheet 4 text"]
        locator = self._locator()
        assert locator._mandatory_indices(pages) == [0]
        assert pages[0] == "ACME  electric"  # original text untouched

    def test_body_occurrence_is_retained(self):
        pages = [
            "Acme Electric\nGeneral rules A",
            "Acme Electric\nBody mentions Acme Electric here",
            "Acme Electric\nGeneral rules C",
        ]
        assert self._locator()._mandatory_indices(pages) == [0, 1]

    def test_wrapped_body_identity_is_retained(self):
        pages = [
            "Acme Electric\nGeneral rules A",
            "Acme Electric\nIssued by Acme\nElectric Company",
            "Acme Electric\nGeneral rules C",
        ]
        assert self._locator()._mandatory_indices(pages) == [0, 1]

    def test_unique_status_page_survives_common_margins(self):
        pages = [
            "Acme Electric\nrules\nPage footer",
            "Acme Electric\nThis tariff for Acme Electric is WITHDRAWN\nPage footer",
            "Acme Electric\nrules\nPage footer",
        ]
        assert self._locator()._mandatory_indices(pages) == [0, 1]

    def test_header_and_footer_patterns_each_keep_a_representative(self):
        pages = ["Acme Electric\nbody\nplain"] + ["Acme Electric\nbody\nAcme Electric Rev 3"] * 4
        assert self._locator()._mandatory_indices(pages) == [0, 1]

    def test_distinct_identity_pages_over_budget_still_reject(self, tmp_path, monkeypatch):
        source = tmp_path / "book.pdf"
        source.write_bytes(b"fixture")
        pages = [f"Notice {n}: Acme Electric unique status {n}" for n in range(6)]
        locator = self._locator(max_selected_pages=3)
        monkeypatch.setattr(locator, "_ensure_client", lambda: _ScriptedClient([(1, 1)]))
        assert locator.locate(source, pages) is None
        assert locator.incomplete
        assert not locator._cache_path(source).exists()


class TestFinalReferenceFrontier:
    def _locate(self, tmp_path, monkeypatch, client, pages=20, **kwargs):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"fixture")
        locator = PageLocator("rates", context_pages=0, **kwargs)
        monkeypatch.setattr(locator, "_ensure_client", lambda: client)
        return source, locator, locator.locate(source, ["rates"] * pages)

    def test_full_cap_primary_dependency_is_reported_omitted(self, tmp_path, monkeypatch):
        client = _ScriptedClient([(1, 1)], [(2, 2)])
        _, locator, selection = self._locate(tmp_path, monkeypatch, client, max_selected_pages=1)
        assert selection == PageSelection(((1, 1),), ((2, 2),))
        assert len(client.prompts) == 2
        assert not locator.incomplete

    def test_transitive_hop_that_fills_cap_is_examined(self, tmp_path, monkeypatch):
        client = _ScriptedClient([(1, 1)], [(2, 2)], [(3, 3)])
        _, _, selection = self._locate(tmp_path, monkeypatch, client, max_selected_pages=2)
        assert selection == PageSelection(((1, 2),), ((3, 3),))
        assert len(client.prompts) == 3

    def test_final_empty_answer_is_complete_and_cached(self, tmp_path, monkeypatch):
        client = _ScriptedClient([(1, 1)], [])
        source, locator, selection = self._locate(
            tmp_path, monkeypatch, client, max_selected_pages=1
        )
        assert selection == PageSelection(((1, 1),))
        assert locator._cache_path(source).exists()

    @pytest.mark.parametrize("failure", [RuntimeError("down"), [(0, 0), (3, 3)]])
    def test_final_failure_keeps_selection_but_is_incomplete(self, tmp_path, monkeypatch, failure):
        client = _ScriptedClient([(1, 1)], failure)
        source, locator, selection = self._locate(
            tmp_path, monkeypatch, client, max_selected_pages=1
        )
        assert selection == PageSelection(((1, 1),))
        assert locator.incomplete
        assert not locator._cache_path(source).exists()

    def test_cycles_overlaps_and_self_references_terminate(self, tmp_path, monkeypatch):
        client = _ScriptedClient([(1, 2)], [(1, 3), (2, 3)], [(1, 4), (3, 3)], [(1, 1)], [])
        _, _, selection = self._locate(tmp_path, monkeypatch, client, max_selected_pages=10)
        assert selection == PageSelection(((1, 4),))
        assert len(client.prompts) == 4

    def test_omitted_pages_are_never_traversed(self, tmp_path, monkeypatch):
        client = _ScriptedClient([(1, 1)], [(5, 9)], [(10, 10)])
        _, _, selection = self._locate(tmp_path, monkeypatch, client, max_selected_pages=2)
        assert selection == PageSelection(((1, 1),), ((5, 9),))
        assert len(client.prompts) == 2

    def test_later_fitting_part_reclaims_pages_from_omitted_range(self, tmp_path, monkeypatch):
        client = _ScriptedClient([(10, 10)], [(20, 24), (24, 25)], [])
        _, _, selection = self._locate(
            tmp_path, monkeypatch, client, pages=30, max_selected_pages=4
        )
        assert selection == PageSelection(((10, 10), (24, 25)), ((20, 23),))
        assert not set(page_numbers(selection.ranges)) & set(page_numbers(selection.omitted))


class TestStrictRangeAnswers:
    @pytest.mark.parametrize(
        "extra",
        [
            "scalar",
            {"start": 1},
            {"end": 4},
            {"start": True, "end": 2},
            {"start": 1.5, "end": 2},
            {"start": "1", "end": 2},
            {"start": 4, "end": 2},
            {"start": 90, "end": 99},
        ],
    )
    def test_mixed_malformed_answer_is_rejected_whole(self, tmp_path, monkeypatch, extra):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"fixture")
        locator = PageLocator("rates")
        client = _FakeClient([{"start": 4, "end": 4}, extra])
        monkeypatch.setattr(locator, "_ensure_client", lambda: client)
        assert locator.locate(source, ["rates"] * 5) is None
        assert locator.incomplete
        assert not locator._cache_path(source).exists()

    def test_malformed_reference_answer_keeps_only_established_pages(self, tmp_path, monkeypatch):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"fixture")
        locator = PageLocator("rates", context_pages=0)

        class Client(_ScriptedClient):
            def extract(self, **kwargs):
                self.prompts.append(kwargs["user_prompt"])
                if len(self.prompts) == 1:
                    return {"data": {"ranges": [{"start": 2, "end": 2}]}}
                return {"data": {"ranges": [{"start": 4, "end": 4}, {"start": 9}]}}

        client = Client()
        monkeypatch.setattr(locator, "_ensure_client", lambda: client)
        assert locator.locate(source, ["rates"] * 5) == PageSelection(((2, 2),))
        assert locator.incomplete

    def test_valid_empty_answer_is_not_a_failure(self, tmp_path, monkeypatch):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"fixture")
        locator = PageLocator("rates")
        monkeypatch.setattr(locator, "_ensure_client", lambda: _ScriptedClient([]))
        assert locator.locate(source, ["rates"]) is None
        assert not locator.incomplete

    def test_ordered_ranges_are_clamped_and_overlaps_normalized(self, tmp_path, monkeypatch):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"fixture")
        locator = PageLocator("rates")
        client = _ScriptedClient([(0, 2), (2, 3), (4, 99)], [])
        monkeypatch.setattr(locator, "_ensure_client", lambda: client)
        assert locator.locate(source, ["rates"] * 5) == PageSelection(((1, 5),))


class TestUnreadableNativeLoad:
    @pytest.mark.parametrize("loaded", [[], ["", "  \n"]])
    def test_empty_load_is_incomplete_before_the_trigger(self, tmp_path, monkeypatch, loaded):
        source = tmp_path / "scan.pdf"
        source.write_bytes(b"fixture")
        locator = PageLocator("rates", trigger_chars=1000)
        monkeypatch.setattr("psweep.extraction.pdf_utils.extract_pages_text", lambda _p: loaded)
        monkeypatch.setattr(locator, "_ensure_client", lambda: pytest.fail("client built"))
        assert locator.locate(source) is None
        assert locator.incomplete
        assert not locator._cache_path(source).exists()

    def test_explicit_unusable_pages_are_incomplete(self, tmp_path):
        source = tmp_path / "scan.pdf"
        source.write_bytes(b"fixture")
        locator = PageLocator("rates")
        assert locator.locate(source, ["", " "]) is None
        assert locator.incomplete

    def test_blank_pages_among_readable_pages_keep_numbering(self, tmp_path, monkeypatch):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"fixture")
        locator = PageLocator("rates", context_pages=0)
        monkeypatch.setattr(locator, "_ensure_client", lambda: _ScriptedClient([(3, 3)], []))
        assert locator.locate(source, ["", "", "rates", ""]) == PageSelection(((3, 3),))

    def test_small_readable_load_needs_no_client(self, tmp_path, monkeypatch):
        source = tmp_path / "small.pdf"
        source.write_bytes(b"fixture")
        locator = PageLocator("rates", trigger_chars=1000)
        monkeypatch.setattr("psweep.extraction.pdf_utils.extract_pages_text", lambda _p: ["rates"])
        monkeypatch.setattr(locator, "_ensure_client", lambda: pytest.fail("client built"))
        assert locator.locate(source) is None
        assert not locator.incomplete


class TestWorkerLocalState:
    def test_incomplete_does_not_leak_to_next_document(self, tmp_path, monkeypatch):
        bad, good = tmp_path / "bad.pdf", tmp_path / "good.pdf"
        bad.write_bytes(b"bad")
        good.write_bytes(b"good")
        locator = PageLocator("rates", context_pages=0)
        monkeypatch.setattr(
            locator, "_ensure_client", lambda: _ScriptedClient(RuntimeError("down"))
        )
        assert locator.locate(bad, ["rates"]) is None and locator.incomplete
        monkeypatch.setattr(locator, "_ensure_client", lambda: _ScriptedClient([(1, 1)], []))
        locator.set_source_context(None)
        assert locator.locate(good, ["rates"]) == PageSelection(((1, 1),))
        assert not locator.incomplete
