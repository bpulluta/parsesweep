"""Synthetic evidence and coverage tests; no source-document fixtures required."""

import hashlib
import json
from pathlib import Path

import pytest

from psweep.extraction.pdf_inspection import INSPECTOR_VERSION, inspect_pdf

pymupdf = pytest.importorskip("pymupdf")


def _text_page(document, text="The existing requirement remains readable.", *, y=100):
    page = document.new_page()
    page.insert_text((72, y), text, fontsize=12)
    return page, page.search_for(text)[0]


def _save(document, tmp_path, **kwargs):
    path = tmp_path / "source.pdf"
    document.save(path, **kwargs)
    document.close()
    return path


def _crossing(page, rect, *, rectangle=False, color=(0, 0, 0), fraction=0.59):
    y = rect.y0 + fraction * rect.height
    if rectangle:
        page.draw_rect(
            pymupdf.Rect(rect.x0, y - 0.24, rect.x1, y + 0.24),
            color=None,
            fill=color,
            width=0,
        )
    else:
        page.draw_line((rect.x0, y), (rect.x1, y), color=color, width=0.48)


def test_native_pdf_contract_and_determinism(tmp_path):
    document = pymupdf.open()
    _text_page(document)
    path = _save(document, tmp_path)

    result = inspect_pdf(path)

    assert result == inspect_pdf(path)
    assert result == {
        "state": "no_findings",
        "inspector_version": INSPECTOR_VERSION,
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "page_count": 1,
        "inspected_pages": 1,
        "evidence": [],
        "evidence_count": 0,
        "limitations": [],
    }
    json.dumps(result, allow_nan=False)


def test_strikeout_annotation_flags_retained_text(tmp_path):
    document = pymupdf.open()
    page, rect = _text_page(document)
    page.add_strikeout_annot(rect)
    path = _save(document, tmp_path)

    result = inspect_pdf(path)

    assert result["state"] == "flagged"
    assert result["evidence_count"] == 1
    assert result["evidence"][0]["kind"] == "strikeout_annotation"
    assert result["evidence"][0]["strength"] == "strong"
    assert result["evidence"][0]["page"] == 1
    with pymupdf.open(path) as reopened:
        assert "remains readable" in reopened[0].get_text()


def test_highlight_is_weak_not_a_flag(tmp_path):
    document = pymupdf.open()
    page, rect = _text_page(document)
    page.add_highlight_annot(rect)
    result = inspect_pdf(_save(document, tmp_path))

    assert result["state"] == "no_findings"
    assert result["evidence_count"] == 1
    assert result["evidence"][0]["kind"] == "highlight_annotation"
    assert result["evidence"][0]["strength"] == "weak"


@pytest.mark.parametrize("rectangle", [False, True])
@pytest.mark.parametrize("color", [(0, 0, 0), (1, 0, 0)])
def test_flattened_strokes_and_thin_filled_rectangles(tmp_path, rectangle, color):
    document = pymupdf.open()
    page, _ = _text_page(document)
    rect = page.search_for("existing requirement")[0]
    _crossing(page, rect, rectangle=rectangle, color=color)
    path = _save(document, tmp_path)

    result = inspect_pdf(path)

    assert result["state"] == "flagged"
    assert result["evidence_count"] == 1
    expected_kind = "text_crossing_rectangle" if rectangle else "text_crossing_stroke"
    assert result["evidence"][0]["kind"] == expected_kind
    assert "existing requirement" in result["evidence"][0]["text"]
    assert len(result["evidence"][0]["bbox"]) == 4
    with pymupdf.open(path) as reopened:
        assert "existing requirement" in reopened[0].get_text()


@pytest.mark.parametrize("fraction", [0.78, 0.9, 1.0, 0.05])
@pytest.mark.parametrize("rectangle", [False, True])
def test_underlines_and_overlines_do_not_flag(tmp_path, fraction, rectangle):
    document = pymupdf.open()
    page, rect = _text_page(document)
    _crossing(page, rect, fraction=fraction, rectangle=rectangle)
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "no_findings"
    assert result["evidence_count"] == 0


