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
        {"pattern": "emissionsfrom", "replacement": "emissions from",
         "ignore_case": True},
    ]
    out = pdf_utils._cleanup_ocr_errors("Ibs Emissionsfrom stack", rules)
    assert "lbs" in out
    assert "emissions from" in out.lower()


class _Page:
    def __init__(self, native="", ocr=""):
        self.native, self.ocr = native, ocr

    def get_text(self, **kwargs):
        return self.ocr if kwargs else self.native

    def get_textpage_ocr(self):
        return object()


class _Doc(UserList):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_native_pdf_selected_pages_and_clamped_metadata(tmp_path, monkeypatch):
    """Native text and reported scope honor the identical bounded selection."""
    pdf = tmp_path / "native.pdf"
    pdf.write_bytes(b"fake")
    doc = _Doc([_Page(native=f"PAGE{i} " * 150) for i in range(1, 4)])
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", True)
    monkeypatch.setattr(pdf_utils.pymupdf, "open", lambda _p: doc)
    text, meta = pdf_utils.extract_text_from_pdf(
        pdf, (-2, 2), return_meta=True
    )
    assert "PAGE1" in text and "PAGE2" in text and "PAGE3" not in text
    assert meta["page_range"] == (1, 2)
    assert meta["extracted_pages"] == [1, 2]


def test_ocr_skipped_pages_are_actual_not_requested_coverage(
    tmp_path, monkeypatch
):
    """Coverage omits failed scan pages instead of claiming extraction."""

    class BadPage(_Page):
        def get_textpage_ocr(self):
            raise RuntimeError("damaged scan")

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"fake")
    doc = _Doc(
        [_Page(ocr="first " * 200), BadPage(), _Page(ocr="last " * 200)]
    )
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", True)
    monkeypatch.setattr(pdf_utils.pymupdf, "open", lambda _p: doc)
    monkeypatch.setattr(pdf_utils, "PYPDF_AVAILABLE", False)
    _, meta = pdf_utils.extract_text_from_pdf(pdf, return_meta=True)
    assert meta["extracted_pages"] == [1, 3]
    assert meta["page_count"] == 3


def test_pypdf_fallback_honors_range_and_reports_clamped_scope(
    tmp_path, monkeypatch
):
    """The pypdf fallback preserves selection and reports clamped bounds."""
    pdf = tmp_path / "selected.pdf"
    pdf.write_bytes(b"fake")
    pages = [
        SimpleNamespace(extract_text=lambda i=i: f"PAGE{i} " * 150)
        for i in range(1, 4)
    ]
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", False)
    monkeypatch.setattr(pdf_utils, "PYPDF_AVAILABLE", True)
    monkeypatch.setattr(
        pdf_utils, "PdfReader", lambda _f: SimpleNamespace(pages=pages)
    )
    text, meta = pdf_utils.extract_text_from_pdf(pdf, (2, 9), return_meta=True)
    assert "PAGE1" not in text and "PAGE2" in text and "PAGE3" in text
    assert meta["page_range"] == (2, 3)
    assert meta["page_count"] == 3
    assert meta["used_ocr"] is False


def test_ocr_honors_range_and_returns_raw_text_metadata(tmp_path, monkeypatch):
    """OCR returns only selected pages and retains raw pre-correction text."""
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"fake")
    doc = _Doc([_Page(ocr=f"PAGE{i} RAW " * 100) for i in range(1, 4)])
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", True)
    monkeypatch.setattr(pdf_utils.pymupdf, "open", lambda _p: doc)
    monkeypatch.setattr(pdf_utils, "PYPDF_AVAILABLE", False)
    text, meta = pdf_utils.extract_text_from_pdf(
        pdf,
        (2, 2),
        return_meta=True,
        ocr_corrections=[{"pattern": "RAW", "replacement": "fixed"}],
    )
    assert "PAGE1" not in text and "PAGE3" not in text
    assert "PAGE2 fixed" in text
    assert meta["page_range"] == (2, 2)
    assert meta["used_ocr"] is True
    assert "PAGE2 RAW" in meta["raw_ocr_text"]


@pytest.mark.parametrize(
    "page_range", [(8, 9), (3, 2), (0, 0), (-2, -1), (1.5, 2), (True, 2)]
)
def test_invalid_pdf_scope_rejected(tmp_path, monkeypatch, page_range):
    """Invalid requests cannot silently fall back to a different PDF scope."""
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"fake")
    monkeypatch.setattr(
        pdf_utils.pymupdf, "open", lambda _p: _Doc([_Page("native")] * 3)
    )
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", True)
    with pytest.raises(ValueError, match="page"):
        pdf_utils.extract_text_from_pdf(pdf, page_range, return_meta=True)


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
    assert meta["page_range"] is None


def test_pypdf_failure_still_attempts_scoped_ocr(tmp_path, monkeypatch):
    """A broken pypdf parser must not prevent the scoped OCR fallback."""
    pdf = tmp_path / "fallback.pdf"
    pdf.write_bytes(b"fake")
    doc = _Doc([_Page(ocr=f"PAGE{i} " * 150) for i in range(1, 4)])
    monkeypatch.setattr(pdf_utils, "PYMUPDF_AVAILABLE", True)
    monkeypatch.setattr(pdf_utils.pymupdf, "open", lambda _p: doc)
    monkeypatch.setattr(pdf_utils, "PYPDF_AVAILABLE", True)

    def fail(f):
        raise OSError("pypdf failed")

    monkeypatch.setattr(pdf_utils, "PdfReader", fail)
    text, meta = pdf_utils.extract_text_from_pdf(pdf, (2, 2), return_meta=True)
    assert "PAGE2" in text and "PAGE1" not in text and "PAGE3" not in text
    assert meta["used_ocr"] is True
