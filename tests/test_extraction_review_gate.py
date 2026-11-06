"""Regression gates for independent Phase 2 integration findings."""

import csv
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, Mock

import pytest

from psweep import pipeline
from psweep.extraction import schema_utils
from psweep.extraction.document_utils import write_text_cache
from psweep.extraction.page_locator import PageLocator, PageSelection
from psweep.pipeline import _execute_extraction, _prepare_extraction
from psweep.utils.config import Config
from psweep.utils.log_context import current_document
from psweep.utils.page_range import load_pages_csv, page_numbers


@pytest.fixture
def reviewed_inputs(tmp_path, monkeypatch):
    """Isolate source, contract, credentials, and every provider call."""
    source = tmp_path / "documents"
    source.mkdir()
    (source / "doc.txt").write_text("Source evidence", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {
                    "extraction": {
                        "main_data_array": "items",
                    }
                },
                "type": "object",
                "required": ["source", "items"],
                "properties": {
                    "source": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                    },
                    "items": {"type": "array", "items": {"type": "object"}},
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = {"provider": "openai", "model": "gpt-fixture", "api_key": "fixture"}
    monkeypatch.setattr(
        "psweep.utils.config.get_config",
        lambda: SimpleNamespace(llm_config=cfg),
    )
    provider = Mock(
        return_value={
            "data": {"source": {"id": "fixture"}, "items": []},
            "cost": 0.01,
            "input_tokens": 12,
            "output_tokens": 4,
        }
    )
    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", provider)
    inputs = {
        "path": str(source),
        "schema": str(schema),
        "output": str(tmp_path / "records"),
        "llm_config": cfg,
    }
    return inputs, provider


def test_record_contract_does_not_depend_on_repository_path(monkeypatch, tmp_path):
    """Validate installed records away from a source checkout."""
    schema_utils.record_validator.cache_clear()
    monkeypatch.setattr(
        schema_utils,
        "__file__",
        str(tmp_path / "site-packages/psweep/extraction/schema_utils.py"),
    )
    try:
        validator = schema_utils.record_validator()
        assert validator.schema["properties"]["contract_version"]["const"] == "2.0.0"
    finally:
        schema_utils.record_validator.cache_clear()


def test_proxy_with_explicit_azure_needs_no_direct_azure_endpoint(
    reviewed_inputs,
):
    """Accept proxy credentials despite an explicit provider override."""
    inputs, provider = reviewed_inputs
    inputs.update(
        {
            "provider": "azure",
            "llm_config": {
                "provider": "openai",
                "model": "deployment",
                "api_key": "fixture",
                "base_url": "https://proxy.example/v1",
            },
        }
    )
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert outcome.exit_code == 0
    provider.assert_called_once()


def test_document_work_logs_name_the_document(reviewed_inputs, monkeypatch):
    """Locating (main thread) and extracting (worker) both log under the document."""
    inputs, provider = reviewed_inputs
    inputs["page_targeting"] = {"enabled": True, "section_description": "evidence"}
    seen = []
    monkeypatch.setattr(
        "psweep.extraction.section_locator.SectionLocator.locate",
        lambda _self, _doc: seen.append(("locate", current_document())),
    )

    def extract(*_args, **_kwargs):
        seen.append(("extract", current_document()))
        return DEFAULT

    provider.side_effect = extract
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert seen == [("locate", "doc.txt"), ("extract", "doc.txt")]
    assert current_document() is None


def test_invalid_locator_keywords_preserve_fresh_output(reviewed_inputs):
    """A locator setup typo is rejected before destructive freshness work."""
    inputs, provider = reviewed_inputs
    output = Path(inputs["output"])
    output.mkdir()
    sentinel = output / "prior.json"
    sentinel.write_text("prior record", encoding="utf-8")
    inputs["page_targeting"] = {
        "enabled": True,
        "section_description": "evidence",
        "keywords": [123],
    }
    with pytest.raises(ValueError, match="keywords"):
        plan = _prepare_extraction(inputs, fresh=True)
        _execute_extraction(plan)
    assert sentinel.read_text(encoding="utf-8") == "prior record"
    provider.assert_not_called()


def test_project_relative_page_mapping_is_preserved(reviewed_inputs, monkeypatch):
    """Preserve CSV paths relative to the current directory."""
    inputs, _provider = reviewed_inputs
    root = Path(inputs["path"]).parent
    monkeypatch.chdir(root)
    (Path(inputs["path"]) / "doc.txt").unlink()
    source = Path(inputs["path"]) / "doc.pdf"
    source.write_bytes(b"fixture pdf")
    pages = root / "pages.csv"
    pages.write_text(
        "file_path,start_page,end_page\ndocuments/doc.pdf,5,9\n",
        encoding="utf-8",
    )
    inputs["pages_csv"] = str(pages)
    plan = _prepare_extraction(inputs)
    assert plan["ranges"][source] == ((5, 9),)


@pytest.mark.parametrize("kind", ["manual", "unsupported", "cached", "small"])
def test_locator_credentials_required_only_for_actual_calls(reviewed_inputs, monkeypatch, kind):
    """Skip locator credentials when no provider call is needed."""

    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    inputs["page_targeting"] = {
        "enabled": True,
        "model": "claude-locator",
        "section_description": "evidence",
        "keywords": ["evidence"],
        "trigger_chars": 1000,
    }
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if kind in {"manual", "cached"}:
        (root / "doc.txt").unlink()
        source = root / "doc.pdf"
        source.write_bytes(b"fixture")
        monkeypatch.setattr(
            "psweep.extraction.document_utils.extract_text_from_document",
            lambda *_args, **kw: (
                "Source evidence",
                {
                    "page_ranges": ((1, 1),),
                    "page_count": 1,
                    "selected_pages": [1],
                    "extracted_pages": [1],
                },
            )
            if kw.get("return_meta")
            else "Source evidence",
        )
        if kind == "manual":
            inputs["pages"] = "1-1"
        else:
            locator = PageLocator(
                "evidence",
                model="claude-locator",
                keywords=["evidence"],
                trigger_chars=1000,
            )
            locator._write_cache(source, PageSelection(((1, 1),)), 1)
    elif kind == "unsupported":
        (root / "doc.txt").rename(root / "doc.csv")
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert outcome.exit_code == 0
    provider.assert_called_once()


def test_locator_routing_change_invalidates_record_reuse(reviewed_inputs, monkeypatch):
    """Bind targeted reuse to effective locator routing."""

    inputs, provider = reviewed_inputs
    inputs["page_targeting"] = {
        "enabled": True,
        "model": "azure/locator",
        "section_description": "evidence",
        "trigger_chars": 1000,
    }
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "fixture")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://first.openai.azure.com")
    monkeypatch.setattr(
        pipeline,
        "_target_extraction_inputs",
        lambda plan: (plan["ranges"], {}, []),
    )
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://second.openai.azure.com")
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert provider.call_count == 2


