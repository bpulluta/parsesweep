"""Tests for the `extract` audit cleanup (domain-neutralization + dedup).

Covers the behavior introduced/changed by the process module audit:
- domain-neutral `detect_provider` (no hardcoded customer deployment prefix)
- the shared `_extract_one_document` helper (success + failure record shapes)
- generic `--filter` index filtering with back-compat state/jurisdiction aliases
- schema-driven identifier fields (neutral default, no hardcoded domain term)
- model-aware cost via the single shared pricing DB
- token usage surfaced from the LLM client
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import json

from psweep.extraction.llm_factory import detect_provider
from psweep.extraction.llm_client import LLMClient
from psweep.cli.commands import (
    _extract_one_document,
    _build_index_filters,
    _row_matches_filters,
    _extract_and_save_result,
)


# ── detect_provider (domain-neutral) ────────────────────────────────────────


def test_detect_provider_has_no_hardcoded_customer_prefix():
    # The old code special-cased a specific customer deployment prefix; a bare
    # deployment name must NOT be sniffed as azure — provider is set explicitly.
    assert detect_provider("compassop-gpt-4.1-mini") == "openai"
    assert detect_provider("my-azure-deployment") == "openai"


def test_detect_provider_recognizes_canonical_markers():
    assert detect_provider("azure/anything") == "azure"
    assert detect_provider("claude-opus-4.5") == "anthropic"
    assert detect_provider("gemini-3-pro") == "gemini"
    assert detect_provider("gpt-5") == "openai"
    assert detect_provider("mistral-large-2") == "mistral"


# ── generic index filters ───────────────────────────────────────────────────


def test_generic_filter_parses_column_value_pairs():
    filters = _build_index_filters(["source_state=CA", "domain=Tariffs"])
    assert ("source_state", "ca") in filters
    assert ("domain", "tariffs") in filters


def test_state_and_jurisdiction_aliases_map_onto_generic_filter():
    filters = _build_index_filters(
        [], filter_state="CA", filter_jurisdiction="Imperial County"
    )
    assert ("source_state", "ca") in filters
    assert ("source_jurisdiction", "imperial-county") in filters


def test_row_matches_filters_is_case_and_format_insensitive():
    filters = _build_index_filters(
        [], filter_jurisdiction="Imperial County"
    )
    assert _row_matches_filters(
        {"source_jurisdiction": "imperial-county"}, filters
    )
    assert _row_matches_filters(
        {"source_jurisdiction": "Imperial County"}, filters
    )
    assert not _row_matches_filters(
        {"source_jurisdiction": "san-diego"}, filters
    )


# ── schema-driven identifier fields ─────────────────────────────────────────


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


def test_identifier_neutral_default_ignores_domain_terms(tmp_path):
    # Without a schema-supplied identifier, a top-level 'jurisdiction' field is
    # NOT treated as the identifier (no hardcoded domain term) -> falls back to
    # the file stem.
    result = _make_result(
        {"metadata": {"jurisdiction": "Example County"}, "items": [{"a": 1}]}
    )
    _extract_and_save_result(
        Path("doc.pdf"), result, tmp_path, "cat", "gpt-5", False
    )
    saved = json.loads((tmp_path / "doc.json").read_text(encoding="utf-8"))
    assert saved["document"]["source_document_id"] == "doc"


def test_identifier_uses_schema_supplied_fields(tmp_path):
    result = _make_result(
        {"metadata": {"jurisdiction": "Example County"}, "items": [{"a": 1}]}
    )
    _extract_and_save_result(
        Path("doc.pdf"),
        result,
        tmp_path,
        "cat",
        "gpt-5",
        False,
        identifier_fields=["jurisdiction"],
    )
    saved = json.loads((tmp_path / "doc.json").read_text(encoding="utf-8"))
    assert saved["document"]["source_document_id"] == "Example County"


# ── _extract_one_document (shared per-document core) ─────────────────────────


def test_extract_one_document_success_shape(tmp_path):
    doc = tmp_path / "sample.txt"
    doc.write_text("hello world", encoding="utf-8")

    extractor = MagicMock()
    extractor.extract.return_value = _make_result(
        {"items": [{"x": 1}, {"x": 2}]}
    )

    res = _extract_one_document(
        doc,
        extractor=extractor,
        loaded_schema={"type": "object", "properties": {}},
        page_range_map={},
        section_text_map={},
        source_context_map={},
        file_output_dirs={},
        output_dir=tmp_path,
        category="cat",
        actual_model="gpt-5",
        enable_validation=False,
        runtime_artifact=None,
        run_id="run://abc",
        provider="openai",
        schema_path=tmp_path / "schema.json",
    )

    assert res["success"] is True
    assert res["file"] == "sample.txt"
    assert res["items"] == 2
    assert res["input_tokens"] == 10
    assert res["output_tokens"] == 20
    assert (tmp_path / "sample.json").exists()


def test_extract_one_document_failure_shape(tmp_path):
    doc = tmp_path / "sample.txt"
    doc.write_text("hello world", encoding="utf-8")

    extractor = MagicMock()
    extractor.extract.side_effect = RuntimeError("boom")

    res = _extract_one_document(
        doc,
        extractor=extractor,
        loaded_schema={"type": "object", "properties": {}},
        page_range_map={},
        section_text_map={},
        source_context_map={},
        file_output_dirs={},
        output_dir=tmp_path,
        category="cat",
        actual_model="gpt-5",
        enable_validation=False,
        runtime_artifact=None,
        run_id="run://abc",
        provider="openai",
        schema_path=tmp_path / "schema.json",
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

    with patch(
        "psweep.extraction.llm_client.completion",
        return_value=fake_response,
    ), patch(
        "psweep.extraction.llm_client.completion_cost",
        return_value=0.002,
    ):
        out = client.extract("text", {"type": "object", "properties": {}})

    assert out["input_tokens"] == 123
    assert out["output_tokens"] == 45
    assert out["cost"] == 0.002
