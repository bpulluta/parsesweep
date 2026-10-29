"""Resolved extraction schemas are enforced before a result can succeed."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from psweep.exceptions import SchemaValidationError
from psweep.extraction.document_extractor import DocumentExtractor
from psweep.utils.error_taxonomy import build_error_record
from psweep.utils.schema_metadata import SchemaMetadata
from psweep.pipeline import _extract_one_document


@pytest.fixture(autouse=True)
def controlled_configuration(monkeypatch):
    """Never consult the project's real .env while exercising extraction."""
    config = SimpleNamespace(llm_config={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": "fixture-key",
    })
    monkeypatch.setattr("psweep.utils.config._global_config", config)
    monkeypatch.setattr(
        "psweep.cli.commands_extract.load_dotenv", lambda: None,
    )


@pytest.fixture
def schema():
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": {"extraction": {
            "main_data_array": "items", "context_objects": ["source"],
            "identifier_fields": ["source.id"],
        }},
        "type": "object",
        "required": ["source", "items"],
        "properties": {
            "source": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string", "pattern": r"\S"},
                    "label": {"type": "string"},
                    "optional_nullable": {"type": ["string", "null"]},
                },
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["quote", "locator"],
                    "properties": {
                        "quote": {"type": "string", "pattern": r"\S"},
                        "locator": {"type": "string", "pattern": r"\S"},
                        "date": {"type": ["string", "null"], "format": "date"},
                        "count": {"type": "integer"},
                        "optional_nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            },
        },
    }


@pytest.fixture
def payload():
    return {"source": {"id": "one"}, "items": [{"quote": "Quoted evidence", "locator": "Page 1"}]}


def extract_reply(schema, payload):
    extractor = DocumentExtractor(api_key="test-key", model="gpt-4o-mini")
    with patch.object(extractor.client, "extract", return_value={"data": payload, "cost": 0.0}):
        return extractor.extract("sample document", schema)


@pytest.mark.parametrize("change", ["missing_required", "wrong_type", "blank_quote", "blank_locator", "invalid_date", "non_object"])
def test_contract_invalid_llm_payload_fails_before_success(schema, payload, change):
    if change == "missing_required":
        del payload["items"][0]["quote"]
    elif change == "wrong_type":
        payload["items"][0]["count"] = "1"
    elif change == "blank_quote":
        payload["items"][0]["quote"] = " \t\n "
    elif change == "blank_locator":
        payload["items"][0]["locator"] = " "
    elif change == "invalid_date":
        payload["items"][0]["date"] = "2024-02-30"
    else:
        payload = []
    with pytest.raises(SchemaValidationError) as exc:
        extract_reply(schema, payload)
    record = build_error_record(exc.value, stage="extraction")
    assert record["category"] == "schema_contract"
    assert record["code"] == "schema_validation_failed"
    assert record["retryable"] is False


def test_optional_missing_fields_respect_schema_nullability(schema, payload):
    result = extract_reply(schema, payload)
    assert result.data["source"]["optional_nullable"] is None
    assert result.data["items"][0]["optional_nullable"] is None
    assert result.data["items"][0]["date"] is None
    assert "label" not in result.data["source"]
    assert "count" not in result.data["items"][0]


def test_normalization_cannot_invalidate_success(schema, payload):
    schema["properties"]["source"]["properties"]["id"] = {"const": "A  B"}
    payload["source"]["id"] = "A  B"
    with pytest.raises(SchemaValidationError):
        extract_reply(schema, payload)


def test_invalid_schema_fails_before_llm_call(payload):
    extractor = DocumentExtractor(api_key="test-key", model="gpt-4o-mini")
    with patch.object(extractor.client, "extract") as client:
        with pytest.raises(SchemaValidationError):
            extractor.extract("document", {"type": "invented"})
    client.assert_not_called()


def test_resolved_schema_not_metadata_schema_controls_validation(schema, payload):
    extractor = DocumentExtractor(api_key="test-key", model="gpt-4o-mini")
    extractor.schema_metadata = type("Metadata", (), {"schema": {"type": "object"}})()
    del payload["source"]
    with patch.object(extractor.client, "extract", return_value={"data": payload, "cost": 0.0}):
        with pytest.raises(SchemaValidationError):
            extractor.extract("sample document", schema)


def test_valid_date_and_empty_array_are_allowed(schema, payload):
    payload["items"][0]["date"] = "2024-02-29"
    assert extract_reply(schema, payload).data["items"][0]["date"] == "2024-02-29"
    payload["items"] = []
    assert extract_reply(schema, payload).data["items"] == []


def test_schema_validation_does_not_verify_semantics(schema, payload):
    payload["items"][0]["quote"] = "This quotation has not been compared to source text."
    assert extract_reply(schema, payload).data["items"]


