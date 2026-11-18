"""Captured identity of derived OCR evidence comes from the page cache."""

from __future__ import annotations

from psweep.extraction.document_utils import (
    extract_text_from_document,
    ocr_page_cache,
    read_text_cache,
    write_text_cache,
)
from psweep.extraction.prepared_document import capture_source


def test_old_whole_document_ocr_entry_is_not_an_effective_product(tmp_path):
    pdf = tmp_path / "old.pdf"
    pdf.write_bytes(b"fake")
    write_text_cache(pdf, "raw ocr text", method="ocr")
    assert read_text_cache(pdf) is None
    with capture_source(pdf) as source:
        assert source.cached_text is None
        assert source.ocr_sha256 is None


def test_page_digests_are_the_captured_ocr_identity(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"fake")
    with capture_source(pdf) as source:
        cache = ocr_page_cache(source.original, source.sha256)
        cache.put(3, "page three")
        cache.put(1, "page one")
    with capture_source(pdf) as first:
        pass
    cache.put(1, "page one changed")
    with capture_source(pdf) as second:
        pass
    assert first.ocr_sha256 and second.ocr_sha256 and first.ocr_sha256 != second.ocr_sha256
    assert first.identity == second.identity  # derived OCR is not part of a scope identity


def test_non_pdf_sources_have_no_ocr_identity(tmp_path):
    page = tmp_path / "page.html"
    page.write_bytes(b"<p>A</p>")
    with capture_source(page) as source:
        assert source.ocr_sha256 is None
        assert extract_text_from_document(page, source=source)
