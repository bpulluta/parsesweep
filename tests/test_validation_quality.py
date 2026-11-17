"""Quote fidelity, completeness and record-billed cost per contender."""

from __future__ import annotations

import json

import pytest

from psweep.extraction.document_utils import write_text_cache
from psweep.utils.schema_metadata import SchemaMetadata
from psweep.validation import quality as quality_module
from psweep.validation.quality import failed_documents, model_quality

TEXT = {"type": ["string", "null"]}
EVIDENCE = {
    "type": "object",
    "properties": {"value": TEXT, "quote": TEXT},
    "required": ["value", "quote"],
}
ROSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "ahj": {"type": "string"},
                    "online": EVIDENCE,
                    "fee": EVIDENCE,
                },
            },
        }
    },
    "$metadata": {"extraction": {"main_data_array": "rows"}},
}
ROWS_SCHEMA = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "feature": TEXT,
                    "value": TEXT,
                    "units": TEXT,
                    "source_verbatim": TEXT,
                },
            },
        }
    },
    "$metadata": {"extraction": {"main_data_array": "rules"}},
}
SOURCE = (
    "Wells shall be set back a minimum of 500 feet from residences.\nNoise is lim-\nited to 55 dBA."
)


def _metadata(tmp_path, schema):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    return SchemaMetadata(path)


