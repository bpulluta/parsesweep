"""Tests for PDF text extraction helpers (pdf_utils)."""

from __future__ import annotations

from collections import UserList
from types import SimpleNamespace

import pytest

from psweep.extraction import pdf_utils


pymupdf = pytest.importorskip("pymupdf")


def _make_digital_pdf(path, text: str) -> None:
    """Write a small text-based (non-scanned) PDF containing ``text``."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def test_digital_pdf_text_not_mangled_by_ocr_corrections(tmp_path):
    """OCR-artifact corrections must not touch normally-extracted text.

    Regression: `_cleanup_ocr_errors` used to run on every PDF, corrupting
    words like "Oil" -> "Cil" and "Oakmont" -> "Cakmont" via its
    capital-O-to-C rule. It now runs only on OCR-derived text.
    """
    pdf = tmp_path / "digital.pdf"
    _make_digital_pdf(pdf, "Oil and gas facilities in Oakmont Borough")

    text, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)

    assert meta["used_ocr"] is False
    assert "Oil and gas" in text
    assert "Oakmont" in text
    assert "Cil" not in text
    assert "Cakmont" not in text


def test_cleanup_ocr_errors_noop_without_rules():
    """No configured rules => no-op (corrections are opt-in per domain)."""
    assert pdf_utils._cleanup_ocr_errors("Ibs per acre") == "Ibs per acre"
    assert pdf_utils._cleanup_ocr_errors("Ibs per acre", []) == "Ibs per acre"


def test_cleanup_ocr_errors_applies_configured_rules_in_order():
    """Configured rules apply in order, honoring ignore_case."""
    rules = [
        {"pattern": r"\bIbs\b", "replacement": "lbs"},
        {"pattern": "emissionsfrom", "replacement": "emissions from", "ignore_case": True},
    ]
    out = pdf_utils._cleanup_ocr_errors("Ibs Emissionsfrom stack", rules)
    assert "lbs" in out
    assert "emissions from" in out.lower()


class _Page:
    """Fake page; ``raster`` is the fraction of the page covered by one image."""

    rect = SimpleNamespace(get_area=lambda: 100.0)

    def __init__(self, native="", ocr="", raster=0.0):
        self.native, self.ocr, self.raster = native, ocr, raster

    def get_text(self, **kwargs):
        return self.ocr if kwargs else self.native

    def get_textpage_ocr(self):
        return object()

    def get_image_info(self):
        return [{"bbox": (0, 0, 10, 10 * self.raster)}] if self.raster else []

    def get_drawings(self):
        return []


class _Doc(UserList):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_native_pdf_disjoint_selection_marks_omitted_pages(tmp_path, monkeypatch):
    """Disjoint ranges extract only selected pages and mark the gap between them."""
    pdf = tmp_path / "native.pdf"
    pdf.write_bytes(b"fake")
    doc = _Doc([_Page(native=f"PAGE{i} " * 150) for i in range(1, 6)])
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", True)
    monkeypatch.setattr(pdf_utils.pymupdf, "open", lambda _p: doc)
    text, meta = pdf_utils.extract_text_from_pdf(pdf, ((1, 1), (4, 9)), return_meta=True)
    assert "PAGE1" in text and "PAGE4" in text and "PAGE5" in text
    assert "PAGE2" not in text and "PAGE3" not in text
    assert text.index("PAGE1") < text.index("[Omitted pages: 2-3]") < text.index("PAGE4")
    assert meta["page_ranges"] == ((1, 1), (4, 5))
    assert meta["selected_pages"] == meta["extracted_pages"] == [1, 4, 5]


def test_ocr_skipped_pages_are_actual_not_requested_coverage(tmp_path, monkeypatch):
    """Coverage omits failed scan pages instead of claiming extraction."""

    class BadPage(_Page):
        def get_textpage_ocr(self):
            raise RuntimeError("damaged scan")

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"fake")
    doc = _Doc(
        [_Page(ocr="first " * 200, raster=1), BadPage(raster=1), _Page(ocr="last " * 200, raster=1)]
    )
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", True)
    monkeypatch.setattr(pdf_utils.pymupdf, "open", lambda _p: doc)
    monkeypatch.setattr(pdf_utils, "PYPDF_AVAILABLE", False)
    text, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert text.index("first") < text.index("last") and "[Omitted" not in text
    assert _status(meta) == {1: "ocr", 2: "unreadable", 3: "ocr"}
    assert meta["extracted_pages"] == [1, 3]
    assert meta["selected_pages"] == [1, 2, 3]
    assert meta["page_count"] == 3


def test_pypdf_fallback_honors_range_and_reports_clamped_scope(tmp_path, monkeypatch):
    """The pypdf fallback preserves selection and reports clamped bounds."""
    pdf = tmp_path / "selected.pdf"
    pdf.write_bytes(b"fake")
    pages = [SimpleNamespace(extract_text=lambda i=i: f"PAGE{i} " * 150) for i in range(1, 4)]
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", False)
    monkeypatch.setattr(pdf_utils, "PYPDF_AVAILABLE", True)
    monkeypatch.setattr(pdf_utils, "PdfReader", lambda _f: SimpleNamespace(pages=pages))
    text, meta = pdf_utils.extract_text_from_pdf(pdf, ((2, 9),), return_meta=True)
    assert "PAGE1" not in text and "PAGE2" in text and "PAGE3" in text
    assert meta["page_ranges"] == ((2, 3),)
    assert meta["page_count"] == 3
    assert meta["used_ocr"] is False


def test_ocr_honors_range_and_returns_raw_text_metadata(tmp_path, monkeypatch):
    """OCR returns only selected pages and retains raw pre-correction text."""
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"fake")
    doc = _Doc([_Page(ocr=f"PAGE{i} RAW " * 100, raster=1) for i in range(1, 4)])
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", True)
    monkeypatch.setattr(pdf_utils.pymupdf, "open", lambda _p: doc)
    monkeypatch.setattr(pdf_utils, "PYPDF_AVAILABLE", False)
    text, meta = pdf_utils.extract_text_from_pdf(
        pdf,
        ((2, 2),),
        return_meta=True,
        ocr_corrections=[{"pattern": "RAW", "replacement": "fixed"}],
    )
    assert "PAGE1" not in text and "PAGE3" not in text
    assert "PAGE2 fixed" in text
    assert meta["page_ranges"] == ((2, 2),)
    assert meta["used_ocr"] is True
    assert "PAGE2 RAW" in meta["raw_text"]
    assert meta["ocr_spans"] == [[0, len(meta["raw_text"]) - 1]]


@pytest.mark.parametrize(
    "page_ranges",
    [
        ((8, 9),),
        ((3, 2),),
        ((0, 0),),
        ((-2, -1),),
        ((1.5, 2),),
        ((True, 2),),
        ((1, 1), (8, 9)),
        (),
        (2, 3),
    ],
)
def test_invalid_pdf_scope_rejected(tmp_path, monkeypatch, page_ranges):
    """Invalid requests cannot silently fall back to a different PDF scope."""
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"fake")
    monkeypatch.setattr(pdf_utils.pymupdf, "open", lambda _p: _Doc([_Page("native")] * 3))
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", True)
    with pytest.raises(ValueError, match="page"):
        pdf_utils.extract_text_from_pdf(pdf, page_ranges, return_meta=True)


def test_parser_failure_preserves_metadata_return_shape(tmp_path, monkeypatch):
    """Failed parsers keep return_meta's text/metadata pair contract."""
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"fake")
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", False)
    monkeypatch.setattr(pdf_utils, "PYPDF_AVAILABLE", True)

    def fail(f):
        raise OSError("invalid PDF")

    monkeypatch.setattr(pdf_utils, "PdfReader", fail)
    result = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert isinstance(result, tuple)
    text, meta = result
    assert not text and meta["used_ocr"] is False
    assert meta["page_ranges"] is None