def test_nullable_reference_is_filled_without_inventing_nonnullable_values(schema, payload):
    schema["definitions"] = {
        "nullable_text": {"type": ["string", "null"]},
        "required_text": {"type": "string"},
    }
    properties = schema["properties"]["items"]["items"]["properties"]
    properties["optional_ref"] = {"$ref": "#/definitions/nullable_text"}
    properties["nonnullable_ref"] = {"$ref": "#/definitions/required_text"}
    result = extract_reply(schema, payload)
    assert result.data["items"][0]["optional_ref"] is None
    assert "nonnullable_ref" not in result.data["items"][0]


def test_workflow_document_failure_keeps_contract_taxonomy_and_no_record(
    tmp_path, schema, payload,
):
    """The shared worker never publishes contract-invalid payloads."""
    document = tmp_path / "document.txt"
    document.write_text("Document evidence")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema))
    metadata = SchemaMetadata(schema_path)
    extractor = DocumentExtractor(api_key="test-key", model="gpt-4o-mini")
    del payload["items"][0]["quote"]
    with patch.object(extractor.client, "extract", return_value={"data": payload, "cost": 0.0}):
        result = _extract_one_document(
            document, extractor=extractor, loaded_schema=schema,
            schema_metadata=metadata, schema_path=schema_path,
            output_path=tmp_path / "document.json", model="gpt-4o-mini",
            run_id="run://contract", provider="openai",
            input_fingerprint="a" * 64,
        )
    assert result["success"] is False
    assert result["errors"][0]["category"] == "schema_contract"
    assert result["errors"][0]["code"] == "schema_validation_failed"
    assert not (tmp_path / "document.json").exists()


@pytest.mark.parametrize("command", ["extract", "run"])
def test_invalid_schema_preflight_preserves_fresh_output(tmp_path, schema, command, monkeypatch):
    import yaml
    from click.testing import CliRunner
    from psweep.cli.main import cli

    invalid = dict(schema)
    invalid["type"] = "invalid_type"
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(invalid))
    valid_compile_schema = tmp_path / "compile.json"
    from _helpers import synthetic_evidence_schema

    valid_compile_schema.write_text(json.dumps(synthetic_evidence_schema()))
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.json"
    sentinel.write_text("preserve previous output")
    config = tmp_path / "run.yaml"
    config.write_text(yaml.safe_dump({"domain": "synthetic", "extraction": {
        "schema": str(path), "input_dir": str(tmp_path / "missing"), "output_dir": str(output),
    }, "compilation": {"schema": str(valid_compile_schema), "input_dir": str(output)}}))
    provider = Mock(side_effect=AssertionError("must not contact provider"))
    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", provider)
    result = CliRunner().invoke(cli, [command, "--config", str(config), "--fresh"])
    assert result.exit_code != 0
    assert "schema" in result.output.lower()
    assert sentinel.read_text() == "preserve previous output"
    provider.assert_not_called()
    validation = CliRunner().invoke(cli, [command, "--config", str(config), "--validate-config"])
    assert validation.exit_code != 0


def test_context_truncation_survives_canonical_record(tmp_path, schema, payload):
    from psweep.extraction.record_writer import build_extraction_record

    extractor = DocumentExtractor(api_key="test-key", model="gpt-4o-mini", max_context_chars=5)
    with patch.object(extractor.client, "extract", return_value={"data": payload, "cost": 0.0}) as client:
        result = extractor.extract("long source text", schema)
    assert client.call_args.args[0] == "long "
    assert any("truncated" in note for note in result.validation_notes)
    document = tmp_path / "doc.txt"
    document.write_text("long source text")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema))
    record, count = build_extraction_record(
        doc_path=document, result=result, model="gpt-4o-mini",
        provider="openai",
        schema_metadata=SchemaMetadata(schema_path),
    )
    assert count == record["item_count"] == 1
    assert record["processing_metrics"]["coverage"] == {
        "input_characters": 16, "extracted_characters": 5, "context_truncated": True,
    }
    assert any("truncated" in note for note in record["quality"]["warnings"])


