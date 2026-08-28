"""Tests for LLM-assisted section targeting (SectionLocator)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from psweep.extraction.section_locator import (
    SectionLocator,
    _Chunk,
    _pipe_row,
    _table_to_markdown,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

HTML_WITH_TABLE = """\
<html><body>
<h1>Introduction</h1>
<p>Some introductory text about the document.</p>
<h2>Rate Schedule</h2>
<p>The following rates apply:</p>
<table>
<tr><th>Category</th><th>Rate</th><th>Unit</th></tr>
<tr><td>Residential</td><td>100</td><td>$/month</td></tr>
<tr><td>Commercial</td><td>200</td><td>$/month</td></tr>
</table>
<h2>Definitions</h2>
<p>Definitions of terms used in this document.</p>
</body></html>
"""

HTML_SMALL = "<html><body><h1>Hi</h1><p>Short.</p></body></html>"

TXT_WITH_SECTIONS = """\
Some preamble text here.

Section 1 General Provisions
The general provisions apply to all facilities.
Noise limits shall not exceed 55 dBA.

Section 2 Setback Requirements
Minimum setback from residential zones is 500 feet.
Setback from schools is 1000 feet.

Section 3 Permit Requirements
All facilities require a conditional use permit.
"""

TXT_SMALL = "Just a short document.\nNo section markers."


class _FakeClient:
    """Stand-in LLM client that returns canned section lists and counts calls."""

    def __init__(self, relevant_sections: list[int]):
        self._sections = relevant_sections
        self.calls = 0

    def extract(self, **_kwargs):
        self.calls += 1
        return {"data": {"relevant_sections": self._sections}}


# ---------------------------------------------------------------------------
# HTML splitting
# ---------------------------------------------------------------------------


class TestHtmlSplitPreservesTables:
    def test_table_appears_as_markdown_pipes(self):
        locator = SectionLocator("rate schedule")
        chunks = locator._split_html_sections(HTML_WITH_TABLE)
        all_text = "\n".join(c.text for c in chunks)
        # Markdown pipe format should appear
        assert "|" in all_text

    def test_table_cell_values_preserved(self):
        locator = SectionLocator("rate schedule")
        chunks = locator._split_html_sections(HTML_WITH_TABLE)
        all_text = "\n".join(c.text for c in chunks)
        assert "Residential" in all_text
        assert "100" in all_text

    def test_sections_split_by_headings(self):
        locator = SectionLocator("rate schedule")
        chunks = locator._split_html_sections(HTML_WITH_TABLE)
        headings = [c.heading for c in chunks if c.heading]
        assert any("Rate" in h or "rate" in h.lower() for h in headings)
        assert any("Definition" in h or "definition" in h.lower() for h in headings)

    def test_script_tags_excluded(self):
        html = "<html><body><script>alert('x')</script><h1>Topic</h1><p>Content.</p></body></html>"
        locator = SectionLocator("topic")
        chunks = locator._split_html_sections(html)
        all_text = "\n".join(c.text for c in chunks)
        assert "alert" not in all_text


# ---------------------------------------------------------------------------
# TXT splitting
# ---------------------------------------------------------------------------


class TestTxtSplit:
    def test_sections_split_by_markers(self):
        locator = SectionLocator("setback requirements")
        chunks = locator._split_txt_sections(TXT_WITH_SECTIONS)
        headings = [c.heading for c in chunks]
        assert any("Section 2" in h or "Setback" in h for h in headings)

    def test_section_content_included(self):
        locator = SectionLocator("setback requirements")
        chunks = locator._split_txt_sections(TXT_WITH_SECTIONS)
        all_text = "\n".join(c.text for c in chunks)
        assert "500 feet" in all_text

    def test_fallback_to_paragraph_chunks_when_no_markers(self):
        long_txt = ("Word content here.\n\n" * 50).strip()
        locator = SectionLocator("some section")
        chunks = locator._split_txt_sections(long_txt)
        # Should produce at least one chunk
        assert len(chunks) >= 1


# ---------------------------------------------------------------------------
# XLSX / CSV → always None
# ---------------------------------------------------------------------------


class TestXlsxCsvReturnNone:
    def test_xlsx_returns_none(self, tmp_path):
        xlsx = tmp_path / "data.xlsx"
        xlsx.write_bytes(b"PK fake xlsx")
        assert SectionLocator("any section").locate(xlsx) is None

    def test_csv_returns_none(self, tmp_path):
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("col1,col2\nval1,val2")
        assert SectionLocator("any section").locate(csv_file) is None

    def test_pdf_returns_none(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        assert SectionLocator("any section").locate(pdf) is None


# ---------------------------------------------------------------------------
# trigger_chars skips small docs (no LLM call)
# ---------------------------------------------------------------------------


class TestTriggerCharsSkipsSmallDoc:
    def test_small_html_skipped(self, tmp_path, monkeypatch):
        small_html = tmp_path / "small.html"
        small_html.write_text(HTML_SMALL)
        called = []
        locator = SectionLocator("rate schedule", trigger_chars=1_000_000)
        monkeypatch.setattr(locator, "_ensure_client", lambda: called.append(1) or _FakeClient([1]))
        result = locator.locate(small_html)
        assert result is None
        assert called == []  # LLM never invoked

    def test_small_txt_skipped(self, tmp_path, monkeypatch):
        small_txt = tmp_path / "small.txt"
        small_txt.write_text(TXT_SMALL)
        called = []
        locator = SectionLocator("rate schedule", trigger_chars=1_000_000)
        monkeypatch.setattr(locator, "_ensure_client", lambda: called.append(1) or _FakeClient([1]))
        result = locator.locate(small_txt)
        assert result is None
        assert called == []


# ---------------------------------------------------------------------------
# Cache round-trip
# ---------------------------------------------------------------------------


class TestCacheRoundTrip:
    def _html_file(self, tmp_path: Path) -> Path:
        f = tmp_path / "doc.html"
        f.write_text(HTML_WITH_TABLE)
        return f

    def test_write_and_read_back(self, tmp_path):
        html = self._html_file(tmp_path)
        locator = SectionLocator("rate schedule")
        expected = "Rate Schedule\nThe following rates apply."
        locator._write_cache(html, expected)
        assert locator._read_cache(html) == expected

    def test_cache_miss_when_description_changes(self, tmp_path):
        html = self._html_file(tmp_path)
        locator = SectionLocator("rate schedule")
        locator._write_cache(html, "cached text")
        locator2 = SectionLocator("completely different description")
        assert locator2._read_cache(html) is None

    def test_cache_miss_when_file_modified(self, tmp_path):
        html = self._html_file(tmp_path)
        locator = SectionLocator("rate schedule")
        locator._write_cache(html, "cached text")
        # Overwrite file to change mtime/size
        html.write_text(HTML_WITH_TABLE + "<!-- extra -->")
        assert locator._read_cache(html) is None

    def test_cache_hit_skips_llm(self, tmp_path, monkeypatch):
        html = self._html_file(tmp_path)
        locator = SectionLocator("rate schedule", trigger_chars=10)
        fake = _FakeClient([2])
        monkeypatch.setattr(locator, "_ensure_client", lambda: fake)
        # Seed cache directly
        locator._write_cache(html, "cached content")
        result = locator._read_cache(html)
        assert result == "cached content"
        assert fake.calls == 0


# ---------------------------------------------------------------------------
# LLM failure → None, no ERROR log
# ---------------------------------------------------------------------------


class TestLlmFailureReturnsNoneNoErrorLog:
    def test_llm_failure_no_error_log(self, tmp_path, monkeypatch, caplog):
        html = tmp_path / "big.html"
        # Write a large-enough file so trigger_chars doesn't skip it
        html.write_text(HTML_WITH_TABLE * 100)

        class _FailClient:
            def extract(self, **_kwargs):
                raise RuntimeError("simulated LLM failure")

        locator = SectionLocator("rate schedule", trigger_chars=100)
        monkeypatch.setattr(locator, "_ensure_client", lambda: _FailClient())

        with caplog.at_level(logging.ERROR):
            result = locator.locate(html)

        assert result is None
        # No ERROR-level records allowed — failures must be DEBUG only
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == []

    def test_llm_returns_empty_sections_gives_none(self, tmp_path, monkeypatch):
        html = tmp_path / "big.html"
        html.write_text(HTML_WITH_TABLE * 100)

        locator = SectionLocator("rate schedule", trigger_chars=100)
        monkeypatch.setattr(locator, "_ensure_client", lambda: _FakeClient([]))
        result = locator.locate(html)
        assert result is None


# ---------------------------------------------------------------------------
# Keyword scoring
# ---------------------------------------------------------------------------


class TestHeuristicScoring:
    def test_keyword_dense_chunk_is_candidate(self):
        locator = SectionLocator("rate schedule per kwh")
        dense = _Chunk("Rate Schedule", "rate schedule energy charge per kwh monthly " * 5)
        sparse = _Chunk("Definitions", "general terms and conditions apply")
        candidates = locator._candidate_chunks([sparse, dense])
        assert 1 in candidates  # dense chunk is index 1

    def test_no_candidates_when_no_keyword_hits(self):
        locator = SectionLocator("rate schedule per kwh")
        chunks = [_Chunk("General", "nothing relevant here") for _ in range(5)]
        assert locator._candidate_chunks(chunks) == []


# ---------------------------------------------------------------------------
# table_to_markdown helper
# ---------------------------------------------------------------------------


class TestTableToMarkdown:
    def test_basic_table(self):
        from bs4 import BeautifulSoup

        html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        md = _table_to_markdown(table)
        assert "| A | B |" in md
        assert "| 1 | 2 |" in md


# ---------------------------------------------------------------------------
# Serializer output locks (guard the table -> markdown consolidation)
#
# section_locator has two table serializers that share the bordered `| a | b |`
# row format via `_pipe_row` but differ in row filtering:
#   - `_table_to_markdown` (bs4)   : keeps every row that has cells, incl. all-blank
#   - `_table_md` (docx, nested)   : drops rows whose cells are ALL blank
# These tests pin the exact byte output of each so the shared helper can never
# silently change either format.
# ---------------------------------------------------------------------------


class TestPipeRowHelper:
    def test_bordered_format(self):
        assert _pipe_row(["a", "b"]) == "| a | b |"

    def test_empty_cells_preserved_as_blank_columns(self):
        assert _pipe_row(["", ""]) == "|  |  |"

    def test_single_cell(self):
        assert _pipe_row(["x"]) == "| x |"


class TestTableToMarkdownExactOutput:
    """Lock bs4 serializer (#1): keeps empty cells AND all-blank rows."""

    def test_exact_output_keeps_empty_cells_and_blank_rows(self):
        from bs4 import BeautifulSoup

        html = (
            "<table>"
            "<tr><th>A</th><th>B</th></tr>"
            "<tr><td>1</td><td></td></tr>"
            "<tr><td></td><td></td></tr>"
            "</table>"
        )
        table = BeautifulSoup(html, "html.parser").find("table")
        assert _table_to_markdown(table) == "| A | B |\n| 1 |  |\n|  |  |"

    def test_row_without_cells_is_skipped(self):
        from bs4 import BeautifulSoup

        html = "<table><tr></tr><tr><td>x</td></tr></table>"
        table = BeautifulSoup(html, "html.parser").find("table")
        assert _table_to_markdown(table) == "| x |"


class TestDocxTableSerializerExactOutput:
    """Lock docx serializer (#2, `_table_md` via `_split_docx_sections`).

    Bordered format like the bs4 path, but rows whose cells are ALL blank are
    dropped (``any(cells)``).
    """

    def _docx_with_table(self, tmp_path: Path) -> Path:
        import docx

        d = docx.Document()
        d.add_paragraph("Rate Schedule", style="Heading 1")
        tbl = d.add_table(rows=3, cols=2)
        tbl.rows[0].cells[0].text = "A"
        tbl.rows[0].cells[1].text = "B"
        tbl.rows[1].cells[0].text = "1"
        tbl.rows[1].cells[1].text = ""  # one blank cell -> preserved
        # row 2 left entirely blank -> whole row dropped
        path = tmp_path / "table.docx"
        d.save(path)
        return path

    def test_exact_output_drops_all_blank_row_keeps_empty_cell(self, tmp_path):
        path = self._docx_with_table(tmp_path)
        chunks = SectionLocator("rate schedule")._split_docx_sections(path)
        table_chunks = [c for c in chunks if "|" in c.text]
        assert len(table_chunks) == 1
        assert table_chunks[0].text == "| A | B |\n| 1 |  |"