def test_pypdf_fallback_runs_only_when_pymupdf_represents_no_page(tmp_path, monkeypatch):
    """A broken PyMuPDF still falls back to pypdf; unreadable-by-PyMuPDF pages stay unreadable."""
    pdf = tmp_path / "fallback.pdf"
    pdf.write_bytes(b"fake")
    pages = [SimpleNamespace(extract_text=lambda i=i: f"PAGE{i} " * 150) for i in range(1, 4)]
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", True)
    monkeypatch.setattr(
        pdf_utils.pymupdf, "open", lambda _p: (_ for _ in ()).throw(RuntimeError("bad xref"))
    )
    monkeypatch.setattr(pdf_utils, "PYPDF_AVAILABLE", True)
    monkeypatch.setattr(pdf_utils, "PdfReader", lambda _f: SimpleNamespace(pages=pages))
    text, meta = pdf_utils.extract_text_from_pdf(pdf, ((2, 2),), return_meta=True)
    assert "PAGE2" in text and "PAGE1" not in text and "PAGE3" not in text
    assert meta["extracted_pages"] == [2]


# -- per-page representation (real synthetic PDFs; OCR is faked, never run) ----------

_PROSE = "The utility serves residential customers under the filed schedule. " * 12


def _pix(width=120, height=160):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pix.set_rect(pix.irect, (200, 200, 200))
    return pix


