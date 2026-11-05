from pathlib import Path

import openpyxl
import pytest

from psweep.extraction.document_utils import (
    _extract_from_docx,
    _extract_from_xlsx,
    _HTMLTextExtractor,
    extract_text_from_document,
    is_supported_document,
)


def test_extract_text_from_html_document(tmp_path: Path):
    html_path = tmp_path / "filing.html"
    html_path.write_text(
        """
<html>
  <head>
    <title>Apple Filing Summary</title>
    <style>.hidden { display:none; }</style>
  </head>
  <body>
    <h1>Apple Q1 2026</h1>
    <p>Net sales were $143.8 billion.</p>
    <script>console.log('ignore me')</script>
    <p>Net income was $42.1 billion.</p>
  </body>
</html>
""",
        encoding="utf-8",
    )

    extracted = extract_text_from_document(html_path)

    assert is_supported_document(html_path) is True
    assert "Apple Filing Summary" in extracted
    assert "Apple Q1 2026" in extracted
    assert "Net sales were $143.8 billion." in extracted
    assert "Net income was $42.1 billion." in extracted
    assert "ignore me" not in extracted


# ---------------------------------------------------------------------------
# Serializer output locks
#
# document_utils has two table serializers that are intentionally DIFFERENT
# from each other and from section_locator's bordered `| a | b |` format:
#   - `_extract_from_docx` (#3) : unbordered `a | b`, empty cells DROPPED
#   - `_HTMLTextExtractor` (#4) : tab-separated columns, empty cells kept
# These tests pin the exact byte output so the "kept separate" decision cannot
# regress into accidental drift.
# ---------------------------------------------------------------------------


class TestDocxExtractTableExactOutput:
    """Lock #3: unbordered `a | b`, empty cells dropped, all-blank rows dropped."""

    def test_exact_output(self, tmp_path: Path):
        import docx

        d = docx.Document()
        d.add_paragraph("Rate Schedule")
        tbl = d.add_table(rows=3, cols=2)
        tbl.rows[0].cells[0].text = "A"
        tbl.rows[0].cells[1].text = "B"
        tbl.rows[1].cells[0].text = "1"
        tbl.rows[1].cells[1].text = ""  # blank cell -> dropped entirely
        # row 2 left blank -> dropped
        path = tmp_path / "t.docx"
        d.save(path)

        # `Rate Schedule` is a plain paragraph; table rows follow, unbordered,
        # with the blank cell in row 1 omitted (`1`, not `1 | `).
        assert _extract_from_docx(path) == "Rate Schedule\nA | B\n1"


class TestHtmlExtractorTableExactOutput:
    """Lock #4: tab-separated columns, empty cells preserved."""

    def test_exact_output(self):
        html = (
            "<html><body><table>"
            "<tr><td>AG</td><td>500</td><td>feet</td></tr>"
            "<tr><td>R1</td><td></td><td>x</td></tr>"
            "</table></body></html>"
        )
        ex = _HTMLTextExtractor()
        ex.feed(html)
        assert ex.get_text() == "AG\t500\tfeet\n\nR1\t\tx"


def test_xlsx_workbook_closed_on_success_and_failure(tmp_path, monkeypatch):
    """Workbook handles close after serialization and row failures."""

    class Workbook:
        sheetnames = ["Sheet1"]
        closed = False
        fail = False

        def __getitem__(self, key):
            return self

        def iter_rows(self, **kwargs):
            if self.fail:
                raise OSError("bad row")
            return iter([(1, None, "value")])

        def close(self):
            self.closed = True

    wb = Workbook()
    monkeypatch.setattr(openpyxl, "load_workbook", lambda *_a, **_k: wb)
    assert (
        _extract_from_xlsx(tmp_path / "a.xlsx")
        == "\n### Sheet: Sheet1 ###\n\n1 |  | value"
    )
    assert wb.closed
    wb.closed, wb.fail = False, True
    with pytest.raises(RuntimeError, match="bad row"):
        _extract_from_xlsx(tmp_path / "a.xlsx")
    assert wb.closed


def test_document_pdf_return_meta_passes_actual_scope(tmp_path, monkeypatch):
    """Document parsing exposes the parser's bounded actual PDF scope."""
    path = tmp_path / "a.pdf"
    path.write_bytes(b"fake")
    monkeypatch.setattr(
        "psweep.extraction.pdf_utils.extract_text_from_pdf",
        lambda *_a, **_k: (
            "selected",
            {"used_ocr": False, "page_range": (2, 3), "page_count": 3},
        ),
    )
    assert extract_text_from_document(path, (2, 9), return_meta=True) == (
        "selected",
        {"used_ocr": False, "page_range": (2, 3), "page_count": 3},
    )


# ── Tabular exports: every row is self-describing (``Header: value`` pairs) ──