def test_color_and_normal_table_borders_do_not_flag(tmp_path):
    document = pymupdf.open()
    page, rect = _text_page(document)
    page.insert_text((72, 200), "Colored final text", color=(1, 0, 0))
    page.draw_rect(pymupdf.Rect(rect.x0 - 10, rect.y0 - 10, rect.x1 + 10, rect.y1 + 10), width=0.5)
    page.draw_line((40, 140), (555, 140), width=0.5)
    page.draw_line((40, 40), (40, 300), width=0.5)
    page.draw_line((555, 40), (555, 300), width=0.5)
    page.add_underline_annot(rect)
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "no_findings"
    assert result["evidence_count"] == 0


def test_separately_drawn_table_grid_crossing_text_is_not_a_strike(tmp_path):
    document = pymupdf.open()
    page, rect = _text_page(document)
    # Even a poorly positioned grid rule is structural when joined to vertical edges.
    _crossing(page, rect)
    for x in (rect.x0, rect.x1):
        page.draw_line((x, rect.y0 - 20), (x, rect.y1 + 20), width=0.48)
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "no_findings"


def test_wide_rule_and_diagonal_line_do_not_flag(tmp_path):
    document = pymupdf.open()
    page, rect = _text_page(document)
    y = rect.y0 + 0.59 * rect.height
    page.draw_line((20, y), (575, y), width=0.48)
    page.draw_line((rect.x0, rect.y0), (rect.x1, rect.y1), width=0.48)
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "no_findings"


def test_rotated_text_is_not_guessed_from_axis_aligned_bbox(tmp_path):
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((200, 300), "Rotated words", rotate=90)
    rect = page.search_for("Rotated words")[0]
    _crossing(page, rect)
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "unknown"
    assert result["evidence_count"] == 0
    assert "rotated_text" in result["limitations"]


@pytest.mark.parametrize("field", ["title", "subject", "keywords"])
def test_supplied_regex_searches_metadata(tmp_path, field):
    document = pymupdf.open()
    _text_page(document)
    document.set_metadata({field: "Municipal ordinance / WORKING COPY / revision 2"})
    path = _save(document, tmp_path)
    result = inspect_pdf(path, draft_patterns=[r"(?i)\bworking copy\b"])
    assert result["state"] == "flagged"
    assert result["evidence"][0]["kind"] == "draft_metadata"
    assert result["evidence"][0]["field"] == field
    assert result["evidence"][0]["page"] is None
    assert inspect_pdf(path)["state"] == "no_findings"


def test_xmp_title_is_inspected_but_creator_is_not(tmp_path):
    document = pymupdf.open()
    _text_page(document)
    document.set_xml_metadata("""<x:xmpmeta xmlns:x="adobe:ns:meta/">
      <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
        <rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">
          <dc:title><rdf:Alt><rdf:li xml:lang="x-default">WORKING COPY</rdf:li></rdf:Alt>
          </dc:title><dc:creator>DRAFT AUTHOR</dc:creator>
        </rdf:Description>
      </rdf:RDF></x:xmpmeta>""")
    path = _save(document, tmp_path)
    result = inspect_pdf(path, draft_patterns=["WORKING COPY"])
    assert result["state"] == "flagged"
    assert result["evidence"][0]["field"] == "xmp:title"
    assert inspect_pdf(path, draft_patterns=["DRAFT AUTHOR"])["state"] == "no_findings"


@pytest.mark.parametrize(
    "text,y,expected",
    [
        ("DRAFT", 45, "flagged"),
        ("DRAFT", 400, "no_findings"),
        ("Please submit a draft report.", 45, "no_findings"),
        ("Please submit a draft report.", 400, "no_findings"),
    ],
)
def test_only_standalone_header_wording_is_evidence(tmp_path, text, y, expected):
    document = pymupdf.open()
    _text_page(document, text, y=y)
    result = inspect_pdf(_save(document, tmp_path), draft_patterns=[r"(?i)\bdraft\b"])
    assert result["state"] == expected
    if expected == "flagged":
        assert result["evidence"][0]["kind"] == "draft_header"


