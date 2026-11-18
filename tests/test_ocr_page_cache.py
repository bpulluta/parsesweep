"""One page-keyed raw-OCR cache shared by the locator index and extraction."""

from __future__ import annotations

import json

import pytest

from psweep.extraction import pdf_utils
from psweep.extraction.document_utils import extract_text_from_document, ocr_page_cache
from psweep.extraction.page_locator import PageLocator
from psweep.extraction.prepared_document import capture_source

pymupdf = pytest.importorskip("pymupdf")

_PROSE = "Native prose about the tariff schedule. " * 20


def _build(path, specs):
    doc = pymupdf.open()
    for spec in specs:
        spec(doc.new_page())
    doc.save(str(path))
    doc.close()
    return path


def _text(content):
    return lambda page: page.insert_textbox(pymupdf.Rect(36, 36, 560, 780), content, fontsize=11)


def _raster(page):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 160), False)
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(page.rect, pixmap=pix)


@pytest.fixture
def ocr_calls(monkeypatch):
    calls = []

    def fake(page):
        calls.append(page.number + 1)
        return f"RAW Energy charge page {page.number + 1}"

    monkeypatch.setattr(pdf_utils, "_ocr_page", fake)
    return calls


def _scan(tmp_path, name="scan.pdf"):
    return _build(tmp_path / name, [_text(_PROSE), _raster, _raster])


def _entries(pdf):
    return sorted((pdf.parent / ".text" / f"{pdf.name}.ocr").glob("*.json"))


def test_locator_then_extraction_ocr_a_raster_page_once(tmp_path, ocr_calls, monkeypatch):
    pdf = _scan(tmp_path)
    locator = PageLocator("rates", trigger_chars=0)
    monkeypatch.setattr(locator, "_ensure_client", _Client)
    with capture_source(pdf) as source:
        locator.locate(source)
        assert sorted(ocr_calls) == [2, 3]
        text = extract_text_from_document(pdf, source=source)
    assert sorted(ocr_calls) == [2, 3]
    assert "RAW Energy charge page 2" in text


class _Client:
    def extract(self, **_kwargs):
        return {"data": {"ranges": [{"start": 1, "end": 3}]}}


def test_page_range_run_reuses_pages_from_a_full_run(tmp_path, ocr_calls):
    pdf = _scan(tmp_path)
    with capture_source(pdf) as source:
        extract_text_from_document(pdf, source=source)
    assert sorted(ocr_calls) == [2, 3]
    with capture_source(pdf) as source:
        text = extract_text_from_document(pdf, page_ranges=((3, 3),), source=source)
    assert sorted(ocr_calls) == [2, 3]
    assert "page 3" in text and "page 2" not in text


def test_full_run_reuses_pages_from_a_page_range_run(tmp_path, ocr_calls):
    pdf = _scan(tmp_path)
    with capture_source(pdf) as source:
        extract_text_from_document(pdf, page_ranges=((2, 2),), source=source)
    with capture_source(pdf) as source:
        extract_text_from_document(pdf, source=source)
    assert sorted(ocr_calls) == [2, 3]


def test_a_source_change_misses(tmp_path, ocr_calls):
    pdf = _scan(tmp_path)
    with capture_source(pdf) as source:
        extract_text_from_document(pdf, source=source)
    pdf.write_bytes(pdf.read_bytes() + b"\n% changed")
    with capture_source(pdf) as source:
        extract_text_from_document(pdf, source=source)
    assert sorted(ocr_calls) == [2, 2, 3, 3]


def test_a_corrupt_entry_misses_and_is_rewritten(tmp_path, ocr_calls):
    pdf = _scan(tmp_path)
    with capture_source(pdf) as source:
        extract_text_from_document(pdf, source=source)
    first, _ = _entries(pdf)
    entry = json.loads(first.read_text())
    entry["text"] = "tampered"
    first.write_text(json.dumps(entry))
    with capture_source(pdf) as source:
        text = extract_text_from_document(pdf, source=source)
    assert ocr_calls.count(2) == 2 and ocr_calls.count(3) == 1
    assert "tampered" not in text
    assert json.loads(first.read_text())["text"].startswith("RAW")


def test_unparseable_entry_misses(tmp_path, ocr_calls):
    pdf = _scan(tmp_path)
    with capture_source(pdf) as source:
        extract_text_from_document(pdf, source=source)
    _entries(pdf)[0].write_text("{not json")
    with capture_source(pdf) as source:
        extract_text_from_document(pdf, source=source)
    assert sorted(ocr_calls) == [2, 2, 3]


def test_an_engine_version_change_misses(tmp_path, ocr_calls, monkeypatch):
    pdf = _scan(tmp_path)
    with capture_source(pdf) as source:
        extract_text_from_document(pdf, source=source)
    monkeypatch.setattr(pdf_utils, "OCR_PAGE_VERSION", "ocr-page-next")
    with capture_source(pdf) as source:
        extract_text_from_document(pdf, source=source)
    assert sorted(ocr_calls) == [2, 2, 3, 3]


def test_entries_hold_raw_text_and_corrections_apply_on_read(tmp_path, ocr_calls):
    pdf = _scan(tmp_path)
    rules = [{"pattern": "RAW", "replacement": "FIXED"}]
    with capture_source(pdf) as source:
        first = extract_text_from_document(pdf, source=source, ocr_corrections=rules)
    with capture_source(pdf) as source:
        second = extract_text_from_document(pdf, source=source, ocr_corrections=None)
    assert "FIXED Energy" in first and "RAW Energy" in second
    assert "RAW Energy charge page 2" in _entries(pdf)[0].read_text()
    assert sorted(ocr_calls) == [2, 3]


def test_per_page_write_is_atomic(tmp_path, ocr_calls, monkeypatch):
    pdf = _scan(tmp_path)
    with capture_source(pdf) as source:
        cache = ocr_page_cache(source.original, source.sha256)
    cache.put(2, "old")
    original = type(pdf).replace

    def fail(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(type(pdf), "replace", fail)
    cache.put(2, "new")  # best effort: never raises, never publishes a partial entry
    monkeypatch.setattr(type(pdf), "replace", original)
    assert cache.get(2) == "old"
    assert [path.name for path in _entries(pdf)] == ["2.json"]
    assert list(_entries(pdf)[0].parent.iterdir()) == _entries(pdf)


def test_captured_identity_is_derived_from_the_page_digests(tmp_path, ocr_calls):
    pdf = _scan(tmp_path)
    with capture_source(pdf) as source:
        assert source.ocr_sha256 is None
        _, meta = extract_text_from_document(pdf, source=source, return_meta=True)
    with capture_source(pdf) as source:
        assert source.ocr_sha256 == meta["ocr_sha256"] is not None
    _entries(pdf)[0].unlink()
    with capture_source(pdf) as source:
        assert source.ocr_sha256 not in {None, meta["ocr_sha256"]}


def test_native_pdf_never_creates_a_page_cache(tmp_path, ocr_calls):
    pdf = _build(tmp_path / "native.pdf", [_text(_PROSE)])
    with capture_source(pdf) as source:
        _, meta = extract_text_from_document(pdf, source=source, return_meta=True)
        assert meta["ocr_sha256"] is None
    assert not (tmp_path / ".text").exists()