def _doc(tmp_path: Path, name: str, content) -> Path:
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_csv_rows_carry_their_header_labels(tmp_path: Path):
    path = _doc(
        tmp_path,
        "locations.csv",
        "\ufeffSolarAPP+ locations\n"
        "Jurisdiction Name,Latitude,State,Notes,Solar + Storage\n"
        'Pima County,32.0444,AZ,,TRUE\n"Town of\n  Gilbert",33.3,AZ,"multi, word",FALSE,extra\n'
        "Short Town\n",
    )
    assert extract_text_from_document(path).splitlines() == [
        "SolarAPP+ locations",
        "Jurisdiction Name: Pima County | Latitude: 32.0444 | State: AZ | Solar + Storage: TRUE",
        "Jurisdiction Name: Town of Gilbert | Latitude: 33.3 | State: AZ | Notes: multi, word"
        " | Solar + Storage: FALSE | extra",
        "Jurisdiction Name: Short Town",
    ]


@pytest.mark.parametrize(
    "content",
    ["Pima County,32.0444,AZ\nBoulder County,40.1,CO\n", "a,b,a\nPima County,32.0444,AZ\n"],
)
def test_csv_without_a_header_row_stays_positional(tmp_path: Path, content):
    text = extract_text_from_document(_doc(tmp_path, "rows.csv", content))
    assert "Pima County | 32.0444 | AZ" in text.splitlines()


def test_csv_falls_back_to_cp1252(tmp_path: Path):
    path = _doc(tmp_path, "fees.csv", "Name,Fee\nCañon City,$100\n".encode("cp1252"))
    assert extract_text_from_document(path) == "Name: Cañon City | Fee: $100"


def test_xlsx_sheets_label_rows_with_their_own_header(tmp_path: Path):
    workbook = openpyxl.Workbook()
    first = workbook.active
    first.title = "Counties"
    for row in (("County", "Online"), ("Pima County", True), ("Boulder County", None)):
        first.append(row)
    second = workbook.create_sheet("Fees")
    for row in (("Permit", "Fee"), ("Solar PV", 300)):
        second.append(row)
    path = tmp_path / "book.xlsx"
    workbook.save(path)
    assert extract_text_from_document(path).splitlines() == [
        "",
        "### Sheet: Counties ###",
        "",
        "County: Pima County | Online: true",
        "County: Boulder County",
        "",
        "### Sheet: Fees ###",
        "",
        "Permit: Solar PV | Fee: 300",
    ]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (  # Socrata resource/x.json
            [{"county": "Pima", "online": True, "fee": 300, "notes": None}, {"county": "Yuma"}],
            ["county: Pima | online: true | fee: 300", "county: Yuma"],
        ),
        (  # ArcGIS REST query
            {
                "objectIdFieldName": "OBJECTID",
                "fields": [{"name": "NAME", "type": "esriFieldTypeString"}],
                "features": [
                    {"attributes": {"NAME": "Pima County", "LEVEL": "Gold"},
                     "geometry": {"x": -111.7, "y": 32.0}},
                ],
            },
            [
                "objectIdFieldName: OBJECTID",
                "", "### fields ###", "",
                "name: NAME | type: esriFieldTypeString",
                "", "### features ###", "",
                "attributes.NAME: Pima County | attributes.LEVEL: Gold"
                " | geometry.x: -111.7 | geometry.y: 32.0",
            ],
        ),
        (  # GeoJSON: polygon rings carry no text and are skipped
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature",
                     "properties": {"name": "Pima County", "tags": ["solar", "pv"],
                                    "contacts": [{"role": "permits"}]},
                     "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]}},
                ],
            },
            [
                "type: FeatureCollection",
                "", "### features ###", "",
                "type: Feature | properties.name: Pima County | properties.tags: solar, pv"
                " | properties.contacts.0.role: permits | geometry.type: Polygon",
            ],
        ),
        (  # CKAN datastore_search
            {"success": True,
             "result": {"records": [{"_id": 1, "County": "Pima"}], "total": 1}},
            ["success: true | result.total: 1", "", "### result.records ###", "",
             "_id: 1 | County: Pima"],
        ),
        (  # array of arrays: positional table rows
            {"data": [["Pima County", 32.0], ["Yuma County", 32.6]]},
            ["", "### data ###", "", "Pima County | 32.0", "Yuma County | 32.6"],
        ),
    ],
)
def test_json_record_arrays_render_as_labelled_rows(tmp_path: Path, payload, expected):
    import json

    path = _doc(tmp_path, "export.json", json.dumps(payload))
    assert is_supported_document(path)
    assert extract_text_from_document(path).splitlines() == expected


def test_json_without_records_is_indented_text(tmp_path: Path):
    path = _doc(tmp_path, "config.json", '{"county": "Pima", "levels": ["Gold"]}')
    assert extract_text_from_document(path) == (
        '{\n  "county": "Pima",\n  "levels": [\n    "Gold"\n  ]\n}'
    )


def test_invalid_json_fails_loudly(tmp_path: Path):
    with pytest.raises(RuntimeError, match="Failed to extract from JSON"):
        extract_text_from_document(_doc(tmp_path, "bad.json", "<html>not json</html>"))


def test_quotes_verify_against_labelled_rows(tmp_path: Path):
    from psweep.extraction.roster import quote_present

    path = _doc(tmp_path, "l.csv", "Jurisdiction Name,State,Solar + Storage\nPima County,AZ,TRUE\n")
    text = extract_text_from_document(path)
    assert quote_present("Jurisdiction Name: Pima County | State: AZ", text)
    assert quote_present("Pima County, AZ", text) is False