def test_saved_page_ranges_cover_reused_records(reviewed_inputs, monkeypatch):
    """The review file lists the ranges behind every current record, reused or new."""
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "doc.txt").unlink()
    inputs["page_targeting"] = {
        "enabled": True,
        "section_description": "evidence",
        "save_discovered": True,
    }
    found = {"a.pdf": ((3, 4),), "b.pdf": ((2, 2), (7, 9)), "c.pdf": None}
    located = []

    def locate(_self, doc):
        located.append(doc.name)
        return PageSelection(found[doc.name]) if found[doc.name] else None

    def parse(_doc, page_ranges=None, **_kwargs):
        selected = page_numbers(page_ranges or ((1, 9),))
        return "Source evidence", {
            "page_count": 9,
            "page_ranges": page_ranges or ((1, 9),),
            "selected_pages": selected,
            "extracted_pages": selected,
        }

    monkeypatch.setattr("psweep.extraction.page_locator.PageLocator.locate", locate)
    monkeypatch.setattr("psweep.extraction.document_utils.extract_text_from_document", parse)
    saved = Path(inputs["output"]) / "discovered_page_ranges.csv"

    def run():
        assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
        with saved.open(encoding="utf-8", newline="") as handle:
            return [
                (Path(row["file_path"]).name, int(row["start_page"]), int(row["end_page"]))
                for row in csv.DictReader(handle)
            ]

    (root / "a.pdf").write_bytes(b"first")
    assert run() == [("a.pdf", 3, 4)]
    (root / "b.pdf").write_bytes(b"second")
    assert run() == [("a.pdf", 3, 4), ("b.pdf", 2, 2), ("b.pdf", 7, 9)]
    assert list(load_pages_csv(saved).values()) == [((3, 4),), ((2, 2), (7, 9))]
    (root / "b.pdf").unlink()
    assert run() == [("a.pdf", 3, 4)]
    (root / "a.pdf").unlink()
    (root / "c.pdf").write_bytes(b"third")
    assert run() == []
    assert located == ["a.pdf", "b.pdf", "c.pdf"]
    assert provider.call_count == 3