def test_no_global_draft_vocabulary(tmp_path):
    document = pymupdf.open()
    _text_page(document, "DRAFT", y=45)
    document.set_metadata({"title": "Draft preliminary proposed redline working copy"})
    path = _save(document, tmp_path)
    assert inspect_pdf(path)["state"] == "no_findings"
    assert inspect_pdf(path, draft_patterns=[])["state"] == "no_findings"


def test_all_pages_inspected_and_payload_bound_without_losing_strong_evidence(tmp_path):
    document = pymupdf.open()
    for _ in range(60):
        page, rect = _text_page(document)
        page.add_highlight_annot(rect)
    page, rect = _text_page(document)
    _crossing(page, rect, rectangle=True)
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "flagged"
    assert result["page_count"] == result["inspected_pages"] == 61
    assert result["evidence_count"] == 61
    assert len(result["evidence"]) == 50
    assert any(item["page"] == 61 and item["strength"] == "strong" for item in result["evidence"])
    assert "evidence_truncated" in result["limitations"]
    assert len(json.dumps(result, allow_nan=False)) < 25_000


def test_large_metadata_evidence_text_is_bounded(tmp_path):
    document = pymupdf.open()
    _text_page(document)
    document.set_metadata({"title": "DRAFT " + "x" * 100_000})
    result = inspect_pdf(_save(document, tmp_path), draft_patterns=["DRAFT"])
    assert result["state"] == "flagged"
    assert len(result["evidence"][0]["text"]) <= 200


def test_blank_pages_are_fully_inspected(tmp_path):
    document = pymupdf.open()
    document.new_page()
    document.new_page()
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "no_findings"
    assert result["page_count"] == result["inspected_pages"] == 2
    assert result["limitations"] == []


@pytest.mark.parametrize("native_header", [False, True])
def test_image_based_page_without_enough_native_text_is_unknown(tmp_path, native_header):
    document = pymupdf.open()
    page = document.new_page()
    image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
    image.clear_with(120)
    page.insert_image(page.rect, stream=image.tobytes("png"))
    if native_header:
        page.insert_text((72, 45), "Page 1")
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "unknown"
    assert result["page_count"] == result["inspected_pages"] == 1
    assert "insufficient_native_text" in result["limitations"]


def test_scan_does_not_hide_later_positive_evidence(tmp_path):
    document = pymupdf.open()
    page = document.new_page()
    image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
    image.clear_with(120)
    page.insert_image(page.rect, stream=image.tobytes("png"))
    page, rect = _text_page(document)
    page.add_strikeout_annot(rect)
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "flagged"
    assert result["inspected_pages"] == 2
    assert "insufficient_native_text" in result["limitations"]