def _build(path, pages):
    """Write a PDF; each page spec is a callable receiving the new page."""
    doc = pymupdf.open()
    for spec in pages:
        spec(doc.new_page())
    doc.save(str(path))
    doc.close()
    return path


def _text(content, size=11):
    return lambda page: page.insert_textbox(pymupdf.Rect(36, 36, 560, 780), content, fontsize=size)


def _raster(page):
    page.insert_image(page.rect, pixmap=_pix())


def _blank(page):
    del page


@pytest.fixture
def ocr_calls(monkeypatch):
    """Replace OCR with a recorder returning a rate row for every raster page."""
    calls = []

    def fake(page):
        calls.append(page.number + 1)
        return "Rate table: Energy charge $0.1234 per kWh"

    monkeypatch.setattr(pdf_utils, "_ocr_page", fake)
    return calls


def _status(meta):
    return {rep["page"]: rep["status"] for rep in meta["pages"]}


def test_raster_rate_page_is_not_hidden_by_a_readable_cover(tmp_path, ocr_calls):
    pdf = _build(tmp_path / "mixed.pdf", [_text(_PROSE), _raster])
    text, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert ocr_calls == [2]
    assert "$0.1234" in text and "filed schedule" in text
    assert _status(meta) == {1: "native", 2: "ocr"}
    assert meta["extracted_pages"] == [1, 2] and meta["used_ocr"] is True


def test_failed_ocr_leaves_the_raster_page_unreadable_not_extracted(tmp_path, monkeypatch):
    def fail(page):
        raise RuntimeError("no tesseract")

    monkeypatch.setattr(pdf_utils, "_ocr_page", fail)
    pdf = _build(tmp_path / "scan.pdf", [_text(_PROSE), _raster])
    _, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert _status(meta) == {1: "native", 2: "unreadable"}
    assert meta["selected_pages"] == [1, 2] and meta["extracted_pages"] == [1]
    assert meta["used_ocr"] is False


def test_blank_page_is_covered_and_never_ocred(tmp_path, ocr_calls):
    pdf = _build(tmp_path / "blank.pdf", [_text(_PROSE), _blank, _text(_PROSE)])
    _, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert ocr_calls == []
    assert _status(meta) == {1: "native", 2: "blank", 3: "native"}
    assert meta["extracted_pages"] == [1, 2, 3]


