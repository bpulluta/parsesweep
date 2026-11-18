"""Extraction entrypoints share safe planning, outcomes, and verified reuse."""

import csv
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
import pymupdf
from click.testing import CliRunner

from psweep.cli.main import cli
from psweep.outcomes import EXIT_FAILED, EXIT_PARTIAL
from psweep.pipeline import extract_documents


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    """Use a self-contained contract and a controlled provider boundary."""
    schema = {
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
    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", provider)
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


@pytest.mark.parametrize(("failure", "expected"), [("all", EXIT_FAILED), ("partial", EXIT_PARTIAL)])
def test_document_failures_exit_nonzero(workflow, failure, expected):
    """Total failure halts downstream stages; partial failure is distinguishable."""
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
    assert result.exit_code == expected, result.output
    assert list((output / "run_manifests").glob("*.json"))


@pytest.mark.parametrize("invalid", ["missing_input", "invalid_pages", "missing_key"])
def test_fresh_preflight_failure_preserves_output(workflow, monkeypatch, invalid):
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
    manifest = json.loads(next((output / "run_manifests").glob("*.manifest.json")).read_text())
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


def _mutate_after_fingerprints(monkeypatch, document, content):
    """Change the source after preparation fixed its identity, once."""
    from psweep import pipeline

    original = pipeline._extraction_fingerprints
    state = {"done": False}

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        if not state["done"]:
            state["done"] = True
            document.write_text(content)
        return result

    monkeypatch.setattr(pipeline, "_extraction_fingerprints", wrapper)


def test_source_changed_after_preparation_is_held(workflow, monkeypatch):
    """Bytes that differ from the prepared identity are never extracted."""
    documents, schema, output, provider = workflow
    _mutate_after_fingerprints(monkeypatch, documents / "alpha.txt", "Source beta")
    result = extract_documents(documents, schema, output_dir=output)
    assert (result.successful, result.failed) == (0, 1)
    assert result.errors[0]["errors"][0]["code"] == "source_changed"
    provider.assert_not_called()
    assert not (output / "alpha.json").exists()


def test_a_b_a_never_reuses_a_payload_extracted_from_b(workflow, monkeypatch):
    """A run that prepared A but met B publishes nothing reusable under A."""
    documents, schema, output, provider = workflow
    _mutate_after_fingerprints(monkeypatch, documents / "alpha.txt", "Source beta")
    extract_documents(documents, schema, output_dir=output)
    (documents / "alpha.txt").write_text("Source alpha")
    provider.reset_mock()
    result = extract_documents(documents, schema, output_dir=output)
    assert result.successful == 1
    assert provider.call_count == 1
    assert "Source alpha" in str(provider.call_args)
    assert "Source beta" not in str(provider.call_args)


def test_record_digest_is_the_consumed_version(workflow):
    """A mid-request change cannot make the record claim the live bytes."""
    import hashlib

    documents, schema, output, provider = workflow
    consumed = (documents / "alpha.txt").read_bytes()
    answer = deepcopy(provider.return_value)

    def mutate(*_args, **_kwargs):
        (documents / "alpha.txt").write_text("Source beta")
        return answer

    provider.side_effect = mutate
    assert extract_documents(documents, schema, output_dir=output).successful == 1
    record = json.loads((output / "alpha.json").read_text())
    assert record["document"]["source_sha256"] == hashlib.sha256(consumed).hexdigest()
    provider.side_effect = None
    provider.reset_mock()
    assert extract_documents(documents, schema, output_dir=output).successful == 1
    assert provider.call_count == 1


@pytest.mark.parametrize("initial", ["Pima County A evidence", "No roster matches"])
@pytest.mark.parametrize("change", ["source", "effective"])
def test_roster_decision_is_bound_to_prepared_input(workflow, monkeypatch, initial, change):
    from psweep import pipeline
    from psweep.extraction.document_utils import write_text_cache
    from psweep.extraction.provenance import SourceContext
    from psweep.extraction.roster import Roster

    documents, schema, output, provider = workflow
    document = documents / "alpha.txt"
    contract = json.loads(schema.read_text())
    contract["required"] = ["items"]  # valid abstention must not mask stale no-match
    schema.write_text(json.dumps(contract))
    roster = Roster.build([{"state": "AZ", "ahj": "Pima County"}], ("state", "ahj"))
    monkeypatch.setattr(pipeline, "load_roster", lambda *_: roster)
    monkeypatch.setattr(
        pipeline,
        "build_source_context_map",
        lambda *_: {"alpha.txt": SourceContext(None, "shared", None, None, {})},
    )
    if change == "effective":
        write_text_cache(document, initial, method="browser_render")
    else:
        document.write_text(initial)
    plan = pipeline._prepare_extraction(
        {"path": str(documents), "schema": str(schema), "output": str(output)}
    )
    if change == "effective":
        write_text_cache(document, "Pima County B evidence", method="browser_render")
    else:
        document.write_text("Pima County B evidence")
    outcome = pipeline._execute_extraction(plan)
    assert outcome.failed == 1
    provider.assert_not_called()
    assert not (output / "alpha.json").exists()


@pytest.mark.parametrize("initial", ["Pima County A evidence", "No roster matches"])
def test_roster_windows_use_frozen_effective_evidence(workflow, monkeypatch, initial):
    from psweep import pipeline
    from psweep.extraction.document_utils import write_text_cache
    from psweep.extraction.provenance import SourceContext
    from psweep.extraction.roster import Roster

    documents, schema, output, provider = workflow
    document = documents / "alpha.txt"
    contract = json.loads(schema.read_text())
    contract["required"] = ["items"]
    schema.write_text(json.dumps(contract))
    roster = Roster.build([{"state": "AZ", "ahj": "Pima County"}], ("state", "ahj"))
    monkeypatch.setattr(pipeline, "load_roster", lambda *_: roster)
    monkeypatch.setattr(
        pipeline,
        "build_source_context_map",
        lambda *_: {"alpha.txt": SourceContext(None, "shared", None, None, {})},
    )
    write_text_cache(document, initial, method="browser_render")
    original = pipeline._verified_roster_text

    def verify(*args, **kwargs):
        write_text_cache(document, "Pima County B evidence", method="browser_render")
        try:
            return original(*args, **kwargs)
        finally:
            write_text_cache(document, initial, method="browser_render")

    monkeypatch.setattr(pipeline, "_verified_roster_text", verify)
    plan = pipeline._prepare_extraction(
        {"path": str(documents), "schema": str(schema), "output": str(output)}
    )
    assert pipeline._execute_extraction(plan).successful == 1
    if initial.startswith("Pima"):
        assert "A evidence" in str(provider.call_args)
        assert "B evidence" not in str(provider.call_args)
    else:
        provider.assert_not_called()


def test_effective_a_b_a_during_generation_uses_frozen_a(workflow, monkeypatch):
    from psweep import pipeline
    from psweep.extraction.document_utils import write_text_cache

    documents, schema, output, provider = workflow
    document = documents / "alpha.txt"
    write_text_cache(document, "A evidence", method="browser_render")
    original = pipeline._prepared_input

    def prepare(*args, **kwargs):
        write_text_cache(document, "B evidence", method="browser_render")
        try:
            return original(*args, **kwargs)
        finally:
            write_text_cache(document, "A evidence", method="browser_render")

    monkeypatch.setattr(pipeline, "_prepared_input", prepare)
    assert extract_documents(documents, schema, output_dir=output).successful == 1
    assert "A evidence" in str(provider.call_args)
    assert "B evidence" not in str(provider.call_args)
    provider.reset_mock()
    assert extract_documents(documents, schema, output_dir=output).successful == 1
    provider.assert_not_called()


def test_snapshots_are_private_and_removed(workflow, monkeypatch, tmp_path):
    """Per-document snapshots live outside inputs/outputs and never accumulate."""
    documents, schema, output, provider = workflow
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    seen = []

    def inspect_snapshot(*_args, **_kwargs):
        seen.extend(scratch.iterdir())
        return deepcopy(provider.return_value)

    provider.side_effect = inspect_snapshot
    assert extract_documents(documents, schema, output_dir=output).successful == 1
    assert len(seen) == 1
    assert list(scratch.iterdir()) == []
    provider.side_effect = RuntimeError("fixture failure")
    (documents / "alpha.txt").write_text("Changed")
    assert extract_documents(documents, schema, output_dir=output).failed == 1
    assert list(scratch.iterdir()) == []


# --- Step 3: current admission is separate from preserved history -------------


def _answer(items=1):
    return {
        "data": {"source": {"id": "x"}, "items": [{"n": i} for i in range(items)]},
        "cost": 0.01,
        "input_tokens": 10,
        "output_tokens": 5,
    }


def _route(provider, fail=(), items=1):
    """Fail the call whose prompt mentions a failing document marker."""

    def respond(*args, **kwargs):
        blob = f"{args}{kwargs}"
        if any(marker in blob for marker in fail):
            raise RuntimeError("fixture failure")
        return _answer(items)

    provider.side_effect = respond


def _admission(output):
    return json.loads((output / "run_manifests" / "current_admission.json").read_text())["records"]


def _two_changed_documents(workflow):
    documents, schema, output, provider = workflow
    (documents / "a.txt").write_text("marker-a one")
    (documents / "b.txt").write_text("marker-b one")
    (documents / "alpha.txt").unlink()
    _route(provider)
    assert extract_documents(documents, schema, output_dir=output).successful == 2
    (documents / "a.txt").write_text("marker-a two")
    (documents / "b.txt").write_text("marker-b two")
    return documents, schema, output, provider


def test_failed_changed_document_keeps_history_but_loses_admission(workflow):
    """The old-a/new-b reproduction: history stays, admission does not."""
    import hashlib

    documents, schema, output, provider = _two_changed_documents(workflow)
    old_a = (output / "a.json").read_bytes()
    _route(provider, fail=("marker-a",))
    result = extract_documents(documents, schema, output_dir=output)
    assert (result.successful, result.failed) == (1, 1)
    assert (output / "a.json").read_bytes() == old_a
    records = _admission(output)
    assert records["a.json"]["disposition"] == "failed"
    assert "record_sha256" not in records["a.json"]
    assert records["b.json"]["disposition"] == "new"
    assert (
        records["b.json"]["record_sha256"]
        == hashlib.sha256((output / "b.json").read_bytes()).hexdigest()
    )


def test_limit_deferred_changed_record_is_not_admitted(workflow):
    documents, schema, output, _ = _two_changed_documents(workflow)
    extract_documents(documents, schema, output_dir=output, limit=1)
    records = _admission(output)
    assert records["a.json"]["disposition"] == "new"
    assert records["b.json"]["disposition"] == "deferred"
    extract_documents(documents, schema, output_dir=output, limit=1)
    assert {rel: r["disposition"] for rel, r in _admission(output).items()} == {
        "a.json": "reused",
        "b.json": "new",
    }


def test_verified_reuse_is_admitted_and_scoped_runs_preserve_siblings(workflow):
    documents, schema, output, provider = _two_changed_documents(workflow)
    extract_documents(documents, schema, output_dir=output)
    provider.reset_mock()
    extract_documents(documents, schema, output_dir=output)
    provider.assert_not_called()
    assert {r["disposition"] for r in _admission(output).values()} == {"reused"}
    # A single-file scope qualifies only itself and keeps its sibling's entry.
    (documents / "a.txt").write_text("marker-a three")
    _route(provider, fail=("marker-a",))
    extract_documents(documents / "a.txt", schema, output_dir=output)
    records = _admission(output)
    assert records["a.json"]["disposition"] == "failed"
    assert records["b.json"]["disposition"] == "reused"


def test_withdrawn_record_leaves_admission(workflow):
    documents, schema, output, _ = _two_changed_documents(workflow)
    extract_documents(documents, schema, output_dir=output)
    (documents / "b.txt").unlink()
    extract_documents(documents, schema, output_dir=output)
    assert not (output / "b.json").exists()
    assert set(_admission(output)) == {"a.json"}


def test_extraction_output_rejects_a_second_writer(workflow):
    from psweep.utils.file_io import PublicationLockError, publication_lock
    from psweep import pipeline

    documents, schema, output, provider = workflow
    with (
        publication_lock(pipeline.extraction_lock_path(output)),
        pytest.raises(PublicationLockError),
    ):
        extract_documents(documents, schema, output_dir=output)
    provider.assert_not_called()


def test_reuse_is_reverified_under_the_lock(workflow):
    """A record replaced between prepare and execute is never certified as reused."""
    from psweep import pipeline

    documents, schema, output, _ = workflow
    assert extract_documents(documents, schema, output_dir=output).successful == 1
    inputs = {
        "path": str(documents),
        "schema": str(schema),
        "output": str(output),
        "skip_existing": True,
    }
    plan = pipeline._prepare_extraction(inputs)
    assert list(plan["reused"]) and not plan["pending"]
    record = output / "alpha.json"
    record.write_bytes(record.read_bytes() + b"\n")  # a second writer lands
    result = pipeline._execute_extraction(plan)
    assert (result.successful, result.failed) == (0, 1)
    assert result.admitted == 0
    assert _admission(output)["alpha.json"]["disposition"] == "failed"


def test_admission_entries_carry_source_and_fingerprint_identity(workflow):
    import hashlib

    documents, schema, output, _ = _two_changed_documents(workflow)
    extract_documents(documents, schema, output_dir=output)
    extract_documents(documents, schema, output_dir=output)  # verified reuse
    for rel, entry in _admission(output).items():
        source = documents / rel.replace(".json", ".txt")
        assert entry["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert len(entry["input_fingerprint"]) == 64


def test_interrupted_run_leaves_a_consistent_recoverable_pointer(workflow):
    """A crash mid-run excludes rewritten documents instead of mismatching bytes."""
    from psweep.compilation.input_provenance import select_compile_inputs

    documents, schema, output, provider = _two_changed_documents(workflow)
    extract_documents(documents, schema, output_dir=output)
    (documents / "a.txt").write_text("marker-a three")
    (documents / "b.txt").write_text("marker-b three")

    def respond(*args, **kwargs):
        if "marker-b" in f"{args}{kwargs}":
            raise KeyboardInterrupt
        return _answer()

    provider.side_effect = respond
    with pytest.raises(KeyboardInterrupt):
        extract_documents(documents, schema, output_dir=output)
    records = _admission(output)
    assert {r["disposition"] for r in records.values()} == {"deferred"}
    inputs = select_compile_inputs(output)
    assert not inputs.records
    _route(provider)
    assert extract_documents(documents, schema, output_dir=output).admitted == 2


def test_manifest_records_item_and_zero_item_outcomes(workflow):
    documents, schema, output, provider = workflow
    (documents / "empty.txt").write_text("marker-empty")
    provider.side_effect = lambda *a, **k: _answer(0 if "marker-empty" in f"{a}{k}" else 2)
    extract_documents(documents, schema, output_dir=output)
    (manifest,) = (output / "run_manifests").glob("*.manifest.json")
    status = json.loads(manifest.read_text())["status"]
    assert status["items"] == 2 and status["zero_item_records"] == 1


def test_compilation_uses_only_admitted_current_records(workflow, tmp_path):
    """The old-a/new-b gate: history stays on disk, compilation reads new-b only."""
    from psweep.pipeline import compile_extractions, compiled_table_fresh

    documents, schema, output, provider = _two_changed_documents(workflow)
    _route(provider, fail=("marker-a",))
    assert extract_documents(documents, schema, output_dir=output).failed == 1
    compiled = tmp_path / "compiled"
    result = compile_extractions(output, schema, output_dir=compiled)
    assert result.total_rows == 1 and result.status == "success"
    assert (output / "a.json").is_file()
    (manifest,) = (compiled / "run_manifests").glob("*.manifest.json")
    admission = json.loads(manifest.read_text())["outputs"]["admission"]
    assert admission == {
        "qualification": "current",
        "admitted_records": 1,
        "excluded_records": 1,
        "failed_documents": 1,
    }
    assert compiled_table_fresh(output, schema, config_path=None, output_dir=compiled)
    # Same record bytes, different admission: the table is no longer fresh.
    _route(provider)
    extract_documents(documents, schema, output_dir=output)
    assert not compiled_table_fresh(output, schema, config_path=None, output_dir=compiled)


def test_broken_current_pointer_blocks_compilation_and_freshness(workflow, tmp_path):
    from psweep.pipeline import compile_extractions, compiled_table_fresh

    _, schema, output, _ = _two_changed_documents(workflow)
    compiled = tmp_path / "compiled"
    compile_extractions(output, schema, output_dir=compiled)
    (output / "run_manifests" / "current_admission.json").write_text("{broken")
    with pytest.raises(ValueError, match="admission evidence"):
        compile_extractions(output, schema, output_dir=compiled)
    assert not compiled_table_fresh(output, schema, config_path=None, output_dir=compiled)


def test_empty_table_beside_failed_documents_is_not_success(workflow, tmp_path):
    """Zero rows from valid empty records plus failures never reads as clean data."""
    from psweep.pipeline import compile_extractions

    documents, schema, output, provider = workflow
    (documents / "good.txt").write_text("marker-good")
    (documents / "bad.txt").write_text("marker-bad")
    (documents / "alpha.txt").unlink()

    def respond(*args, **kwargs):
        if "marker-bad" in f"{args}{kwargs}":
            raise RuntimeError("fixture failure")
        return _answer(0)

    provider.side_effect = respond
    assert extract_documents(documents, schema, output_dir=output).failed == 1
    result = compile_extractions(output, schema, output_dir=tmp_path / "compiled")
    assert result.total_rows == 0 and result.status == "failed"


def test_valid_empty_results_without_failures_remain_success(workflow, tmp_path):
    from psweep.pipeline import compile_extractions

    documents, schema, output, provider = workflow
    provider.side_effect = lambda *a, **k: _answer(0)
    extract_documents(documents, schema, output_dir=output)
    result = compile_extractions(output, schema, output_dir=tmp_path / "compiled")
    assert result.total_rows == 0 and result.status == "success"
