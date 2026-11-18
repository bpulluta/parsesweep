"""Per-page OCR provenance carried by cached and captured text products."""

from __future__ import annotations

import json

from psweep.extraction.document_utils import (
    extract_text_from_document,
    read_text_cache,
    text_cache_paths,
    write_text_cache,
)
from psweep.extraction.prepared_document import capture_source

_RULES = [{"pattern": "Oil", "replacement": "XX"}]


def _mixed_cache(pdf):
    raw = "Oil native page\nOil ocr page\n"
    write_text_cache(pdf, raw, page_count=2, ocr_spans=[(16, 28)])
    return raw


def test_cache_hit_corrects_only_ocr_spans(tmp_path):
    pdf = tmp_path / "mixed.pdf"
    pdf.write_bytes(b"fake")
    _mixed_cache(pdf)
    assert (
        extract_text_from_document(pdf, ocr_corrections=_RULES) == "Oil native page\nXX ocr page\n"
    )
    assert read_text_cache(pdf) == "Oil native page\nOil ocr page\n"


def test_captured_cache_hit_uses_the_same_spans(tmp_path):
    pdf = tmp_path / "mixed.pdf"
    pdf.write_bytes(b"fake")
    _mixed_cache(pdf)
    with capture_source(pdf) as source:
        assert source.cached_text.ocr_spans == ((16, 28),)
        text = extract_text_from_document(pdf, ocr_corrections=_RULES, source=source)
    assert text == "Oil native page\nXX ocr page\n"


def test_ocr_cache_without_page_offsets_is_a_miss(tmp_path):
    pdf = tmp_path / "old.pdf"
    pdf.write_bytes(b"fake")
    write_text_cache(pdf, "raw ocr text", ocr_spans=[(0, 12)])
    _, meta_path = text_cache_paths(pdf)
    meta = json.loads(meta_path.read_text())
    del meta["ocr_spans"]
    meta_path.write_text(json.dumps(meta))
    assert read_text_cache(pdf) is None


def test_ocr_cache_with_out_of_bounds_offsets_is_a_miss(tmp_path):
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(b"fake")
    write_text_cache(pdf, "short", ocr_spans=[(0, 99)])
    assert read_text_cache(pdf) is None