def test_pages_left_out_by_the_budget_are_recorded(reviewed_inputs, monkeypatch):
    """Relevant pages the budget dropped stay visible in the record."""
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "doc.txt").unlink()
    (root / "doc.pdf").write_bytes(b"fixture")
    inputs["page_targeting"] = {"enabled": True, "section_description": "evidence"}
    monkeypatch.setattr(
        "psweep.extraction.page_locator.PageLocator.locate",
        lambda _self, _doc: PageSelection(((2, 2), (5, 6)), omitted=((8, 9),)),
    )
    seen = []

    def parse(_doc, page_ranges=None, **_kwargs):
        seen.append(page_ranges)
        return "Source evidence", {
            "page_count": 9,
            "page_ranges": page_ranges,
            "selected_pages": page_numbers(page_ranges),
            "extracted_pages": page_numbers(page_ranges),
        }

    monkeypatch.setattr("psweep.extraction.document_utils.extract_text_from_document", parse)
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert seen == [((2, 2), (5, 6))]
    record = json.loads((Path(inputs["output"]) / "doc.json").read_text(encoding="utf-8"))
    coverage = record["processing_metrics"]["coverage"]
    assert coverage["selected_page_ranges"] == [[2, 2], [5, 6]]
    assert coverage["omitted_page_ranges"] == [[8, 9]]
    assert coverage["parser_partial"] is False
    assert any("left out relevant pages 8-9" in note for note in record["quality"]["warnings"])
    provider.assert_called_once()


def test_records_of_removed_documents_are_withdrawn(reviewed_inputs, tmp_path):
    """A record whose document left the input folder is withdrawn; others survive."""
    inputs, _provider = reviewed_inputs
    root = Path(inputs["path"])
    output = Path(inputs["output"]).resolve()
    (root / "gone.txt").write_text("Gone evidence", encoding="utf-8")
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    single = {**inputs, "path": str(root / "doc.txt")}
    assert _execute_extraction(_prepare_extraction(single)).withdrawn == []
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "other.txt").write_text("Other evidence", encoding="utf-8")
    assert _execute_extraction(_prepare_extraction({**inputs, "path": str(foreign)})).exit_code == 0
    (root / "gone.txt").unlink()
    (foreign / "other.txt").unlink()
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert outcome.exit_code == 0
    assert outcome.withdrawn == [output / "gone.json"]
    assert sorted(path.name for path in output.glob("*.json")) == ["doc.json", "other.json"]
    manifest = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))
    assert manifest["withdrawn"] == [str(output / "gone.json")]


def test_skipped_ocr_pages_survive_record_and_do_not_reuse(reviewed_inputs, monkeypatch):
    """Keep missing OCR pages visible and ineligible for reuse."""
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "doc.txt").unlink()
    (root / "doc.pdf").write_bytes(b"fixture")
    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document",
        lambda *_args, **_kwargs: (
            "Pages one and three evidence",
            {
                "page_count": 3,
                "page_ranges": ((1, 3),),
                "selected_pages": [1, 2, 3],
                "extracted_pages": [1, 3],
            },
        ),
    )
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    record = json.loads((Path(inputs["output"]) / "doc.json").read_text(encoding="utf-8"))
    coverage = record["processing_metrics"]["coverage"]
    assert coverage["page_count"] == 3
    assert coverage["extracted_pages"] == [1, 3]
    assert coverage["parser_partial"] is True
    assert any(
        "page" in warning.lower() and "missing" in warning.lower()
        for warning in record["quality"]["warnings"]
    )
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert provider.call_count == 2


def test_needed_locator_key_fails_before_fresh_reset(reviewed_inputs, monkeypatch):
    """Preserve output when a required locator has no credentials."""
    inputs, provider = reviewed_inputs
    source = Path(inputs["path"]) / "doc.txt"
    source.write_text("evidence " * 500, encoding="utf-8")
    output = Path(inputs["output"])
    output.mkdir()
    sentinel = output / "prior.json"
    sentinel.write_text("prior output", encoding="utf-8")
    inputs["page_targeting"] = {
        "enabled": True,
        "model": "claude-locator",
        "section_description": "evidence",
        "keywords": ["evidence"],
        "trigger_chars": 10,
    }
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(Exception, match="API credentials"):
        _execute_extraction(_prepare_extraction(inputs, fresh=True))
    assert sentinel.read_text(encoding="utf-8") == "prior output"
    provider.assert_not_called()


def test_partitioned_outputs_are_stable_and_reused(reviewed_inputs):
    """Partitioned inputs keep their output identity and verified reuse."""

    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "doc.txt").unlink()
    for label in ("first", "second"):
        doc = root / label / "report.txt"
        doc.parent.mkdir()
        doc.write_text("Source evidence", encoding="utf-8")
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert provider.call_count == 2
    provider.reset_mock()
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert not (Path(inputs["output"]) / "report.json").exists()
    assert (Path(inputs["output"]) / "first/report.json").is_file()
    provider.assert_not_called()