def _record(directory, name, payload, *, quality=None, metrics=None, source_path=None):
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "document": {"source_path": str(source_path or directory / f"{name}.pdf")},
        "payload": payload,
        "quality": quality or {},
        "processing_metrics": metrics or {},
    }
    (directory / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")


def _metrics(cost, tokens=100, seconds=2.0):
    return {
        "input_tokens": tokens,
        "output_tokens": tokens // 10,
        "cost_usd": cost,
        "duration_seconds": seconds,
    }


def _manifest(directory, name, finished_at, failed=()):
    manifests = directory / "run_manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    manifest = {
        "timing": {"started_at": finished_at, "finished_at": finished_at},
        "errors": {"records": [{"source": {"document_path": path}} for path in failed]},
    }
    (manifests / f"{name}.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_roster_fidelity_and_cost(tmp_path):
    metadata = _metadata(tmp_path, ROSTER_SCHEMA)
    extracted = tmp_path / "extracted"
    _record(
        extracted,
        "a",
        {
            "rows": [
                {
                    "state": "AZ",
                    "ahj": "Pima",
                    "online": {"value": "Yes", "quote": "Apply online"},
                    "fee": {"value": None, "quote": None},
                },
                {
                    "state": "AZ",
                    "ahj": "Yuma",
                    "online": {"value": "No", "quote": "Paper only"},
                    "fee": {"value": "$50", "quote": "Fee: $50"},
                },
            ]
        },
        quality={"completeness": 0.75, "unverified_quotes": 1, "off_candidate_items": 2},
        metrics=_metrics(0.01, seconds=3.0),
    )
    _record(
        extracted,
        "b",
        {"rows": []},
        quality={"completeness": 0.0, "no_roster_match": True},
        metrics={"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
    )
    _record(extracted, "c", {"rows": []}, quality={"completeness": None}, metrics=_metrics(0.03))
    _manifest(extracted, "z-old", "2026-01-02T00:00:00Z", failed=["docs/old.pdf"])
    _manifest(extracted, "a-new", "2026-01-03T00:00:00+00:00", failed=["docs/x.pdf", "docs/y.pdf"])

    quality = model_quality({"m": extracted}, metadata, quote_field=None, focus=["online", "fee"])[
        "m"
    ]
    assert quality == {
        "quotes_verified": 3,
        "quotes_unverified": 1,
        "quotes_unchecked": 0,
        "quote_fidelity": 0.75,
        "completeness_mean": 0.375,
        "off_candidate_items": 2,
        "docs_ok": 3,
        "docs_failed": 2,
        "documents_no_roster_match": 0,
        "provider_failure_records": 0,
        "failure_records_by_category": {"unknown": 2},
        "unattributed_failure_records": 2,
        "documents_called": 2,
        "cost_usd": pytest.approx(0.04),
        "cost_per_document": pytest.approx(0.02),
        "unknown_cost_documents": 0,
        "input_tokens": 200,
        "output_tokens": 20,
        "seconds_per_document": 2.5,
    }
    assert failed_documents(extracted) == {"docs/x.pdf", "docs/y.pdf"}


def test_provider_failures_are_attributed_not_inferred_from_all_failed_docs(tmp_path):
    metadata = _metadata(tmp_path, ROSTER_SCHEMA)
    extracted = tmp_path / "extracted"
    _record(extracted, "a", {"rows": []}, quality={"status": "no_roster_match"})
    _manifest(extracted, "new", "2026-01-03T00:00:00Z")
    path = extracted / "run_manifests" / "new.manifest.json"
    manifest = json.loads(path.read_text())
    manifest["errors"]["records"] = [
        {"category": "network", "source": {"document_path": "a.pdf", "provider": "test"}},
        {"category": "document_io", "source": {"document_path": "b.pdf"}},
        {"category": "network", "source": {"document_path": "c.pdf"}},
    ]
    path.write_text(json.dumps(manifest))
    quality = model_quality({"m": extracted}, metadata, quote_field=None, focus=["online"])["m"]
    assert quality["docs_failed"] == 3
    assert quality["provider_failure_records"] == 1
    assert quality["failure_records_by_category"] == {"network": 2, "document_io": 1}
    assert quality["unattributed_failure_records"] == 2
    assert quality["documents_no_roster_match"] == 1


def test_null_cost_on_a_called_record_blanks_cost(tmp_path):
    metadata = _metadata(tmp_path, ROSTER_SCHEMA)
    extracted = tmp_path / "extracted"
    _record(extracted, "a", {"rows": []}, metrics=_metrics(0.01))
    _record(extracted, "b", {"rows": []}, metrics=_metrics(None))
    quality = model_quality({"m": extracted}, metadata, quote_field=None, focus=["online"])["m"]
    assert quality["cost_usd"] is None and quality["cost_per_document"] is None
    assert quality["unknown_cost_documents"] == 1
    assert quality["documents_called"] == 2
    assert quality["quote_fidelity"] is None
    assert quality["docs_failed"] == 0


def test_rows_fidelity_reads_the_extraction_text(tmp_path):
    metadata = _metadata(tmp_path, ROWS_SCHEMA)
    docs = tmp_path / "docs"
    docs.mkdir()
    cached, native, broken = docs / "cached.pdf", docs / "native.txt", docs / "broken.pdf"
    cached.write_bytes(b"%PDF cached")
    native.write_text("Permits expire after 180 days unless renewed.", encoding="utf-8")
    broken.write_bytes(b"%PDF broken")
    write_text_cache(cached, SOURCE, method="browser")
    extracted = tmp_path / "extracted"

    def rule(value, quote, feature="setback"):
        return {"feature": feature, "value": value, "units": None, "source_verbatim": quote}

    _record(
        extracted,
        "cached",
        {
            "rules": [
                rule("500", "set back a minimum of 500 FEET"),
                rule("55", "noise is limited to 55 dBA"),
                rule("60", "Noise is limited to 60 dBA."),
                rule("1", "  "),
                rule(None, "not in the text at all"),
            ]
        },
        source_path=cached,
    )
    _record(
        extracted,
        "native",
        {"rules": [rule("180", "expire after 180 days")]},
        source_path=native,
    )
    _record(
        extracted,
        "broken",
        {"rules": [rule("2", "anything"), rule(None, None)]},
        source_path=broken,
    )

    quality = model_quality(
        {"m": extracted}, metadata, quote_field="source_verbatim", focus=["value"]
    )["m"]
    assert (
        quality["quotes_verified"],
        quality["quotes_unverified"],
        quality["quotes_unchecked"],
    ) == (3, 2, 1)
    assert quality["quote_fidelity"] == 0.6
    assert quality["documents_called"] == 0 and quality["cost_per_document"] is None

    blank = model_quality({"m": extracted}, metadata, quote_field=None, focus=["value"])["m"]
    assert [
        blank[k]
        for k in ("quotes_verified", "quotes_unverified", "quotes_unchecked", "quote_fidelity")
    ] == [None, None, None, None]


def test_rows_dropped_at_extraction_count_as_unverified(tmp_path):
    metadata = _metadata(tmp_path, ROWS_SCHEMA)
    native = tmp_path / "native.txt"
    native.write_text("Permits expire after 180 days unless renewed.", encoding="utf-8")
    rule = {
        "feature": "expiry",
        "value": "180",
        "units": None,
        "source_verbatim": "expire after 180 days",
    }
    extracted = tmp_path / "extracted"
    _record(
        extracted,
        "native",
        {"rules": [rule]},
        quality={"unverified_quotes": 2},
        source_path=native,
    )
    quality = model_quality(
        {"m": extracted}, metadata, quote_field="source_verbatim", focus=["value"]
    )["m"]
    assert (quality["quotes_verified"], quality["quotes_unverified"]) == (1, 2)
    assert quality["quote_fidelity"] == pytest.approx(1 / 3)


def test_contenders_share_one_text_read_per_document(tmp_path, monkeypatch):
    metadata = _metadata(tmp_path, ROWS_SCHEMA)
    native = tmp_path / "native.txt"
    native.write_text("Permits expire after 180 days.", encoding="utf-8")
    rule = {"feature": "f", "value": "180", "units": None}
    reads = []
    real = quality_module.extract_text_from_document
    monkeypatch.setattr(
        quality_module,
        "extract_text_from_document",
        lambda path, **kw: reads.append(path) or real(path, **kw),
    )
    dirs = {}
    for label, quote in (("a", "after 180 days"), ("b", "after 90 days"), ("c", None)):
        dirs[label] = tmp_path / label
        _record(
            dirs[label], "doc", {"rules": [{**rule, "source_verbatim": quote}]}, source_path=native
        )
    _record(dirs["a"], "only_a", {"rules": [{**rule, "source_verbatim": "x"}]}, source_path=native)

    quality = model_quality(dirs, metadata, quote_field="source_verbatim", focus=["value"])
    assert reads == [native, native]
    assert {k: (q["quotes_verified"], q["quotes_unverified"]) for k, q in quality.items()} == {
        "a": (1, 1),
        "b": (0, 1),
        "c": (0, 1),
    }
    assert quality["a"]["docs_ok"] == 2 and quality["b"]["docs_ok"] == 1


def test_uncached_pdfs_are_read_natively_never_ocr(tmp_path, monkeypatch):
    pymupdf = pytest.importorskip("pymupdf")
    metadata = _metadata(tmp_path, ROWS_SCHEMA)
    text_pdf, scanned_pdf = tmp_path / "text.pdf", tmp_path / "scanned.pdf"
    for path, text in ((text_pdf, "Setback is 500 feet."), (scanned_pdf, None)):
        document = pymupdf.open()
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
        document.save(path)
        document.close()
    ocr = []
    monkeypatch.setattr(
        "psweep.extraction.pdf_utils._extract_with_ocr", lambda *a, **kw: ocr.append(a) or ("", {})
    )
    extracted = tmp_path / "extracted"
    rule = {"feature": "f", "value": "500", "units": None, "source_verbatim": "500 feet"}
    _record(extracted, "text", {"rules": [rule]}, source_path=text_pdf)
    _record(extracted, "scanned", {"rules": [rule]}, source_path=scanned_pdf)

    quality = model_quality(
        {"m": extracted}, metadata, quote_field="source_verbatim", focus=["value"]
    )["m"]
    assert (quality["quotes_verified"], quality["quotes_unchecked"]) == (1, 1) and ocr == []


def test_unreadable_manifest_is_an_error(tmp_path):
    extracted = tmp_path / "extracted"
    (extracted / "run_manifests").mkdir(parents=True)
    (extracted / "run_manifests" / "bad.manifest.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad.manifest.json: unreadable run manifest"):
        failed_documents(extracted)
    assert failed_documents(tmp_path / "missing") == set()
