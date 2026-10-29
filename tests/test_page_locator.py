"""Tests for LLM-assisted page targeting (PageLocator + process integration)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from psweep.cli import commands_extract as commands
from psweep.extraction.page_locator import PageLocator


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
                "Residential rate schedule energy charge per kwh monthly "
                "customer charge rate " * 5
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

    def test_locate_returns_range_and_writes_cache(
        self, tmp_path, monkeypatch
    ):
        pdf = self._pdf(tmp_path)
        loc = PageLocator("residential rate schedule per kwh")
        fake = _FakeClient([{"start": 11, "end": 12}])
        monkeypatch.setattr(loc, "_ensure_client", lambda: fake)

        pages = _pages_with_section({10, 11}, total=20)
        rng = loc.locate(pdf, pages=pages)
        assert rng == (11, 12)
        assert fake.calls == 1
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
        rng = loc2.locate(pdf, pages=pages)
        assert rng == (11, 12)
        assert fake2.calls == 0  # served from cache

    def test_cache_invalidated_when_section_changes(
        self, tmp_path, monkeypatch
    ):
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
        rng = loc2.locate(pdf, pages=pages)
        assert fake2.calls == 1
        assert rng == (3, 4)

    def test_range_capped_to_max_selected_pages(self, tmp_path, monkeypatch):
        pdf = self._pdf(tmp_path)
        loc = PageLocator(
            "residential rate schedule per kwh", max_selected_pages=5
        )
        monkeypatch.setattr(
            loc,
            "_ensure_client",
            lambda: _FakeClient([{"start": 10, "end": 100}]),
        )
        pages = _pages_with_section({10, 11, 12}, total=120)
        rng = loc.locate(pdf, pages=pages)
        assert rng == (10, 14)  # 5 pages max

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
        assert locator.locate(source, pages=["rate schedule"] * 5) == (2, 3)
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
    def test_effective_settings_invalidate_cache(
        self, tmp_path, monkeypatch, changed
    ):
        source, _ = self._seed(tmp_path, monkeypatch, model="gpt-4o")
        kwargs = {"model": "gpt-4o", **changed}
        assert (
            PageLocator("rate schedule", **kwargs)._read_cache(source) is None
        )

    @pytest.mark.parametrize(
        "bad_range", [[0, 2], [4, 3], [1, 8], [1, 5], [True, 2], [1.5, 2]]
    )
    def test_cached_bounds_and_cap_validated(
        self, tmp_path, monkeypatch, bad_range
    ):
        source, loc = self._seed(tmp_path, monkeypatch, max_selected_pages=2)
        cache = loc._cache_path(source)
        meta = json.loads(cache.read_text())
        meta["range"] = bad_range
        cache.write_text(json.dumps(meta))
        assert loc._read_cache(source) is None

    def test_cached_lookup_does_not_parse_or_construct_client(
        self, tmp_path, monkeypatch
    ):
        source, loc = self._seed(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "psweep.extraction.pdf_utils.extract_pages_text",
            lambda _p: pytest.fail("parsed cache hit"),
        )
        monkeypatch.setattr(
            loc, "_ensure_client", lambda: pytest.fail("built client")
        )
        assert loc.locate_cached(source) == (2, 3)

    def test_resolved_ambient_model_invalidates_cache(
        self, tmp_path, monkeypatch
    ):
        config = {"model": "gpt-4o"}
        monkeypatch.setattr(
            "psweep.extraction.llm_factory._get_llm_config",
            lambda _cfg: config,
        )
        source, _ = self._seed(tmp_path, monkeypatch)
        config["model"] = "gpt-4o-mini"
        assert PageLocator("rate schedule")._read_cache(source) is None


def test_page_locator_applies_trigger_when_loading_its_own_pages(
    tmp_path, monkeypatch
):
    """Shared workflow targeting does not call the LLM for small PDFs."""
    source = tmp_path / "small.pdf"
    source.write_bytes(b"fake")
    loc = PageLocator("rates", trigger_chars=1000)
    monkeypatch.setattr(
        "psweep.extraction.pdf_utils.extract_pages_text", lambda _p: ["rates"]
    )
    monkeypatch.setattr(
        loc, "_ensure_client", lambda: pytest.fail("small PDF located")
    )
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
    def test_malformed_ranges_never_widen_selection(
        self, tmp_path, monkeypatch, ranges
    ):
        source = tmp_path / "doc.pdf"
        source.write_bytes(b"fake")
        locator = PageLocator("rates")
        monkeypatch.setattr(
            locator, "_ensure_client", lambda: _FakeClient(ranges)
        )
        expected = (4, 4) if len(ranges) > 1 else None
        assert locator.locate(source, pages=["rates"] * 5) == expected


class TestProcessIntegration:
    def test_apply_page_targeting_respects_manual_and_size(
        self, tmp_path, monkeypatch
    ):
        big = tmp_path / "big.pdf"
        big.write_bytes(b"%PDF fake")
        small = tmp_path / "small.pdf"
        small.write_bytes(b"%PDF fake")
        manual = tmp_path / "manual.pdf"
        manual.write_bytes(b"%PDF fake")

        # big.pdf: huge text -> targeting runs; small.pdf: tiny -> skipped.
        page_text = {
            big: ["x" * 300_000],
            small: ["short"],
            manual: ["x" * 300_000],
        }
        monkeypatch.setattr(
            "psweep.extraction.pdf_utils.extract_pages_text",
            lambda p: page_text[Path(p)],
        )
        monkeypatch.setattr(
            "psweep.extraction.page_locator.PageLocator.locate",
            lambda self, doc, pages=None: (5, 9),
        )

        page_range_map = {manual: (1, 3)}  # manual range present
        commands._apply_page_targeting(
            doc_files=[big, small, manual],
            page_range_map=page_range_map,
            config={
                "enabled": True,
                "section_description": "rate schedules",
                "trigger_chars": 200_000,
            },
        )
        assert page_range_map[big] == (5, 9)  # located
        assert small not in page_range_map  # too small, untouched
        assert page_range_map[manual] == (1, 3)  # CSV entry wins

    def test_apply_page_targeting_skips_explicit_full_doc(
        self, tmp_path, monkeypatch
    ):
        """CSV entry with empty pages (explicit full-doc) blocks targeting."""
        doc = tmp_path / "full.pdf"
        doc.write_bytes(b"%PDF fake")

        monkeypatch.setattr(
            "psweep.extraction.pdf_utils.extract_pages_text",
            lambda p: ["x" * 300_000],
        )
        monkeypatch.setattr(
            "psweep.extraction.page_locator.PageLocator.locate",
            lambda self, doc, pages=None: (5, 9),
        )

        # Explicit full-doc entry (None value, but key IS present)
        page_range_map = {doc: None}
        commands._apply_page_targeting(
            doc_files=[doc],
            page_range_map=page_range_map,
            config={
                "enabled": True,
                "section_description": "rate schedules",
                "trigger_chars": 200_000,
            },
        )
        assert page_range_map[doc] is None  # still None, targeting skipped

    def test_apply_page_targeting_writes_discovered_csv(
        self, tmp_path, monkeypatch
    ):
        """Discovered page ranges are written to a CSV for human review."""
        doc = tmp_path / "big.pdf"
        doc.write_bytes(b"%PDF fake")

        monkeypatch.setattr(
            "psweep.extraction.pdf_utils.extract_pages_text",
            lambda p: ["x" * 300_000],
        )
        monkeypatch.setattr(
            "psweep.extraction.page_locator.PageLocator.locate",
            lambda self, doc, pages=None: (10, 20),
        )

        page_range_map: dict = {}
        out_dir = tmp_path / "output"
        out_dir.mkdir()
        commands._apply_page_targeting(
            doc_files=[doc],
            page_range_map=page_range_map,
            config={
                "enabled": True,
                "section_description": "rate schedules",
                "trigger_chars": 200_000,
                "save_discovered": True,
            },
            output_dir=out_dir,
        )
        csv_path = out_dir / "discovered_page_ranges.csv"
        assert csv_path.exists()
        content = csv_path.read_text()
        assert "big.pdf" in content
        assert "10" in content and "20" in content

    def test_apply_page_targeting_noop_without_description(
        self, tmp_path, monkeypatch, capsys
    ):
        doc = tmp_path / "d.pdf"
        doc.write_bytes(b"%PDF fake")
        page_range_map: dict = {}
        commands._apply_page_targeting(
            doc_files=[doc],
            page_range_map=page_range_map,
            config={"enabled": True},  # no section_description
        )
        assert page_range_map == {}


class TestNonPdfGuard:
    def test_locate_on_non_pdf_returns_none(self, tmp_path):
        """PageLocator.locate() on a non-PDF path returns None without raising."""
        html = tmp_path / "doc.html"
        html.write_text("<html><body>irrelevant content</body></html>")
        loc = PageLocator("rate schedule charges")
        # Pass an empty pages list to avoid pymupdf I/O; locate() returns None early.
        result = loc.locate(html, pages=[])
        assert result is None

    def test_apply_page_targeting_skips_non_pdf(self, tmp_path, monkeypatch):
        """Non-PDF files in doc_files are skipped; no range added to map."""
        html = tmp_path / "doc.html"
        html.write_text("<html></html>")

        monkeypatch.setattr(
            "psweep.extraction.page_locator.PageLocator.locate",
            lambda self, doc, pages=None: (1, 5),
        )

        page_range_map: dict = {}
        commands._apply_page_targeting(
            doc_files=[html],
            page_range_map=page_range_map,
            config={"enabled": True, "section_description": "rate schedules"},
        )
        assert html not in page_range_map

    def test_llm_failure_in_page_locator_emits_no_error_log(
        self, tmp_path, monkeypatch, caplog
    ):
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
        monkeypatch.setattr(loc, "_ensure_client", lambda: _FailClient())

        pages = _pages_with_section({2, 3}, total=10)
        with caplog.at_level(logging.DEBUG):
            result = loc.locate(pdf, pages=pages)

        assert result is None
        error_records = [
            r for r in caplog.records if r.levelno >= logging.ERROR
        ]
        assert not error_records, (
            f"Unexpected ERROR logs: {[r.message for r in error_records]}"
        )