def test_replaced_authoritative_text_invalidates_reuse(reviewed_inputs):
    """Invalidate reuse when authoritative rendered text changes."""

    inputs, provider = reviewed_inputs
    document = Path(inputs["path"]) / "doc.txt"
    write_text_cache(document, "Original rendered source", method="browser_render")
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    write_text_cache(document, "Changed rendered source", method="browser_render")
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert provider.call_count == 2


def test_reuse_manifests_do_not_copy_all_prior_proof(reviewed_inputs):
    """One immutable proof per published record bounds resumed-run growth."""
    inputs, _provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "second.txt").write_text("Source second", encoding="utf-8")
    inputs["limit"] = 1
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    manifests = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (Path(inputs["output"]) / "run_manifests").glob("*.json")
    ]
    assert sum(len(m["reuse"]) for m in manifests) == 2


def test_single_source_file_allows_descendant_output(reviewed_inputs):
    """Single-file mode does not inventory the entire source parent."""
    inputs, _provider = reviewed_inputs
    root = Path(inputs["path"])
    inputs.update(path=str(root / "doc.txt"), output=str(root / "records"))
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0


def test_main_array_typo_preserves_fresh_output(reviewed_inputs):
    """Reject invalid schema metadata before output deletion or calls."""
    inputs, provider = reviewed_inputs
    schema = Path(inputs["schema"])
    contract = json.loads(schema.read_text(encoding="utf-8"))
    contract["$metadata"]["extraction"]["main_data_array"] = "item_typo"
    schema.write_text(json.dumps(contract), encoding="utf-8")
    output = Path(inputs["output"])
    output.mkdir()
    sentinel = output / "prior.json"
    sentinel.write_text("prior", encoding="utf-8")
    with pytest.raises(Exception, match="item_typo"):
        _execute_extraction(_prepare_extraction(inputs, fresh=True))
    assert sentinel.read_text(encoding="utf-8") == "prior"
    provider.assert_not_called()


@pytest.mark.parametrize("reserved", ["validation", "run_manifests", "qa_qc"])
def test_reserved_record_partitions_fail_preflight(reviewed_inputs, reserved):
    """Reject output partitions reserved for internal products."""
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    document = root / reserved / "source.txt"
    document.parent.mkdir()
    document.write_text("source", encoding="utf-8")
    with pytest.raises(ValueError, match="reserved"):
        _execute_extraction(_prepare_extraction(inputs))
    provider.assert_not_called()


def test_derived_raw_ocr_cache_does_not_invalidate_reuse(reviewed_inputs, monkeypatch):
    """Reuse unchanged inputs after parsing populates derived OCR."""

    inputs, provider = reviewed_inputs
    document = Path(inputs["path"]) / "doc.txt"

    def parse(*_args, **_kwargs):
        write_text_cache(document, "raw OCR evidence", method="ocr")
        return "raw OCR evidence"

    monkeypatch.setattr("psweep.extraction.document_utils.extract_text_from_document", parse)
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert provider.call_count == 1


def test_env_routing_survives_removing_only_credentials(tmp_path, monkeypatch):
    """Non-secret model/endpoint settings still identify reusable outputs."""

    for key in (
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_MODEL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-nondefault")
    before = Config(project_root=tmp_path).llm_config
    monkeypatch.delenv("OPENAI_API_KEY")
    after = Config(project_root=tmp_path).llm_config
    assert (after["model"], after["provider"]) == (
        before["model"],
        before["provider"],
    )
    assert not after["api_key"]


def test_output_partition_symlink_escape_rejected(reviewed_inputs):
    """An output parent symlink cannot publish outside the selected corpus."""
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    nested = root / "nested/source.txt"
    nested.parent.mkdir()
    nested.write_text("source", encoding="utf-8")
    output = Path(inputs["output"])
    output.mkdir()
    outside = root.parent / "outside"
    outside.mkdir()
    sentinel = outside / "source.json"
    sentinel.write_text("protected", encoding="utf-8")
    (output / "nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="output"):
        _prepare_extraction(inputs)
    assert sentinel.read_text(encoding="utf-8") == "protected"
    provider.assert_not_called()


@pytest.mark.parametrize("composition", ["anyOf", "allOf"])
def test_composed_main_array_schema_is_supported(reviewed_inputs, composition):
    """Accept composed array schemas without a direct type declaration."""
    inputs, _provider = reviewed_inputs
    path = Path(inputs["schema"])
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["properties"]["items"] = {composition: [{"type": "array", "items": {"type": "object"}}]}
    path.write_text(json.dumps(schema), encoding="utf-8")
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
