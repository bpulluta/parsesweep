"""Records persist how their input was read: per-page representation, image use, coverage."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from jsonschema import ValidationError

from psweep import pipeline
from psweep.extraction import document_extractor, pdf_utils
from psweep.extraction.document_utils import ocr_page_cache
from psweep.extraction.page_locator import PageLocator
from psweep.extraction.record_writer import (
    CONTRACT_VERSION,
    build_extraction_record,
    record_extraction_scope,
)
from psweep.extraction.schema_utils import record_validator
from psweep.pipeline import _execute_extraction, _prepare_extraction
from psweep.utils.file_io import artifact_sha256
from psweep.utils.schema_metadata import SchemaMetadata

pymupdf = pytest.importorskip("pymupdf")

SHA_A, SHA_B = "a" * 64, "b" * 64


def _page(number, status, **extra):
    return {"page": number, "status": status, "chars": 10, "image_fraction": 0.0, **extra}


def _scope(*pages):
    numbers = [p["page"] for p in pages]
    return {
        "page_count": len(pages),
        "selected_pages": numbers,
        "extracted_pages": [p["page"] for p in pages if p["status"] != "unreadable"],
        "pages": list(pages),
    }


# --- pure unit behaviour ----------------------------------------------------------


def test_contract_version_is_bumped_deliberately():
    assert CONTRACT_VERSION == "3.0.0"
    assert record_validator().schema["properties"]["contract_version"]["const"] == "3.0.0"


def test_ocr_digest_only_on_pages_whose_ocr_was_obtained():
    done = pdf_utils.PageRepresentation(2, "ocr", 40, 0.9, ocr_sha256=SHA_A).as_dict()
    plain = pdf_utils.PageRepresentation(1, "native", 40, 0.0).as_dict()
    assert done["ocr_sha256"] == SHA_A
    assert "ocr_sha256" not in plain


@pytest.mark.parametrize(
    ("recorded", "entries", "stale"),
    [
        ({1: None, 2: SHA_A}, {}, False),  # no entries: unknown, regenerable
        ({1: None, 2: SHA_A, 3: SHA_B}, {3: SHA_B}, False),  # one missing, one matching
        ({2: SHA_A}, {2: SHA_B}, True),  # present entry differs from the record
        ({2: None}, {2: SHA_A}, True),  # page the record could not read became readable
        ({1: None, 2: SHA_A}, {1: SHA_B}, True),  # entry for a page the record read natively
        ({2: SHA_A}, {7: SHA_B}, False),  # entry outside the record's selected pages
    ],
)
def test_ocr_reuse_conflict_truth_table(recorded, entries, stale):
    pages = [_page(n, "ocr", **({"ocr_sha256": d} if d else {})) for n, d in recorded.items()]
    assert pdf_utils.ocr_reuse_conflict(pages, entries) is stale


def test_cache_entries_map_valid_pages_to_digests(tmp_path):
    cache = pdf_utils.OcrPageCache(tmp_path / "o", "s" * 64)
    assert cache.entries() == {}
    cache.put(3, "three")
    cache.put(1, "one")
    (tmp_path / "o" / "2.json").write_text("{not json")
    assert cache.entries() == {1: pdf_utils._sha("one"), 3: pdf_utils._sha("three")}


def test_locator_fingerprint_follows_the_ocr_version(monkeypatch):
    before = PageLocator("rates")._fingerprint()
    monkeypatch.setattr(pdf_utils, "OCR_PAGE_VERSION", "ocr-page-v2")
    assert PageLocator("rates")._fingerprint() != before


# --- record contract --------------------------------------------------------------


@pytest.fixture
def metadata(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(
        json.dumps(
            {
                "$metadata": {"extraction": {"main_data_array": "items"}},
                "type": "object",
                "properties": {"items": {"type": "array"}},
            }
        )
    )
    return SchemaMetadata(path)


def _result():
    return SimpleNamespace(data={"items": []}, coverage={}, validation_notes=[])


def _record(metadata, tmp_path, result, **extra):
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"x")
    record, _ = build_extraction_record(
        doc_path=doc,
        source_document_id="doc.pdf",
        result=result,
        model="m",
        provider="openai",
        schema_metadata=metadata,
        source_sha256="c" * 64,
        **extra,
    )
    record_validator().validate(record)
    return record


def test_unreadable_pages_are_named_in_warning_and_coverage():
    result = _result()
    record_extraction_scope(
        result,
        parser_scope=_scope(_page(1, "native"), _page(4, "unreadable"), _page(7, "unreadable")),
    )
    assert result.coverage["parser_partial"] is True
    assert result.coverage["unreadable_pages"] == [4, 7]
    assert any(
        "PDF pages 4,7 could not be read as text or OCR" in n for n in result.validation_notes
    )


def test_other_partial_causes_keep_the_generic_message():
    result = _result()
    scope = _scope(_page(1, "native"), _page(2, "native"))
    scope["extracted_pages"] = [1]
    record_extraction_scope(result, parser_scope=scope)
    assert result.coverage["parser_partial"] is True
    assert "unreadable_pages" not in result.coverage
    assert any("missing selected pages" in n for n in result.validation_notes)


def test_complete_pdf_has_no_unreadable_pages_key():
    result = _result()
    record_extraction_scope(result, parser_scope=_scope(_page(1, "native")))
    assert result.coverage["parser_partial"] is False
    assert "unreadable_pages" not in result.coverage


def test_image_only_grounding_is_qualified_in_coverage():
    result = _result()
    scope = _scope(_page(1, "native"), _page(2, "ocr"), _page(3, "unreadable"))
    record_extraction_scope(result, parser_scope=scope, visual_pages=[2, 3])
    assert result.coverage["image_only_pages"] == [3]  # page 2 also has text to verify against
    assert any("PDF pages 3" in n and "image" in n for n in result.validation_notes)


def test_text_grounded_visual_pages_are_not_image_only():
    result = _result()
    scope = _scope(_page(1, "native"), _page(2, "ocr"))
    record_extraction_scope(result, parser_scope=scope, visual_pages=[1, 2])
    assert "image_only_pages" not in result.coverage


def test_record_persists_input_provenance(metadata, tmp_path):
    scope = _scope(_page(1, "native"), _page(2, "ocr", ocr_sha256=SHA_A))
    record = _record(
        metadata,
        tmp_path,
        _result(),
        input_provenance=pipeline.input_provenance("vision", scope, [1, 2]),
    )
    assert record["input"] == {
        "input_mode": "vision",
        "pages": scope["pages"],
        "visual_pages": [1, 2],
        "render_dpi": pdf_utils.RENDER_DPI,
    }


def test_text_mode_records_no_image_use(metadata, tmp_path):
    record = _record(
        metadata, tmp_path, _result(), input_provenance=pipeline.input_provenance("text", {}, [])
    )
    assert record["input"] == {"input_mode": "text", "pages": [], "visual_pages": []}


def test_input_block_is_required_and_closed(metadata, tmp_path):
    record = _record(
        metadata, tmp_path, _result(), input_provenance=pipeline.input_provenance("text", {}, [])
    )
    validator = record_validator()
    record["input"]["extra"] = 1
    with pytest.raises(ValidationError):
        validator.validate(record)
    del record["input"]
    with pytest.raises(ValidationError):
        validator.validate(record)


def test_removed_context_truncated_key_is_rejected(metadata, tmp_path):
    result = _result()
    result.coverage = {"input_characters": 1, "extracted_characters": 1}
    record = _record(
        metadata, tmp_path, result, input_provenance=pipeline.input_provenance("text", {}, [])
    )
    record["processing_metrics"]["coverage"]["context_truncated"] = False
    with pytest.raises(ValidationError):
        record_validator().validate(record)


def test_extractor_coverage_has_no_truncation_key():
    extractor = document_extractor.DocumentExtractor(api_key="k", model="gpt-4o-mini")
    extractor.client.extract = Mock(return_value={"data": {"items": []}, "cost": 0.0})
    schema = {"type": "object", "properties": {"items": {"type": "array"}}}
    assert "context_truncated" not in extractor.extract("text", schema).coverage


# --- end to end: reuse proof and fingerprints -------------------------------------


@pytest.fixture
def run(tmp_path, monkeypatch):
    source = tmp_path / "documents"
    source.mkdir()
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {"extraction": {"main_data_array": "items"}},
                "type": "object",
                "required": ["items"],
                "properties": {"items": {"type": "array", "items": {"type": "object"}}},
            }
        )
    )
    cfg = {"provider": "openai", "model": "gpt-fixture", "api_key": "fixture"}
    monkeypatch.setattr("psweep.utils.config.get_config", lambda: SimpleNamespace(llm_config=cfg))
    provider = Mock(return_value={"data": {"items": []}, "cost": 0.01})
    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", provider)
    inputs = {
        "path": str(source),
        "schema": str(schema),
        "output": str(tmp_path / "records"),
        "llm_config": cfg,
    }
    return inputs, provider, source


def _build_pdf(path, raster_pages, native=True):
    doc = pymupdf.open()
    if native:
        doc.new_page().insert_textbox(pymupdf.Rect(36, 36, 560, 780), "Native prose. " * 40)
    for _ in range(raster_pages):
        page = doc.new_page()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 160), False)
        pix.set_rect(pix.irect, (200, 200, 200))
        page.insert_image(page.rect, pixmap=pix)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def scan(run, monkeypatch):
    inputs, provider, source = run
    pdf = _build_pdf(source / "scan.pdf", 2)
    monkeypatch.setattr(pdf_utils, "_ocr_page", lambda page: f"OCR text page {page.number + 1}")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.reset_mock()
    return inputs, provider, pdf


def _record_of(inputs, name="scan.json"):
    return json.loads((Path(inputs["output"]) / name).read_text())


def _entry_files(pdf):
    return sorted((pdf.parent / ".text" / f"{pdf.name}.ocr").glob("*.json"))


def test_record_carries_per_page_ocr_digests(scan):
    inputs, _, pdf = scan
    pages = _record_of(inputs)["input"]["pages"]
    assert [p["status"] for p in pages] == ["native", "ocr", "ocr"]
    entries = ocr_page_cache(pdf, artifact_sha256(pdf)).entries()
    assert {p["page"]: p["ocr_sha256"] for p in pages if "ocr_sha256" in p} == entries
    manifest = next((Path(inputs["output"]) / "run_manifests").glob("*.manifest.json"))
    proofs = json.loads(manifest.read_text())["reuse"]
    assert all(not p.get("derived_text_sha256") for p in proofs.values())


def test_complete_cache_keeps_reuse(scan):
    inputs, provider, _ = scan
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.assert_not_called()


def test_one_missing_page_entry_is_reused_without_a_provider_call(scan):
    inputs, provider, pdf = scan
    _entry_files(pdf)[0].unlink()
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.assert_not_called()


def test_no_entries_at_all_is_reused(scan):
    import shutil

    inputs, provider, pdf = scan
    shutil.rmtree(pdf.parent / ".text")
    plan = _prepare_extraction(inputs)
    assert plan["reused"] and not plan["pending"]
    assert _execute_extraction(plan).successful == 1
    provider.assert_not_called()


def test_a_tampered_page_entry_re_extracts(scan):
    inputs, provider, pdf = scan
    ocr_page_cache(pdf, artifact_sha256(pdf)).put(3, "different OCR")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.assert_called_once()
    assert _record_of(inputs)["input"]["pages"][2]["ocr_sha256"] == pdf_utils._sha("different OCR")


def test_a_changed_entry_after_planning_fails_the_reuse(scan):
    inputs, provider, pdf = scan
    plan = _prepare_extraction(inputs)
    assert plan["reused"]
    ocr_page_cache(pdf, artifact_sha256(pdf)).put(2, "changed after planning")
    assert _execute_extraction(plan).failed == 1
    provider.assert_not_called()


def test_a_corrupt_entry_is_a_miss_not_a_conflict(scan):
    inputs, provider, pdf = scan
    _entry_files(pdf)[0].write_text("{corrupt")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.assert_not_called()


def test_a_manual_page_range_judges_only_its_own_pages(run, monkeypatch):
    inputs, provider, source = run
    pdf = _build_pdf(source / "scan.pdf", 2)
    monkeypatch.setattr(pdf_utils, "_ocr_page", lambda page: f"OCR text page {page.number + 1}")
    inputs["pages"] = "1-2"
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.reset_mock()
    ocr_page_cache(pdf, artifact_sha256(pdf)).put(3, "outside the selection")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.assert_not_called()


def test_unrelated_text_documents_are_not_regraded_by_pdf_versions(run, monkeypatch):
    inputs, provider, source = run
    (source / "note.txt").write_text("plain source")
    _build_pdf(source / "scan.pdf", 1)
    monkeypatch.setattr(pdf_utils, "_ocr_page", lambda page: "OCR text")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 2
    provider.reset_mock()
    monkeypatch.setattr(pdf_utils, "OCR_PAGE_VERSION", "ocr-page-v2")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 2
    assert provider.call_count == 1  # the PDF only


def test_page_representation_version_invalidates_pdfs_only(run, monkeypatch):
    inputs, provider, source = run
    (source / "note.txt").write_text("plain source")
    _build_pdf(source / "scan.pdf", 1)
    monkeypatch.setattr(pdf_utils, "_ocr_page", lambda page: "OCR text")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 2
    provider.reset_mock()
    monkeypatch.setattr(pdf_utils, "PAGE_REPRESENTATION_VERSION", "pdf-pages-v2")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 2
    assert provider.call_count == 1


def test_prompt_and_repair_behavior_version_invalidates_reuse(run, monkeypatch):
    inputs, provider, source = run
    (source / "note.txt").write_text("plain source")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.reset_mock()
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.assert_not_called()
    monkeypatch.setattr(document_extractor, "EXTRACTION_BEHAVIOR_VERSION", "extract-v-next")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.assert_called_once()


def test_old_contract_records_are_not_reused(run):
    inputs, provider, source = run
    (source / "note.txt").write_text("plain source")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    path = Path(inputs["output"]) / "note.json"
    record = json.loads(path.read_text())
    record["contract_version"] = "2.0.0"
    path.write_text(json.dumps(record))
    provider.reset_mock()
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.assert_called_once()


# --- end to end: image use --------------------------------------------------------


def test_auto_mode_records_image_use_and_qualifies_image_only_grounding(run, monkeypatch):
    inputs, provider, source = run
    _build_pdf(source / "scan.pdf", 1)
    monkeypatch.setattr(pdf_utils, "_ocr_page", lambda _page: "")
    inputs["input_mode"] = "auto"
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    sent = provider.call_args.kwargs["page_images"]
    assert [image.page for image in sent] == [2]
    record = _record_of(inputs)
    assert record["input"]["input_mode"] == "auto"
    assert record["input"]["visual_pages"] == [2]
    assert record["input"]["render_dpi"] == pdf_utils.RENDER_DPI
    coverage = record["processing_metrics"]["coverage"]
    assert coverage["unreadable_pages"] == [2]
    assert coverage["image_only_pages"] == [2]
    assert any(
        "PDF pages 2 could not be read as text or OCR" in w for w in record["quality"]["warnings"]
    )


def test_text_mode_record_does_not_claim_visual_grounding(run, monkeypatch):
    inputs, provider, source = run
    _build_pdf(source / "scan.pdf", 1)
    monkeypatch.setattr(pdf_utils, "_ocr_page", lambda _page: "")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    record = _record_of(inputs)
    assert record["input"]["input_mode"] == "text"
    assert record["input"]["visual_pages"] == []
    assert "image_only_pages" not in record["processing_metrics"]["coverage"]
    assert not provider.call_args.kwargs["page_images"]
