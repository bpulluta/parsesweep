"""Extraction audit regressions for the canonical workflow and record.

Cover schema-owned counts, record/provenance identity, strict contracts,
atomic writing and provider accounting.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import hashlib
import json

from psweep.exceptions import SchemaValidationError
from psweep.extraction.llm_factory import detect_provider
from psweep.extraction.llm_client import LLMClient
import pytest
from jsonschema import ValidationError

from psweep.extraction.provenance import SourceContext
from psweep.pipeline import _extract_one_document
from psweep.extraction.record_writer import (
    build_extraction_record,
    record_extraction_scope,
    write_extraction_record,
)
from psweep.extraction.schema_utils import (
    build_schema_validator,
    record_validator,
)
from psweep.utils.schema_metadata import SchemaMetadata


# ── detect_provider (domain-neutral) ────────────────────────────────────────


def test_detect_provider_has_no_hardcoded_customer_prefix():
    # A bare deployment name is not sniffed as azure; provider is set explicitly.
    assert detect_provider("my-azure-deployment") == "openai"


def test_detect_provider_recognizes_canonical_markers():
    assert detect_provider("azure/anything") == "azure"
    assert detect_provider("claude-opus-4.5") == "anthropic"
    assert detect_provider("gemini-3-pro") == "gemini"
    assert detect_provider("gpt-5") == "openai"
    assert detect_provider("mistral-large-2") == "mistral"


# ── canonical record envelope ───────────────────────────────────────────────


def _make_result(data):
    return SimpleNamespace(
        data=data,
        cost=0.01,
        processing_time=1.0,
        completeness_score=0.9,
        validation_notes=[],
        input_tokens=10,
        output_tokens=20,
    )


@pytest.fixture
def record_metadata(tmp_path):
    """Declare the record's payload contract without private domain inputs."""
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": {
            "extraction": {
                "main_data_array": "items",
            }
        },
        "type": "object",
        "required": ["items"],
        "properties": {
            "metadata": {"type": "object"},
            "items": {"type": "array"},
            "references": {"type": "array"},
        },
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema), encoding="utf-8")
    return SchemaMetadata(path)


@pytest.fixture
def source_document(tmp_path):
    """Use real source bytes for canonical source digest assertions."""
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(b"synthetic document")
    return doc


def test_record_document_identity_is_caller_supplied(
    record_metadata, source_document
):
    """source_document_id is the curated path; provenance is index-derived."""
    source = SourceContext(
        url="https://example.org/a.pdf",
        scope="target",
        target_id="abc123def456",
        target_label="AZ / Pima County",
        target_metadata={},
    )
    record, _ = build_extraction_record(
        doc_path=source_document,
        source_document_id="AZ/Pima/doc.pdf",
        source=source,
        result=_make_result({"metadata": {"jurisdiction": "X"}, "items": []}),
        model="example",
        provider="openai",
        schema_metadata=record_metadata,
    )
    document = record["document"]
    assert document["source_document_id"] == "AZ/Pima/doc.pdf"
    assert document["source_url"] == "https://example.org/a.pdf"
    assert document["source_scope"] == "target"
    assert document["target_id"] == "abc123def456"
    record_validator().validate(record)


def test_record_without_index_row_has_null_provenance(
    record_metadata, source_document
):
    record, _ = build_extraction_record(
        doc_path=source_document,
        source_document_id="doc.pdf",
        result=_make_result({"items": []}),
        model="example",
        provider="openai",
        schema_metadata=record_metadata,
    )
    assert {
        key: record["document"][key]
        for key in ("source_url", "source_scope", "target_id")
    } == {"source_url": None, "source_scope": None, "target_id": None}
    assert record["quality"]["status"] == "extracted"
    assert record["quality"]["completeness"] == 0.9
    assert record["quality"]["unverified_quotes"] == 0


@pytest.mark.parametrize("items", [[], [{"a": 1}]])
def test_record_counts_declared_array_not_largest(
    record_metadata,
    source_document,
    items,
):
    """Empty primary arrays are zero-item results, not reference counts."""
    result = _make_result({"items": items, "references": list(range(10))})
    record, count = build_extraction_record(
        doc_path=source_document,
        source_document_id="doc.txt",
        result=result,
        model="example",
        provider="openai",
        schema_metadata=record_metadata,
    )
    assert count == len(items)
    assert record["item_count"] == len(items)


