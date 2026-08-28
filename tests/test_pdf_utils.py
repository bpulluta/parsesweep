"""Tests for PDF text extraction helpers (pdf_utils)."""

from __future__ import annotations

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
