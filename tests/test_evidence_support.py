"""Evidence ``support``: distinct nonblank recorded URLs agreeing with the chosen unit."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from psweep.compilation.data_compiler import DataCompiler
from psweep.extraction.provenance import SourceContext
from psweep.extraction.record_writer import build_extraction_record
from psweep.extraction.schema_utils import build_schema_validator, record_validator
from psweep.utils.schema_metadata import SchemaMetadata


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    schema = {
        "$metadata": {
            "extraction": {"main_data_array": "items"},
            "identity": {"deduplication": {"key_fields": ["name"]}},
        },
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "answer", "qualifier"],
                    "properties": {
                        "name": {"type": "string"},
                        "answer": {
                            "type": "object",
                            "required": ["value", "quote"],
                            "properties": {
                                "value": {"type": ["string", "null"]},
                                "quote": {"type": ["string", "null"]},
                            },
                        },
                        "qualifier": {"type": ["string", "null"]},
                        "unrelated": {"type": ["string", "null"]},
                    },
                },
            }
        },
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(schema))
    metadata = SchemaMetadata(path)
    records = tmp_path / "records"
    records.mkdir()
    inputs = {
        "domain": "review_fixture",
        "targets": [{"name": "subject"}],
        "partition_by": ["name"],
        "model": "fixture-model",
        "_reconciliation_cache": tmp_path / "cache",
        "reconciliation": {"enabled": True, "field_groups": [["answer", "qualifier"]]},
    }
    monkeypatch.setattr(
        "psweep.compilation.reconciliation.resolve_llm_kwargs",
        lambda *_args, **_kwargs: {"model": "fixture-model", "provider": "openai"},
    )
    monkeypatch.setattr(
        "psweep.compilation.reconciliation.build_llm_client",
        lambda *_args, **_kwargs: pytest.fail("review must not make provider calls"),
    )

    def write(name, url, value="same", qualifier="unit", source_bytes=None, unrelated=None):
        doc = tmp_path / f"{name}.txt"
        content = source_bytes if source_bytes is not None else f"document {name}".encode()
        doc.write_bytes(content)
        item = {
            "name": "subject",
            "answer": {"value": value, "quote": "source words"},
            "qualifier": qualifier,
        }
        if unrelated is not None:
            item["unrelated"] = unrelated
        payload = {"items": [item]}
        build_schema_validator(schema).validate(payload)
        context = SourceContext(url, "target", "1", "subject", {}) if url is not None else None
        result = SimpleNamespace(data=payload, coverage={}, validation_notes=[])
        record, _ = build_extraction_record(
            doc_path=doc,
            source_document_id=doc.name,
            source=context,
            result=result,
            model="fixture-model",
            provider="openai",
            schema_metadata=metadata,
            source_sha256=hashlib.sha256(content).hexdigest(),
            input_provenance={"input_mode": "text", "pages": [], "visual_pages": []},
        )
        record_validator().validate(record)
        (records / f"{name}.json").write_text(json.dumps(record))

    def compile_(reconciled, allow_model=False):
        resolved = deepcopy(inputs)
        resolved["reconciliation"]["enabled"] = reconciled
        compiler = DataCompiler(metadata, inputs=resolved, verbose=False)
        frame, _ = compiler.compile_from_directory(records, allow_model=allow_model)
        row = frame.iloc[0]
        return row, json.loads(row["evidence"])

    return write, compile_


@pytest.mark.parametrize("reconciled", [False, True], ids=["plain", "reconciled"])
@pytest.mark.parametrize(
    ("urls", "expected"),
    [
        (["https://example.org/a", None], 1),
        ([None, None], 0),
        (["", None], 0),
        (["https://example.org/a", "", None], 1),
        (["https://example.org/a", "https://example.org/a"], 1),
        (["https://example.org/a", "https://example.org/b"], 2),
    ],
    ids=["known+manual", "all-manual", "blank+null", "known+blank+null", "duplicate", "two-known"],
)
def test_support_counts_distinct_nonblank_urls(fixture, reconciled, urls, expected):
    write, compile_ = fixture
    for index, url in enumerate(urls):
        write(str(index), url)
    row, cells = compile_(reconciled)
    assert cells["answer"]["support"] == expected
    assert cells["answer"]["value"] == "same"  # URL-less evidence stays usable
    assert row["confidence"] != "none"


def test_plain_arbitration_counts_only_urls_agreeing_with_the_winner(fixture):
    write, compile_ = fixture
    write("a", "https://example.org/a", value="A")
    write("b", "https://example.org/b", value="A")
    write("c", "https://example.org/c", value="B")
    row, cells = compile_(False)
    assert cells["answer"]["value"] == "A"
    assert cells["answer"]["support"] == 2 and cells["answer"]["conflict"]
    assert row["confidence"] == "low"


def test_reconciliation_counts_the_atomic_unit_not_each_field(fixture):
    write, compile_ = fixture
    write("a", "https://example.org/a", value="same", qualifier="one")
    write("b", "https://example.org/b", value="same", qualifier="two")
    _, cells = compile_(True, False)
    # answer agrees across both URLs, but the unit (answer + qualifier) does not
    assert "answer" not in cells and "qualifier" not in cells


def test_content_deduplication_causes_mode_specific_counts(fixture):
    write, compile_ = fixture
    write("a", "https://example.org/a", source_bytes=b"identical content")
    write("b", "https://example.org/b", source_bytes=b"identical content")
    assert compile_(False)[1]["answer"]["support"] == 2
    assert compile_(True)[1]["answer"]["support"] == 1


def test_abstained_unit_leaves_row_low_despite_a_two_url_populated_cell(fixture):
    write, compile_ = fixture
    write("a", "https://example.org/a", value="A", qualifier="one", unrelated="agreed")
    write("b", "https://example.org/b", value="B", qualifier="two", unrelated="agreed")
    row, cells = compile_(True, False)
    assert "answer" not in cells
    assert cells["unrelated"]["support"] == 2 and "conflict" not in cells["unrelated"]
    assert row["confidence"] == "low"


def test_support_does_not_change_the_reconciliation_decision_identity(fixture):
    """Same observations, same cache key: only the published integer is recomputed."""
    from psweep.compilation import reconciliation

    assert reconciliation.VERSION == "2"


def test_roster_evidence_version_binds_compiled_table_freshness(tmp_path, monkeypatch):
    from psweep import pipeline
    from psweep.compilation import data_compiler

    extracted, compiled = tmp_path / "extracted", tmp_path / "compiled"
    extracted.mkdir()
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {"main_data_array": "items"},
                    "identity": {"deduplication": {"key_fields": ["name"]}},
                },
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "answer": {"type": "string"},
                            },
                        },
                    }
                },
            }
        )
    )
    config = tmp_path / "run.yaml"
    config.write_text(
        json.dumps(
            {
                "domain": "fixture",
                "schema": str(schema),
                "discovery": {
                    "targets": [{"name": "subject"}],
                    "partition_by": ["name"],
                    "queries": ["{name}"],
                },
            }
        )
    )
    (extracted / "record.json").write_text(
        json.dumps(
            {
                "document": {"source_url": "https://example.org/a", "source_scope": "target"},
                "payload": {"items": [{"name": "subject", "answer": "same"}]},
            }
        )
    )
    kwargs = {"config_path": config, "output_dir": compiled}
    pipeline.compile_extractions(extracted, schema, report_format="csv", **kwargs)
    assert pipeline.compiled_table_fresh(extracted, schema, **kwargs)
    monkeypatch.setattr(data_compiler, "ROSTER_EVIDENCE_VERSION", 2)
    assert not pipeline.compiled_table_fresh(extracted, schema, **kwargs)
    pipeline.compile_extractions(extracted, schema, report_format="csv", **kwargs)
    assert pipeline.compiled_table_fresh(extracted, schema, **kwargs)


def test_rows_mode_fingerprint_ignores_the_roster_evidence_version(fixture, tmp_path, monkeypatch):
    from psweep.compilation import data_compiler

    schema_path = tmp_path / "schema.json"
    compiler = DataCompiler(SchemaMetadata(schema_path), inputs={"domain": "x"}, verbose=False)
    records = tmp_path / "records"
    before = compiler.fingerprint(records)
    monkeypatch.setattr(data_compiler, "ROSTER_EVIDENCE_VERSION", 99)
    assert compiler.fingerprint(records) == before
