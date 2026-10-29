"""Regression gates for independent Phase 2 integration findings."""

import builtins
import csv
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from psweep import pipeline
from psweep.cli.commands_extract import _run_validation_extraction
from psweep.cli.main import cli
from psweep.config.model_registry import ModelRegistry
from psweep.exceptions import SchemaValidationError
from psweep.extraction import schema_utils
from psweep.extraction.document_utils import write_text_cache
from psweep.extraction.page_locator import PageLocator
from psweep.pipeline import _execute_extraction, _prepare_extraction
from psweep.utils.config import Config
from psweep.validation.multi_model_extractor import run_multi_model_extraction


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
                        "identifier_fields": ["source.id"],
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
    monkeypatch.setattr(
        "psweep.extraction.llm_client.LLMClient.extract", provider
    )
    inputs = {
        "path": str(source),
        "schema": str(schema),
        "output": str(tmp_path / "records"),
        "llm_config": cfg,
    }
    return inputs, provider


def test_record_contract_does_not_depend_on_repository_path(
    monkeypatch, tmp_path
):
    """Validate installed records away from a source checkout."""
    schema_utils.record_validator.cache_clear()
    monkeypatch.setattr(
        schema_utils,
        "__file__",
        str(tmp_path / "site-packages/psweep/extraction/schema_utils.py"),
    )
    try:
        validator = schema_utils.record_validator()
        assert (
            validator.schema["properties"]["contract_version"]["const"]
            == "1.0.0"
        )
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


def test_project_relative_page_mapping_is_preserved(
    reviewed_inputs, monkeypatch
):
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
    assert plan["ranges"][source] == (5, 9)


@pytest.mark.parametrize("kind", ["manual", "unsupported", "cached", "small"])
def test_locator_credentials_required_only_for_actual_calls(
    reviewed_inputs, monkeypatch, kind
):
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
                    "page_range": (1, 1),
                    "page_count": 1,
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
            locator._write_cache(source, (1, 1), "fixture", 1)
    elif kind == "unsupported":
        (root / "doc.txt").rename(root / "doc.csv")
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert outcome.exit_code == 0
    provider.assert_called_once()


def test_locator_routing_change_invalidates_record_reuse(
    reviewed_inputs, monkeypatch
):
    """Bind targeted reuse to effective locator routing."""

    inputs, provider = reviewed_inputs
    inputs["page_targeting"] = {
        "enabled": True,
        "model": "azure/locator",
        "section_description": "evidence",
        "trigger_chars": 1000,
    }
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "fixture")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT", "https://first.openai.azure.com"
    )
    monkeypatch.setattr(
        pipeline,
        "_target_extraction_inputs",
        lambda plan: (plan["ranges"], {}, []),
    )
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT", "https://second.openai.azure.com"
    )
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert provider.call_count == 2


def test_skipped_ocr_pages_survive_record_and_do_not_reuse(
    reviewed_inputs, monkeypatch
):
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
                "page_range": (1, 3),
                "extracted_pages": [1, 3],
            },
        ),
    )
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    record = json.loads(
        (Path(inputs["output"]) / "doc.json").read_text(encoding="utf-8")
    )
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


def test_unknown_cost_success_remains_successful_for_qaqc(reviewed_inputs):
    """Keep successful QA payloads valid when pricing is unavailable."""
    inputs, provider = reviewed_inputs
    provider.return_value["cost"] = None
    results = run_multi_model_extraction(
        doc_text="Source evidence",
        doc_name="doc",
        schema=json.loads(Path(inputs["schema"]).read_text(encoding="utf-8")),
        registry=ModelRegistry(llm_config=inputs["llm_config"]),
        model_tiers=["gpt-fixture"],
        output_dir=Path(inputs["output"]),
    )
    assert results["gpt-fixture"].success
    assert results["gpt-fixture"].cost is None
    assert results["gpt-fixture"].input_tokens == 12
    meta = json.loads(
        (Path(inputs["output"]) / "validation/doc/metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert meta["summary"]["unknown_cost_models"] == 1


def test_needed_locator_key_fails_before_fresh_reset(
    reviewed_inputs, monkeypatch
):
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


def test_filtered_index_has_stable_outputs(reviewed_inputs):
    """Selecting one partition keeps its output identity and verified reuse."""

    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "doc.txt").unlink()
    rows = []
    for label in ("first", "second"):
        doc = root / label / "report.txt"
        doc.parent.mkdir()
        doc.write_text("Source evidence", encoding="utf-8")
        rows.append(
            {
                "status": "downloaded",
                "path": str(doc),
                "relative_path": f"{label}/report.txt",
                "label": label,
            }
        )
    index = root.parent / "index.csv"
    with index.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    assert (
        _execute_extraction(
            _prepare_extraction(inputs, from_index=str(index))
        ).exit_code
        == 0
    )
    provider.reset_mock()
    assert (
        _execute_extraction(
            _prepare_extraction(
                inputs, from_index=str(index), index_filters=["label=first"]
            )
        ).exit_code
        == 0
    )
    assert not (Path(inputs["output"]) / "report.json").exists()
    provider.assert_not_called()


def test_check_displays_unknown_cost(reviewed_inputs):
    """Valid unpriced records are inspectable through the recommended CLI."""

    inputs, provider = reviewed_inputs
    provider.return_value["cost"] = None
    result = _execute_extraction(_prepare_extraction(inputs))
    check = CliRunner().invoke(
        cli,
        [
            "check",
            str(result.output_dir / "doc.json"),
            "--schema",
            inputs["schema"],
            "--show-data",
        ],
    )
    assert check.exit_code == 0, check.output
    assert "unknown" in check.output.lower()


def test_paid_qa_failure_retains_usage(reviewed_inputs):
    """A failed payload still carries returned billing evidence to QA."""

    inputs, provider = reviewed_inputs
    error = SchemaValidationError("fixture invalid payload")
    error.usage = {"cost": 0.025, "input_tokens": 13, "output_tokens": 7}
    provider.side_effect = error
    results = run_multi_model_extraction(
        doc_text="Source",
        doc_name="doc",
        schema=json.loads(Path(inputs["schema"]).read_text(encoding="utf-8")),
        registry=ModelRegistry(llm_config=inputs["llm_config"]),
        model_tiers=["gpt-fixture"],
        output_dir=Path(inputs["output"]),
    )
    assert not results["gpt-fixture"].success
    assert results["gpt-fixture"].cost == 0.025
    assert results["gpt-fixture"].input_tokens == 13


def test_replaced_authoritative_text_invalidates_reuse(reviewed_inputs):
    """Invalidate reuse when authoritative rendered text changes."""

    inputs, provider = reviewed_inputs
    document = Path(inputs["path"]) / "doc.txt"
    write_text_cache(
        document, "Original rendered source", method="browser_render"
    )
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    write_text_cache(
        document, "Changed rendered source", method="browser_render"
    )
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


def test_index_accepts_existing_project_relative_sources(
    reviewed_inputs, monkeypatch
):
    """Published project-relative source spellings remain executable."""

    inputs, _provider = reviewed_inputs
    root = Path(inputs["path"]).parent
    monkeypatch.chdir(root)
    index = root / "discovered/demo/index.csv"
    index.parent.mkdir(parents=True)
    with index.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["status", "path", "relative_path"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "status": "downloaded",
                "path": "documents/doc.txt",
                "relative_path": "doc.txt",
            }
        )
    assert (
        _execute_extraction(
            _prepare_extraction(inputs, from_index=str(index))
        ).exit_code
        == 0
    )


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