def test_password_encryption_is_unknown(tmp_path):
    document = pymupdf.open()
    _text_page(document)
    path = _save(
        document,
        tmp_path,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-test",
        user_pw="reader-test",
    )
    result = inspect_pdf(path)
    assert result["state"] == "unknown"
    assert result["inspected_pages"] == 0
    assert "encrypted_pdf" in result["limitations"]
    assert result["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("content", [b"", b"not a pdf", b"%PDF-1.7\ntruncated"])
def test_empty_or_corrupt_files_are_unknown_without_throwing(tmp_path, content):
    path = tmp_path / "broken.pdf"
    path.write_bytes(content)
    result = inspect_pdf(path)
    assert result["state"] == "unknown"
    assert result["inspected_pages"] == 0
    assert result["page_count"] is None
    assert "unreadable_pdf" in result["limitations"]
    json.dumps(result, allow_nan=False)


def test_missing_file_is_unknown(tmp_path):
    result = inspect_pdf(tmp_path / "missing.pdf")
    assert result["state"] == "unknown"
    assert result["source_sha256"] is None
    assert result["limitations"] == ["unreadable_source"]


def test_invalid_patterns_are_not_silently_ignored(tmp_path):
    with pytest.raises(ValueError, match="pattern"):
        inspect_pdf(Path(tmp_path / "unused.pdf"), draft_patterns=["["])


def test_highlight_does_not_hide_flattened_strike_on_same_text(tmp_path):
    document = pymupdf.open()
    page, rect = _text_page(document)
    _crossing(page, rect)
    page.add_highlight_annot(rect)
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "flagged"
    assert result["evidence_count"] == 2
    assert {item["strength"] for item in result["evidence"]} == {"strong", "weak"}


def test_owner_only_encryption_is_unknown(tmp_path):
    document = pymupdf.open()
    _text_page(document)
    path = _save(document, tmp_path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner-test")
    result = inspect_pdf(path)
    assert result["state"] == "unknown"
    assert result["inspected_pages"] == 0
    assert "encrypted_pdf" in result["limitations"]


def test_unreadable_page_does_not_prevent_inspection_of_later_pages(tmp_path, monkeypatch):
    document = pymupdf.open()
    _text_page(document)
    page, rect = _text_page(document)
    page.add_strikeout_annot(rect)
    path = _save(document, tmp_path)
    load_page = pymupdf.Document.load_page

    def fail_first(document, page_id):
        if page_id == 0:
            raise RuntimeError("bad page object")
        return load_page(document, page_id)

    monkeypatch.setattr(pymupdf.Document, "load_page", fail_first)
    result = inspect_pdf(path)
    assert result["state"] == "flagged"
    assert result["inspected_pages"] == 1
    assert "unreadable_page" in result["limitations"]
    assert result["evidence"][0]["page"] == 2


def test_malformed_xmp_is_a_coverage_limitation(tmp_path):
    document = pymupdf.open()
    _text_page(document)
    document.set_xml_metadata("<unclosed>")
    result = inspect_pdf(_save(document, tmp_path), draft_patterns=["DRAFT"])
    assert result["state"] == "unknown"
    assert result["inspected_pages"] == 1
    assert "unreadable_xmp" in result["limitations"]


def test_truncated_weak_evidence_is_not_unknown_or_flagged(tmp_path):
    document = pymupdf.open()
    for _ in range(51):
        page, rect = _text_page(document)
        page.add_highlight_annot(rect)
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "no_findings"
    assert result["evidence_count"] == 51
    assert len(result["evidence"]) == 50
    assert result["limitations"] == ["evidence_truncated"]


@pytest.mark.parametrize("location", ["title", "header"])
def test_draft_patterns_are_case_insensitive_without_inline_flags(tmp_path, location):
    document = pymupdf.open()
    _text_page(document, "DRAFT" if location == "header" else "Final ordinance", y=45)
    if location == "title":
        document.set_metadata({"title": "DRAFT"})
    result = inspect_pdf(_save(document, tmp_path), draft_patterns=[r"\bdraft\b"])
    assert result["state"] == "flagged"
    assert result["evidence_count"] == 1


def test_full_page_scan_with_plentiful_ocr_text_is_unknown(tmp_path):
    document = pymupdf.open()
    page = document.new_page()
    image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
    image.clear_with(120)
    page.insert_image(page.rect, stream=image.tobytes("png"))
    page.insert_textbox(
        pymupdf.Rect(72, 72, 500, 700),
        "OCR text does not expose the revision marks painted in the source image. " * 10,
        render_mode=3,
    )
    path = _save(document, tmp_path)
    with pymupdf.open(path) as reopened:
        assert len(reopened[0].get_text()) > 100
    result = inspect_pdf(path)
    assert result["state"] == "unknown"
    assert result["inspected_pages"] == 1
    assert "raster_image_coverage" in result["limitations"]


def test_small_logo_does_not_make_native_page_unknown(tmp_path):
    document = pymupdf.open()
    page, _ = _text_page(document)
    image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10), False)
    image.clear_with(120)
    page.insert_image(pymupdf.Rect(10, 10, 30, 30), stream=image.tobytes("png"))
    result = inspect_pdf(_save(document, tmp_path))
    assert result["state"] == "no_findings"


def test_xmp_dtd_is_not_resolved(tmp_path):
    document = pymupdf.open()
    _text_page(document)
    document.set_xml_metadata("""<!DOCTYPE x [<!ENTITY status "DRAFT">]>
      <x xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>&status;</dc:title></x>""")
    result = inspect_pdf(_save(document, tmp_path), draft_patterns=["DRAFT"])
    assert result["state"] == "unknown"
    assert result["evidence_count"] == 0
    assert "unreadable_xmp" in result["limitations"]
