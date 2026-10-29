"""Extraction entrypoints share safe planning, outcomes, and verified reuse."""

import csv
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
import pymupdf
from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.pipeline import extract_documents


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    """Use a self-contained contract and a controlled provider boundary."""
    schema = {
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
                "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
            "items": {"type": "array", "items": {"type": "object"}},
        },
    }
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema))
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "alpha.txt").write_text("Source alpha")
    output = tmp_path / "records"
    provider = Mock(
        return_value={
            "data": {"source": {"id": "alpha"}, "items": []},
            "cost": 0.01,
            "input_tokens": 10,
            "output_tokens": 5,
        }
    )
    monkeypatch.setattr(
        "psweep.extraction.llm_client.LLMClient.extract", provider
    )
    monkeypatch.setattr("psweep.utils.config._global_config", None)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-fixture")
    config = type(
        "Config",
        (),
        {
            "llm_config": {
                "provider": "openai",
                "model": "gpt-fixture",
                "api_key": "fixture-key",
            }
        },
    )()
    monkeypatch.setattr("psweep.utils.config.get_config", lambda: config)
    for key in (
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    return documents, schema_path, output, provider


def _cli(documents, schema, output, *flags):
    return CliRunner().invoke(
        cli,
        [
            "extract",
            str(documents),
            "--schema",
            str(schema),
            "--output",
            str(output),
            "--quiet",
            *flags,
        ],
    )


@pytest.mark.parametrize("failure", ["all", "partial"])
def test_document_failures_exit_one(workflow, failure):
    """Recorded failures must stop subprocess-driven downstream stages."""
    documents, schema, output, provider = workflow
    if failure == "partial":
        (documents / "beta.txt").write_text("Source beta")
        provider.side_effect = [
            deepcopy(provider.return_value),
            RuntimeError("fixture failure"),
        ]
    else:
        provider.side_effect = RuntimeError("fixture failure")
    result = _cli(documents, schema, output)
    assert result.exit_code == 1, result.output
    assert list((output / "run_manifests").glob("*.json"))


@pytest.mark.parametrize(
    "invalid", ["missing_input", "invalid_pages", "missing_key"]
)
def test_fresh_preflight_failure_preserves_output(
    workflow, monkeypatch, invalid
):
    """A failed preflight never deletes the previous output corpus."""
    documents, schema, output, provider = workflow
    output.mkdir()
    sentinel = output / "prior.json"
    sentinel.write_text("previous output")
    flags = []
    if invalid == "missing_input":
        documents /= "absent"
    elif invalid == "invalid_pages":
        flags = ["--pages", "bad-range"]
    else:
        config = type(
            "Config",
            (),
            {
                "llm_config": {
                    "provider": "openai",
                    "model": "gpt-fixture",
                }
            },
        )()
        monkeypatch.setattr("psweep.utils.config.get_config", lambda: config)
        monkeypatch.delenv("OPENAI_API_KEY")
    result = _cli(documents, schema, output, "--fresh", *flags)
    assert result.exit_code != 0, result.output
    assert sentinel.read_text() == "previous output"
    provider.assert_not_called()


@pytest.mark.parametrize("layout", ["same", "ancestor", "inside"])
def test_output_overlap_rejected_before_fresh_deletion(workflow, layout):
    """Resolve overlap before any source can be deleted or reinventoried."""
    documents, schema, _output, provider = workflow
    output = (
        documents
        if layout == "same"
        else documents.parent
        if layout == "ancestor"
        else documents / "records"
    )
    result = _cli(documents, schema, output, "--fresh")
    assert result.exit_code != 0, result.output
    assert (documents / "alpha.txt").read_text() == "Source alpha"
    assert schema.is_file()
    provider.assert_not_called()


def test_empty_selected_inventory_exits_two(workflow):
    """No selected documents is distinct from a valid empty payload."""
    documents, schema, output, provider = workflow
    (documents / "alpha.txt").unlink()
    result = _cli(documents, schema, output)
    assert result.exit_code == 2, result.output
    provider.assert_not_called()


def test_zero_item_payload_succeeds_and_matches_envelope(workflow):
    """A successful model can return zero domain items."""
    documents, schema, output, provider = workflow
    result = _cli(documents, schema, output)
    assert result.exit_code == 0, result.output
    record = json.loads((output / "alpha.json").read_text())
    assert record["item_count"] == 0
    assert record["lineage"]["model"] == "gpt-fixture"
    assert provider.call_count == 1


def test_recursive_inventory_and_generated_sidecars(workflow):
    """Root files do not hide nested inputs, and sidecars are not documents."""
    documents, schema, output, provider = workflow
    nested = documents / "nested"
    nested.mkdir()
    (nested / "beta.TXT").write_text("Source beta")
    (documents / "alpha.section_target.txt").write_text("Generated excerpt")
    hidden = documents / ".sections"
    hidden.mkdir()
    (hidden / "alpha.txt").write_text("Cached excerpt")
    result = _cli(documents, schema, output)
    assert result.exit_code == 0, result.output
    assert (output / "nested/beta.json").is_file()
    assert provider.call_count == 2
    assert not (output / "alpha.section_target.json").exists()


def test_empty_index_does_not_fall_back_to_directory(workflow):
    """An authoritative index can select nothing even when inputs exist."""
    documents, schema, output, provider = workflow
    index = documents.parent / "index.csv"
    with index.open("w", newline="") as handle:
        csv.writer(handle).writerow(["status", "path", "domain"])
    result = _cli(documents, schema, output, "--from-index", str(index))
    assert result.exit_code == 2, result.output
    provider.assert_not_called()


def test_api_same_stem_collision_fails_before_calls(workflow):
    """API output mapping cannot overwrite or falsely reuse a second format."""
    documents, schema, output, provider = workflow
    (documents / "alpha.html").write_text("<p>Source beta</p>")
    with pytest.raises(ValueError, match="collision"):
        extract_documents(documents, schema, output_dir=output)
    provider.assert_not_called()


def test_reuse_requires_record_and_effective_evidence(workflow):
    """Valid records skip calls; source and schema changes invalidate them."""
    documents, schema, output, provider = workflow
    assert _cli(documents, schema, output).exit_code == 0
    first_calls = provider.call_count
    assert _cli(documents, schema, output).exit_code == 0
    assert provider.call_count == first_calls
    (documents / "alpha.txt").write_text("Changed source")
    assert _cli(documents, schema, output).exit_code == 0
    assert provider.call_count == first_calls + 1
    contract = json.loads(schema.read_text())
    contract["description"] = "Changed contract instructions"
    schema.write_text(json.dumps(contract))
    assert _cli(documents, schema, output).exit_code == 0
    assert provider.call_count == first_calls + 2
    (output / "alpha.json").write_text("{}")
    assert _cli(documents, schema, output).exit_code == 0
    assert provider.call_count == first_calls + 3


def test_all_reused_needs_no_provider_credentials(workflow, monkeypatch):
    """Validated output consumption is free and requires no new API key."""
    documents, schema, output, provider = workflow
    assert _cli(documents, schema, output).exit_code == 0
    provider.reset_mock()
    monkeypatch.setattr("psweep.utils.config._global_config", None)
    monkeypatch.delenv("OPENAI_API_KEY")
    config = type(
        "Config",
        (),
        {
            "llm_config": {
                "provider": "openai",
                "model": "gpt-fixture",
                "api_key": None,
            }
        },
    )()
    monkeypatch.setattr("psweep.utils.config.get_config", lambda: config)
    result = _cli(documents, schema, output)
    assert result.exit_code == 0, result.output
    provider.assert_not_called()


def test_api_limit_applies_after_verified_reuse(workflow):
    """An already-reused first document cannot stall a limited API run."""
    documents, schema, output, provider = workflow
    first = extract_documents(documents, schema, output_dir=output)
    assert first.successful == 1
    (documents / "beta.txt").write_text("Source beta")
    provider.reset_mock()
    second = extract_documents(documents, schema, output_dir=output, limit=1)
    assert second.failed == 0
    assert (output / "beta.json").is_file()
    assert provider.call_count == 1


def test_invalid_pdf_scope_preserves_fresh_output(workflow):
    """Out-of-document pages fail before a whole-output reset."""
    documents, schema, output, provider = workflow
    (documents / "alpha.txt").unlink()
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Source")
        pdf.save(documents / "alpha.pdf")
    output.mkdir()
    sentinel = output / "prior.json"
    sentinel.write_text("prior output")
    result = _cli(documents, schema, output, "--fresh", "--pages", "99-100")
    assert result.exit_code == 1
    assert sentinel.read_text() == "prior output"
    provider.assert_not_called()


def test_symlinked_source_inside_output_is_rejected(workflow):
    """A source symlink must not make fresh cleanup delete its real bytes."""
    documents, schema, output, provider = workflow
    (documents / "alpha.txt").unlink()
    output.mkdir()
    source = output / "source.txt"
    source.write_text("keep source")
    (documents / "alias.txt").symlink_to(source)
    result = _cli(documents, schema, output, "--fresh")
    assert result.exit_code == 1
    assert source.read_text() == "keep source"
    provider.assert_not_called()


def test_parser_failure_has_zero_model_calls(workflow, monkeypatch):
    """A failed local parse is not an unknown paid model attempt."""
    documents, schema, output, provider = workflow

    def fail_parse(*_args, **_kwargs):
        raise RuntimeError("fixture parser failure")

    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document",
        fail_parse,
    )
    result = _cli(documents, schema, output)
    assert result.exit_code == 1
    manifest = json.loads(
        next((output / "run_manifests").glob("*.json")).read_text()
    )
    assert manifest["costs"]["llm_calls"] == 0
    assert manifest["costs"]["unknown_cost_calls"] == 0
    provider.assert_not_called()


def test_client_setup_failure_preserves_fresh_output(workflow, monkeypatch):
    """Invalid client setup must not delete an existing extraction corpus."""
    documents, schema, output, provider = workflow
    output.mkdir()
    sentinel = output / "prior.json"
    sentinel.write_text("prior output")

    def fail_setup(**_kwargs):
        raise ValueError("invalid client setup")

    monkeypatch.setattr("psweep.extraction.DocumentExtractor", fail_setup)
    result = _cli(documents, schema, output, "--fresh")
    assert result.exit_code == 1
    assert sentinel.read_text() == "prior output"
    provider.assert_not_called()