def test_derived_raw_ocr_cache_does_not_invalidate_reuse(
    reviewed_inputs, monkeypatch
):
    """Reuse unchanged inputs after parsing populates derived OCR."""

    inputs, provider = reviewed_inputs
    document = Path(inputs["path"]) / "doc.txt"

    def parse(*_args, **_kwargs):
        write_text_cache(document, "raw OCR evidence", method="ocr")
        return "raw OCR evidence"

    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document", parse
    )
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
    schema["properties"]["items"] = {
        composition: [{"type": "array", "items": {"type": "object"}}]
    }
    path.write_text(json.dumps(schema), encoding="utf-8")
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0


def test_index_common_layout_does_not_reject_disjoint_outputs(reviewed_inputs):
    """Explicit indexed inputs are not a recursive scan of every ancestor."""

    inputs, _provider = reviewed_inputs
    root = Path(inputs["path"]).parent
    second = root / "discovered/second.txt"
    second.parent.mkdir()
    second.write_text("Source", encoding="utf-8")
    index = root / "index.csv"
    with index.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["status", "path"])
        writer.writeheader()
        for document in [Path(inputs["path"]) / "doc.txt", second]:
            writer.writerow({"status": "downloaded", "path": str(document)})
    assert (
        _execute_extraction(
            _prepare_extraction(inputs, from_index=str(index))
        ).exit_code
        == 0
    )


def test_qaqc_rejects_ambiguous_same_stem_before_provider(reviewed_inputs):
    """QA namespace collisions cannot reuse a different document's payload."""

    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    documents = []
    for label in ["a", "b"]:
        path = root / label / "report.txt"
        path.parent.mkdir()
        path.write_text("Source", encoding="utf-8")
        documents.append(path)
    with pytest.raises(ValueError, match="collision"):
        _run_validation_extraction(
            doc_files=documents,
            loaded_schema=json.loads(
                Path(inputs["schema"]).read_text(encoding="utf-8")
            ),
            schema_path=Path(inputs["schema"]),
            output_dir=Path(inputs["output"]),
            registry=ModelRegistry(llm_config=inputs["llm_config"]),
            validation_models=["gpt-fixture", "gpt-second"],
            max_context=600000,
            timeout_seconds=10,
            page_range_map={},
            runtime_artifact=None,
            run_id="run://fixture",
            verbosity="quiet",
            prompt_confirm=False,
        )
    provider.assert_not_called()


def test_qaqc_write_failure_keeps_returned_usage(reviewed_inputs, monkeypatch):
    """Post-response publication failure retains paid call evidence."""

    inputs, _provider = reviewed_inputs
    original = builtins.open

    def fail_record(path, *args, **kwargs):
        if str(path).endswith("gpt-fixture.json"):
            raise OSError("fixture disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_record)
    results = run_multi_model_extraction(
        doc_text="Source",
        doc_name="doc",
        schema=json.loads(Path(inputs["schema"]).read_text(encoding="utf-8")),
        registry=ModelRegistry(llm_config=inputs["llm_config"]),
        model_tiers=["gpt-fixture"],
        output_dir=Path(inputs["output"]),
    )
    assert not results["gpt-fixture"].success
    assert results["gpt-fixture"].cost == 0.01
    assert results["gpt-fixture"].input_tokens == 12