@pytest.mark.parametrize("shape", ["equipment", "evidence"])
def test_mocked_page_limited_extract_compiles_without_private_inputs(tmp_path, monkeypatch, shape):
    from copy import deepcopy

    import pandas as pd

    from _helpers import synthetic_evidence_schema
    from psweep.pipeline import compile_extractions, extract_documents

    if shape == "evidence":
        domain_schema = synthetic_evidence_schema()
        data = {
            "source": {"url": "https://example.org/statement", "record_kind": "statement"},
            "comments": [{
                "type": "statement", "summary": "Asks about timing.",
                "evidence_quote": "When will it begin?", "evidence_locator": "Page 5",
                "project_name": "Example Project", "topics": ["timing"],
                "sentiment": "not_stated",
            }],
        }
    else:
        domain_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$metadata": {
                "extraction": {"main_data_array": "observations", "context_objects": ["catalog"], "identifier_fields": ["catalog.url"]},
                "identity": {"deduplication": {"key_fields": ["serial"], "partition_fields": ["catalog.url"]}},
            },
            "type": "object",
            "properties": {
                "catalog": {"type": "object", "properties": {"url": {"type": "string"}}},
                "observations": {"type": "array", "items": {"type": "object", "properties": {
                    "serial": {"type": "string"}, "kind": {"type": "string"},
                    "summary": {"type": "string"}, "quotation": {"type": "string"},
                    "measurements": {"type": "array", "items": {"type": "number"}},
                }}},
            },
        }
        data = {"catalog": {"url": "https://example.org/catalog"}, "observations": [{
            "serial": "eq-01", "kind": "equipment", "summary": "Observed equipment.",
            "quotation": "Serial eq-01", "measurements": [2.5, 3.5],
        }]}
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(domain_schema))
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "document.pdf").write_text("synthetic placeholder")
    pages_csv = tmp_path / "pages.csv"
    pages_csv.write_text("file_path,start_page,end_page\ndocument.pdf,5,9\n")
    extracted = tmp_path / "extracted"
    seen = []

    def parse_document(_doc, *, page_range=None, return_meta=False, **_kwargs):
        seen.append(page_range)
        text = "Source body"
        return (text, {"page_range": page_range}) if return_meta else text

    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document",
        parse_document,
    )
    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", lambda *args: {"data": deepcopy(data), "cost": 0.0})
    run = extract_documents(documents, schema_path, output_dir=extracted, provider="openai", pages_csv=pages_csv)
    assert run.successful == 1 and run.failed == 0
    assert seen == [(5, 9)]
    record = json.loads((extracted / "document.json").read_text())
    assert record["processing_metrics"]["coverage"]["selected_page_range"] == [5, 9]
    compiled = compile_extractions(extracted, schema_path, output_dir=tmp_path / "compiled", report_format="both")
    assert len(compiled.output_files) == 2
    frame = pd.read_csv(next(path for path in compiled.output_files if path.suffix == ".csv"), dtype=str)
    workbook = pd.read_excel(next(path for path in compiled.output_files if path.suffix == ".xlsx"), dtype=str)
    assert len(frame) == 1
    assert frame.columns.tolist() == workbook.columns.tolist()
    assert any("selected pages 5-9" in warning for warning in record["quality"]["warnings"])
    if shape == "equipment":
        assert "sentiment" not in frame and "project_name" not in frame
        assert frame.iloc[0]["serial"] == "eq-01"
        assert frame.iloc[0]["measurements"] == "2.5, 3.5"
    else:
        assert frame.iloc[0]["sentiment"] == "not_stated"
        assert frame.iloc[0]["evidence_quote"] == "When will it begin?"
    assert "fips" not in frame


@pytest.mark.parametrize("scope", ["pages", "sections"])
def test_workflow_selected_input_scope_survives_record(
    tmp_path, schema, payload, scope,
):
    """Selected pages and sections retain limitations and source text."""
    document = tmp_path / "document.txt"
    document.write_text("Complete source document")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema))
    metadata = SchemaMetadata(schema_path)
    extractor = DocumentExtractor(api_key="test-key", model="gpt-4o-mini")
    with patch.object(
        extractor.client, "extract",
        return_value={"data": payload, "cost": 0.0},
    ) as client:
        result = _extract_one_document(
            document, extractor=extractor, loaded_schema=schema,
            schema_metadata=metadata, schema_path=schema_path,
            output_path=tmp_path / "document.json", model="gpt-4o-mini",
            run_id="run://contract", provider="openai",
            input_fingerprint="a" * 64,
            page_range=(5, 9) if scope == "pages" else None,
            section_text="Selected section" if scope == "sections" else None,
        )
    expected_text = (
        "Selected section" if scope == "sections"
        else "Complete source document"
    )
    assert client.call_args.args[0] == expected_text
    assert result["success"] is True
    record = json.loads((tmp_path / "document.json").read_text())
    coverage = record["processing_metrics"]["coverage"]
    assert coverage == result["coverage"]
    if scope == "pages":
        assert coverage["selected_page_range"] == [5, 9]
    else:
        assert coverage["section_filtered"] is True
    assert any("not a full-document" in warning for warning in record["quality"]["warnings"])


def test_pipeline_does_not_write_invalid_extraction(tmp_path, monkeypatch, schema):
    from psweep.pipeline import extract_documents

    schema["$metadata"] = {"extraction": {"main_data_array": "items", "identifier_fields": ["source.id"]}}
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema))
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "doc.txt").write_text("Source text")
    output = tmp_path / "output"
    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", lambda *args: {"data": {"items": []}, "cost": 0.0})
    result = extract_documents(documents, schema_path, output_dir=output, provider="openai")
    assert result.failed == 1
    assert result.successful == 0
    assert not (output / "doc.json").exists()
    assert "schema" in result.errors[0]["error"].lower()