def test_logo_on_a_text_page_and_tiny_footnote_stay_native(tmp_path, ocr_calls):
    def logo(page):
        _text(_PROSE)(page)
        page.insert_image(pymupdf.Rect(500, 20, 540, 50), pixmap=_pix(40, 30))

    pdf = _build(tmp_path / "plain.pdf", [logo, _text("Rider 4 applies.", size=5)])
    _, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert ocr_calls == []
    assert _status(meta) == {1: "native", 2: "native"}
    assert meta["used_ocr"] is False


def test_logo_only_page_is_decoration_not_unreadable(tmp_path, ocr_calls):
    pdf = _build(
        tmp_path / "logo.pdf",
        [lambda page: page.insert_image(pymupdf.Rect(20, 20, 60, 50), pixmap=_pix(40, 30))],
    )
    _, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert ocr_calls == [] and _status(meta) == {1: "blank"}


def test_page_with_an_ocr_text_layer_is_native(tmp_path, ocr_calls):
    def overlay(page):
        _raster(page)
        _text(_PROSE)(page)

    pdf = _build(tmp_path / "searchable.pdf", [overlay])
    _, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert ocr_calls == [] and _status(meta) == {1: "native"}


def test_heading_over_a_table_image_still_needs_ocr(tmp_path, ocr_calls):
    def heading_and_table(page):
        _raster(page)
        page.insert_text((72, 40), "Schedule R-1 rates")

    pdf = _build(tmp_path / "table.pdf", [heading_and_table])
    text, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert ocr_calls == [1] and _status(meta) == {1: "ocr"} and "$0.1234" in text


def test_rotated_raster_page_is_detected(tmp_path, ocr_calls):
    def rotated(page):
        _raster(page)
        page.set_rotation(90)

    pdf = _build(tmp_path / "rotated.pdf", [rotated])
    pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert ocr_calls == [1]


def test_only_selected_raster_pages_are_ocred_with_original_numbers(tmp_path, ocr_calls):
    pdf = _build(tmp_path / "scope.pdf", [_raster, _text(_PROSE), _raster, _raster])
    text, meta = pdf_utils.extract_text_from_pdf(pdf, ((2, 3),), return_meta=True)
    assert ocr_calls == [3]
    assert meta["selected_pages"] == [2, 3]
    assert [rep["page"] for rep in meta["pages"]] == [2, 3]
    assert _status(meta) == {2: "native", 3: "ocr"}
    assert "[Omitted" not in text


def test_corrections_touch_only_ocr_pages(tmp_path, ocr_calls):
    pdf = _build(tmp_path / "corr.pdf", [_text("Oil schedule. " * 40), _raster])
    rules = [{"pattern": "Oil", "replacement": "XX"}, {"pattern": "Energy", "replacement": "E"}]
    text, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True, ocr_corrections=rules)
    assert "Oil schedule" in text and "XX schedule" not in text
    assert "Rate table: E charge" in text
    assert "Energy charge" in meta["raw_text"]
    assert len(meta["ocr_spans"]) == 1


def test_table_text_page_is_native(tmp_path, ocr_calls):
    rows = "\n".join(f"Tier {n}   {n * 100}-{n * 100 + 99} kWh   ${n}.25" for n in range(1, 30))
    pdf = _build(tmp_path / "rows.pdf", [_text(rows)])
    _, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert ocr_calls == [] and _status(meta) == {1: "native"}


def test_locator_index_includes_ocr_only_when_asked(tmp_path, ocr_calls):
    pdf = _build(tmp_path / "idx.pdf", [_text(_PROSE), _raster])
    assert "$0.1234" not in "".join(pdf_utils.extract_pages_text(pdf))
    assert ocr_calls == []
    pages = pdf_utils.extract_pages_text(pdf, ocr=True)
    assert len(pages) == 2 and "$0.1234" in pages[1] and ocr_calls == [2]