def test_record_source_identity_distinguishes_partitions_and_formats(
    tmp_path,
    record_metadata,
):
    """One run cannot assign equal IDs to different same-stem documents."""
    paths = [
        tmp_path / "a/report.pdf",
        tmp_path / "b/report.pdf",
        tmp_path / "a/report.txt",
    ]
    records = []
    for doc in paths:
        doc.parent.mkdir(exist_ok=True)
        doc.write_bytes(b"source")
        record, _ = build_extraction_record(
            doc_path=doc,
            source_document_id="doc.txt",
            result=_make_result({"items": []}),
            model="example",
            provider="openai",
            run_id="run://shared",
            schema_metadata=record_metadata,
        )
        records.append(record)
    assert len({record["record_id"] for record in records}) == 3
    assert {record["document"]["source_path"] for record in records} == {
        path.as_posix() for path in paths
    }


def test_record_emits_source_digest_and_optional_reuse_fingerprint(
    record_metadata,
    source_document,
):
    """Evidence hashes are canonical lowerhex, without provider secrets."""
    record, _ = build_extraction_record(
        doc_path=source_document,
        source_document_id="doc.txt",
        result=_make_result({"items": []}),
        model="example",
        provider="openai",
        schema_metadata=record_metadata,
        input_fingerprint="a" * 64,
    )
    assert (
        record["document"]["source_sha256"]
        == hashlib.sha256(
            source_document.read_bytes(),
        ).hexdigest()
    )
    assert record["lineage"]["input_fingerprint"] == "a" * 64


def test_emitted_record_and_payload_validate_closed_contract(
    record_metadata,
    source_document,
):
    """Counts and coverage fit envelope 1.1, rejecting unknown fields."""
    result = _make_result({"items": []})
    result.coverage = {
        "input_characters": 15,
        "extracted_characters": 5,
        "context_truncated": True,
    }
    record_extraction_scope(result, page_range=(2, 4), section_filtered=True)
    record, _ = build_extraction_record(
        doc_path=source_document,
        source_document_id="doc.txt",
        result=result,
        model="example",
        provider="openai",
        schema_metadata=record_metadata,
        input_fingerprint="a" * 64,
    )
    validator = record_validator()
    validator.validate(record)
    build_schema_validator(record_metadata.schema).validate(record["payload"])
    assert record["contract_version"] == "1.1.0"
    assert record["processing_metrics"]["coverage"]["selected_page_range"] == [
        2,
        4,
    ]
    assert any(
        "not a full-document" in note for note in record["quality"]["warnings"]
    )
    record["unexpected"] = True
    with pytest.raises(ValidationError):
        validator.validate(record)
    del record["unexpected"]
    record["processing_metrics"]["coverage"]["invented_scope"] = True
    with pytest.raises(ValidationError):
        validator.validate(record)


@pytest.mark.parametrize("items", [None, {}, "not an array"])
def test_record_rejects_invalid_declared_array(
    record_metadata, source_document, items
):
    """Non-arrays cannot silently become verified zero-item records."""
    with pytest.raises(SchemaValidationError, match="main data array"):
        build_extraction_record(
            doc_path=source_document,
            source_document_id="doc.txt",
            result=_make_result({"items": items}),
            model="example",
            provider="openai",
            schema_metadata=record_metadata,
        )


def test_record_write_is_atomic_on_replace_failure(
    tmp_path,
    record_metadata,
    source_document,
    monkeypatch,
):
    """Interrupted publication preserves valid JSON without staging debris."""
    output = tmp_path / "doc.json"
    previous = '{"previous":"complete"}'
    output.write_text(previous, encoding="utf-8")
    before = set(tmp_path.iterdir())

    def fail_replace(*args, **kwargs):
        raise OSError("replacement failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failed"):
        write_extraction_record(
            output,
            doc_path=source_document,
            source_document_id="doc.txt",
            result=_make_result({"items": []}),
            model="example",
            provider="openai",
            schema_metadata=record_metadata,
        )
    assert output.read_text(encoding="utf-8") == previous
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("invalid_part", ["metrics", "coverage", "confidence"])
def test_writer_validates_envelope_before_replacing_existing_json(
    tmp_path,
    record_metadata,
    source_document,
    invalid_part,
):
    """Invalid records cannot replace a previously published artifact."""
    result = _make_result({"items": []})
    if invalid_part == "metrics":
        result.input_tokens = -1
    elif invalid_part == "confidence":
        result.completeness_score = 2
    else:
        result.coverage = {"invented_scope": True}
    output = tmp_path / "doc.json"
    output.write_text('{"previous":"complete"}', encoding="utf-8")
    with pytest.raises(ValidationError):
        write_extraction_record(
            output,
            doc_path=source_document,
            source_document_id="doc.txt",
            result=result,
            model="example",
            provider="openai",
            schema_metadata=record_metadata,
        )
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "previous": "complete",
    }


