"""Tests for LLM-assisted page targeting (PageLocator + process integration)."""

from __future__ import annotations

from pathlib import Path

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

    def test_locate_returns_range_and_writes_cache(self, tmp_path, monkeypatch):
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

    def test_cache_invalidated_when_section_changes(self, tmp_path, monkeypatch):
        pdf = self._pdf(tmp_path)
        pages = _pages_with_section({10, 11}, total=20)
        loc = PageLocator("residential rate schedule per kwh")
        monkeypatch.setattr(
            loc, "_ensure_client", lambda: _FakeClient([{"start": 11, "end": 12}])
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


class TestProcessIntegration:
    def test_apply_page_targeting_respects_manual_and_size(
        self, tmp_path, monkeypatch
    ):
        from psweep.cli import commands

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
        from psweep.cli import commands

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
        from psweep.cli import commands

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
        from psweep.cli import commands

        doc = tmp_path / "d.pdf"
        doc.write_bytes(b"%PDF fake")
        page_range_map: dict = {}
        commands._apply_page_targeting(
            doc_files=[doc],
            page_range_map=page_range_map,
            config={"enabled": True},  # no section_description
        )
        assert page_range_map == {}