def test_writer_default_run_identity_is_unique(
    record_metadata, source_document
):
    """Direct regular writer calls also retain separate attempt identity."""
    kwargs = {
        "doc_path": source_document,
        "source_document_id": "doc.txt",
        "result": _make_result({"items": []}),
        "model": "example",
        "provider": "openai",
        "schema_metadata": record_metadata,
    }
    first, _ = build_extraction_record(**kwargs)
    second, _ = build_extraction_record(**kwargs)
    assert first["lineage"]["run_id"] != second["lineage"]["run_id"]
    assert first["record_id"] != second["record_id"]


@pytest.mark.parametrize("fingerprint", ["short", "A" * 64, 0])
def test_writer_rejects_malformed_input_fingerprint(
    record_metadata,
    source_document,
    fingerprint,
):
    """Invalid reuse evidence never enters a published envelope."""
    with pytest.raises(ValueError, match="input_fingerprint"):
        build_extraction_record(
            doc_path=source_document,
            source_document_id="doc.txt",
            result=_make_result({"items": []}),
            model="example",
            provider="openai",
            schema_metadata=record_metadata,
            input_fingerprint=fingerprint,
        )


def test_writer_missing_source_cannot_replace_existing_record(
    tmp_path, record_metadata
):
    """Missing source bytes are not silent evidence for validated reuse."""
    output = tmp_path / "doc.json"
    output.write_text('{"previous":"complete"}', encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        write_extraction_record(
            output,
            doc_path=tmp_path / "missing.txt",
            source_document_id="doc.txt",
            result=_make_result({"items": []}),
            model="example",
            provider="openai",
            schema_metadata=record_metadata,
        )
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "previous": "complete"
    }


# ── _extract_one_document (shared per-document core) ─────────────────────────


def _plan(tmp_path, metadata, doc):
    return {
        "llm": {"model": "gpt-5", "provider": "openai"},
        "inputs": {},
        "outputs": {doc: tmp_path / "sample.json"},
        "roster": None,
        "candidates": {},
        "anchors": {},
        "roster_errors": {},
        "context": {},
        "root": tmp_path,
        "schema": metadata.schema,
        "schema_path": metadata.schema_path,
        "metadata": metadata,
        "evidence_paths": [],
        "fingerprints": {doc: "a" * 64},
    }


def test_extract_one_document_success_shape(tmp_path, record_metadata):
    """The shared per-document workflow reports recorded items and usage."""
    doc = tmp_path / "sample.txt"
    doc.write_text("hello world", encoding="utf-8")

    extractor = MagicMock()
    extractor.extract.return_value = _make_result(
        {"items": [{"x": 1}, {"x": 2}]}
    )

    res = _extract_one_document(
        doc,
        _plan(tmp_path, record_metadata, doc),
        extractor=extractor,
        run_id="run://abc",
    )

    assert res["success"] is True
    assert res["file"] == "sample.txt"
    assert res["items"] == 2
    assert res["input_tokens"] == 10
    assert res["output_tokens"] == 20
    assert (tmp_path / "sample.json").exists()


def test_extract_one_document_failure_shape(tmp_path, record_metadata):
    """An attempted extraction failure returns a structured error outcome."""
    doc = tmp_path / "sample.txt"
    doc.write_text("hello world", encoding="utf-8")

    extractor = MagicMock()
    extractor.extract.side_effect = RuntimeError("boom")

    res = _extract_one_document(
        doc,
        _plan(tmp_path, record_metadata, doc),
        extractor=extractor,
        run_id="run://abc",
    )

    assert res["success"] is False
    assert res["file"] == "sample.txt"
    assert "boom" in res["error"]
    assert isinstance(res["errors"], list) and res["errors"]


# ── token usage surfaced from the LLM client ────────────────────────────────


def test_llm_client_returns_token_usage():
    client = LLMClient(api_key="k", model="gpt-4o-mini", provider="openai")

    fake_response = MagicMock()
    fake_response.choices = [
        MagicMock(message=MagicMock(content=json.dumps({"items": []})))
    ]
    fake_response.usage = SimpleNamespace(
        prompt_tokens=123, completion_tokens=45
    )

    with (
        patch(
            "psweep.extraction.llm_client.completion",
            return_value=fake_response,
        ),
        patch(
            "psweep.extraction.llm_client.completion_cost",
            return_value=0.002,
        ),
    ):
        out = client.extract("text", {"type": "object", "properties": {}})

    assert out["input_tokens"] == 123
    assert out["output_tokens"] == 45
    assert out["cost"] == 0.002
