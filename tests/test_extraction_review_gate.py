"""Regression gates for independent Phase 2 integration findings."""

import csv
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, Mock
from threading import Event, Lock, get_ident

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
    inputs["page_targeting"] = {
        "enabled": True,
        "section_description": "evidence",
        "model": "gpt-locator",
    }
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


def test_targeting_and_extraction_share_bounded_document_workers(reviewed_inputs, monkeypatch):
    """A slow locator must not block extraction of a different started document."""
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "doc.txt").unlink()
    for name in ("a-slow.txt", "b-fast.txt", "c-next.txt", "d-next.txt"):
        (root / name).write_text("Source evidence")
    inputs.update(
        max_concurrent=2,
        page_targeting={"enabled": True, "section_description": "evidence", "model": "gpt-locator"},
    )
    extracted = Event()
    lock = Lock()
    locators = {}
    seen = []

    def locate(locator, doc):
        with lock:
            locators.setdefault(get_ident(), set()).add(id(locator))
            seen.append(doc.name)
        if doc.name == "a-slow.txt":
            assert extracted.wait(5), "all-document locator barrier blocked extraction"

    def extract(*_args, **_kwargs):
        if current_document() == "b-fast.txt":
            extracted.set()
        return DEFAULT

    monkeypatch.setattr("psweep.extraction.section_locator.SectionLocator.locate", locate)
    provider.side_effect = extract
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert len(locators) == 2
    assert all(len(values) == 1 for values in locators.values())
    assert len(set.union(*locators.values())) == 2
    assert sorted(seen) == ["a-slow.txt", "b-fast.txt", "c-next.txt", "d-next.txt"]


@pytest.mark.parametrize("field", ["asset", "jurisdiction", "instrument"])
def test_targeting_binds_subject_from_configured_identity_not_the_label(
    reviewed_inputs, monkeypatch, field
):
    inputs, _provider = reviewed_inputs
    inputs.update(
        partition_by=["region", field],
        page_targeting={"enabled": True, "section_description": "evidence", "model": "gpt-locator"},
    )
    with (Path(inputs["path"]) / "download_index.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["path", "source_scope", "target_id", "target_label", "target_metadata"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "path": "doc.txt",
                "source_scope": "target",
                "target_id": "fixture",
                "target_label": "North — Alpha, Unit 7",
                "target_metadata": json.dumps(
                    {"region": "North", field: "Alpha, Unit 7", "location": "Other"}
                ),
            }
        )
    seen = []
    monkeypatch.setattr(
        "psweep.extraction.section_locator.SectionLocator.locate",
        lambda locator, _doc: seen.append(locator._subject_hints),
    )
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert seen == [["North — Alpha, Unit 7", "Alpha, Unit 7"]]


@pytest.mark.parametrize("save", [False, True])
def test_completed_section_text_is_released_before_the_next_document(
    reviewed_inputs, monkeypatch, save
):
    inputs, _provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "next.txt").write_text("Source evidence")
    inputs.update(
        max_concurrent=1,
        page_targeting={
            "enabled": True,
            "section_description": "evidence",
            "model": "gpt-locator",
            "save_discovered": save,
        },
    )
    retained = {}
    prepare = pipeline._target_extraction_inputs

    def capture(plan):
        result = prepare(plan)
        retained["sections"] = result[1]
        return result

    monkeypatch.setattr(pipeline, "_target_extraction_inputs", capture)
    monkeypatch.setattr(
        "psweep.extraction.section_locator.SectionLocator.locate",
        lambda _self, _doc: "Source evidence",
    )

    def progress(_doc, outcome):
        if outcome is not None:
            assert retained["sections"] == {}

    assert _execute_extraction(_prepare_extraction(inputs), on_progress=progress).exit_code == 0
    saved = list((Path(inputs["output"]) / ".sections").glob("*.txt"))
    assert len(saved) == (2 if save else 0)
    assert all(path.read_text() == "Source evidence" for path in saved)


def test_subject_context_and_locator_version_invalidate_record_reuse(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    inputs["page_targeting"] = {
        "enabled": True,
        "section_description": "evidence",
        "model": "gpt-locator",
    }
    index = Path(inputs["path"]) / "download_index.csv"
    inputs["path"] = str(Path(inputs["path"]) / "doc.txt")

    def write_context(label):
        index.write_text(
            "path,source_scope,target_id,target_label,target_metadata\n"
            f'doc.txt,target,1,{label},"{{""zone"":""North""}}"\n'
        )

    write_context("Subject Alpha")
    seen = []
    monkeypatch.setattr(
        "psweep.extraction.section_locator.SectionLocator.locate",
        lambda locator, _doc: seen.append(locator._source_context.target_label),
    )
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert _execute_extraction(_prepare_extraction(inputs)).status == "reused"
    write_context("Subject Beta")
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    monkeypatch.setattr(pipeline.page_locator, "LOCATOR_BEHAVIOR_VERSION", "future")
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert provider.call_count == 3
    assert seen == ["Subject Alpha", "Subject Beta", "Subject Beta"]


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
        "model": "gpt-locator",
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
            from psweep.extraction.prepared_document import capture_source

            with capture_source(source) as captured:
                locator._write_cache(captured, PageSelection(((1, 1),)), 1)
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
        lambda plan: (plan["ranges"], {}, [], lambda _doc: None),
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
        "model": "gpt-locator",
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
    inputs["page_targeting"] = {
        "enabled": True,
        "section_description": "evidence",
        "model": "gpt-locator",
    }
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


@pytest.mark.parametrize("kind", ["section", "page"])
def test_locator_consumes_snapshot_during_live_a_b_a(reviewed_inputs, monkeypatch, kind):
    """A locator and primary must use A even while the original path contains B."""
    from psweep.extraction.prepared_document import CapturedSource

    inputs, provider = reviewed_inputs
    document = Path(inputs["path"]) / "doc.txt"
    if kind == "page":
        document = document.rename(document.with_suffix(".pdf"))
        monkeypatch.setattr(
            pipeline.document_utils,
            "_extract_from_pdf",
            lambda path, ranges: (path.read_text(), {"page_ranges": ranges}),
        )
    original = document.read_bytes()
    _targeting(inputs, trigger_chars=1)
    seen = []

    def locate(self, source):
        document.write_bytes(b"B evidence")
        try:
            assert isinstance(source, CapturedSource)
            seen.append(source.path.read_bytes())
            return PageSelection(((1, 1),)) if kind == "page" else source.path.read_text()
        finally:
            document.write_bytes(original)

    monkeypatch.setattr(
        PageLocator if kind == "page" else pipeline.SectionLocator, "locate", locate
    )
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    assert seen == [original]
    assert "B evidence" not in str(provider.call_args)


def test_source_mismatch_precedes_locator_request(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    _targeting(inputs, trigger_chars=1)
    plan = _prepare_extraction(inputs)
    (Path(inputs["path"]) / "doc.txt").write_text("changed")
    locate = Mock(return_value="changed")
    monkeypatch.setattr(pipeline.SectionLocator, "locate", locate)
    assert _execute_extraction(plan).failed == 1
    locate.assert_not_called()
    provider.assert_not_called()


@pytest.mark.parametrize("method", ["browser_render", "api", "ocr"])
@pytest.mark.parametrize("change", ["replace", "missing"])
def test_effective_product_mismatch_precedes_paid_use(reviewed_inputs, method, change):
    from psweep.extraction.document_utils import text_cache_paths

    inputs, provider = reviewed_inputs
    document = Path(inputs["path"]) / "doc.txt"
    write_text_cache(document, "A evidence", method=method)
    plan = _prepare_extraction(inputs)
    if change == "replace":
        write_text_cache(document, "B evidence", method=method)
    else:
        text_cache_paths(document)[0].unlink()
    outcome = _execute_extraction(plan)
    assert outcome.failed == 1
    assert outcome.errors[0]["errors"][0]["code"] == "source_changed"
    provider.assert_not_called()


def test_fresh_scope_change_at_consumption_preserves_prior_output(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    output = _sentinel_output(inputs)
    _targeting(inputs, trigger_chars=1000)
    _sections(monkeypatch)
    original = pipeline._extract_one_document

    def consume(doc, *args, **kwargs):
        doc.write_text("Changed after fresh preflight")
        return original(doc, *args, **kwargs)

    monkeypatch.setattr(pipeline, "_extract_one_document", consume)
    assert _execute_extraction(_prepare_extraction(inputs, fresh=True)).failed == 1
    assert (output / "doc.json").read_bytes() == b"prior record bytes"
    assert (output / "other.json").read_bytes() == b"other prior bytes"
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


def test_manifest_uses_digest_of_exact_published_record(reviewed_inputs, monkeypatch):
    from psweep.utils.file_io import artifact_sha256

    inputs, provider = reviewed_inputs
    document = Path(inputs["path"]) / "doc.txt"
    write_text_cache(document, "OCR A", method="ocr")
    complete = pipeline._complete_extraction
    published = []

    def interleave(plan, *args, **kwargs):
        path = plan["outputs"][document]
        published.append(artifact_sha256(path))
        record = json.loads(path.read_text())
        record["payload"]["source"]["id"] = "OCR B"
        path.write_text(json.dumps(record))
        return complete(plan, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "_complete_extraction", interleave)
        first = _execute_extraction(_prepare_extraction(inputs))
    proof = next(iter(json.loads(first.manifest_path.read_text())["reuse"].values()))
    assert proof["record_sha256"] == published[0]
    provider.reset_mock()
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.assert_called_once()


def test_cached_audio_without_transcriber_tracks_effective_identity(reviewed_inputs):
    inputs, provider = reviewed_inputs
    document = (Path(inputs["path"]) / "doc.txt").rename(Path(inputs["path"]) / "doc.mp3")
    write_text_cache(document, "TRANSCRIPT A", method="transcript:fixture:v1")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    write_text_cache(document, "TRANSCRIPT B", method="transcript:fixture:v1")
    provider.reset_mock()
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.assert_called_once()
    assert "TRANSCRIPT B" in str(provider.call_args)


def test_new_derived_cache_cannot_requalify_planned_reuse(reviewed_inputs):
    from psweep.extraction.document_utils import text_cache_paths

    inputs, provider = reviewed_inputs
    document = Path(inputs["path"]) / "doc.txt"
    write_text_cache(document, "OCR A", method="ocr")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    for path in text_cache_paths(document):
        path.unlink()
    plan = _prepare_extraction(inputs)
    assert plan["reused"]
    write_text_cache(document, "OCR B", method="ocr")
    provider.reset_mock()
    assert _execute_extraction(plan).failed == 1
    provider.assert_not_called()


def test_empty_fresh_run_preserves_prior_output(reviewed_inputs):
    inputs, provider = reviewed_inputs
    output = _sentinel_output(inputs)
    (Path(inputs["path"]) / "doc.txt").unlink()
    outcome = _execute_extraction(_prepare_extraction(inputs, fresh=True))
    assert outcome.status == "empty"
    assert (output / "doc.json").read_bytes() == b"prior record bytes"
    assert (output / "other.json").read_bytes() == b"other prior bytes"
    provider.assert_not_called()


def test_saved_scope_excludes_failed_reused_source(reviewed_inputs):
    inputs, provider = reviewed_inputs
    inputs["page_targeting"] = {"save_discovered": True}
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    output = Path(inputs["output"])
    _rewrite_record(
        output,
        lambda record: record["processing_metrics"]["coverage"].__setitem__(
            "selected_page_ranges",
            [[1, 1]],
        ),
    )
    plan = _prepare_extraction(inputs)
    assert plan["reused"]
    (Path(inputs["path"]) / "doc.txt").write_text("changed")
    provider.reset_mock()
    assert _execute_extraction(plan).failed == 1
    assert load_pages_csv(output / "discovered_page_ranges.csv") == {}
    provider.assert_not_called()


def test_unproven_pre_fix_consumption_is_not_reused(reviewed_inputs):
    inputs, provider = reviewed_inputs
    first = _execute_extraction(_prepare_extraction(inputs))
    manifest = json.loads(first.manifest_path.read_text())
    for proof in manifest["reuse"].values():
        proof.pop("consumption_version")
    first.manifest_path.write_text(json.dumps(manifest))
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    assert provider.call_count == 2


@pytest.mark.parametrize("kind", ["source", "effective"])
def test_prepared_reuse_is_verified_at_consumption(reviewed_inputs, kind):
    inputs, provider = reviewed_inputs
    document = Path(inputs["path"]) / "doc.txt"
    write_text_cache(document, "A evidence", method="api")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    provider.reset_mock()
    plan = _prepare_extraction(inputs)
    assert plan["reused"]
    if kind == "source":
        document.write_text("B source")
    else:
        write_text_cache(document, "B evidence", method="api")
    assert _execute_extraction(plan).failed == 1
    provider.assert_not_called()


def test_changed_derived_cache_invalidates_verified_reuse(reviewed_inputs):
    inputs, provider = reviewed_inputs
    document = Path(inputs["path"]) / "doc.txt"
    write_text_cache(document, "A evidence", method="ocr")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    write_text_cache(document, "B evidence", method="ocr")
    assert _execute_extraction(_prepare_extraction(inputs)).successful == 1
    assert provider.call_count == 2


@pytest.mark.parametrize("kind", ["source", "effective"])
def test_fresh_identity_mismatch_preserves_all_prior_payloads(reviewed_inputs, kind):
    inputs, provider = reviewed_inputs
    output = _sentinel_output(inputs)
    document = Path(inputs["path"]) / "doc.txt"
    write_text_cache(document, "A evidence", method="api")
    plan = _prepare_extraction(inputs, fresh=True)
    if kind == "source":
        document.write_text("B source")
    else:
        write_text_cache(document, "B evidence", method="api")
    assert _execute_extraction(plan).failed == 1
    assert (output / "doc.json").read_bytes() == b"prior record bytes"
    assert (output / "other.json").read_bytes() == b"other prior bytes"
    provider.assert_not_called()


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
        for p in (Path(inputs["output"]) / "run_manifests").glob("*.manifest.json")
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


@pytest.mark.parametrize("fresh", [False, True])
def test_derived_raw_ocr_cache_does_not_invalidate_reuse(reviewed_inputs, monkeypatch, fresh):
    """Reuse unchanged inputs after parsing populates derived OCR."""

    inputs, provider = reviewed_inputs
    document = Path(inputs["path"]) / "doc.txt"
    document.rename(document.with_suffix(".pdf"))

    def parse(*_args, **_kwargs):
        return "raw OCR evidence", {
            "used_ocr": True,
            "raw_text": "raw OCR evidence",
            "ocr_spans": [[0, 16]],
            "page_count": 1,
            "extracted_pages": [1],
            "selected_pages": [1],
        }

    monkeypatch.setattr("psweep.extraction.document_utils._extract_from_pdf", parse)
    if fresh:
        _sentinel_output(inputs)
        _targeting(inputs, trigger_chars=1000)
        monkeypatch.setattr(
            "psweep.extraction.pdf_utils.extract_pages_text", lambda _, **_k: ["small"]
        )
    assert _execute_extraction(_prepare_extraction(inputs, fresh=fresh)).exit_code == 0
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


# -- Protected automatic targeting: fail closed before the primary request ------


def _targeting(inputs, **extra):
    inputs["page_targeting"] = {
        "enabled": True,
        "section_description": "evidence",
        "model": "gpt-locator",
        **extra,
    }


def _sections(monkeypatch, *, by_name=None, default=None, incomplete=False, calls=None):
    """Script SectionLocator: ``by_name`` maps file name -> (section, incomplete)."""

    def locate(self, doc):
        if calls is not None:
            calls.append(doc.name)
        section, flagged = (by_name or {}).get(doc.name, (default, incomplete))
        self.incomplete = flagged
        return section

    monkeypatch.setattr("psweep.extraction.section_locator.SectionLocator.locate", locate)


def _codes(outcome):
    return [error["code"] for record in outcome.errors for error in record["errors"]]


def test_incomplete_targeting_with_no_scope_is_held_before_the_primary_call(
    reviewed_inputs, monkeypatch
):
    inputs, provider = reviewed_inputs
    _targeting(inputs)
    _sections(monkeypatch, incomplete=True)
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert _codes(outcome) == ["targeting_no_usable_scope"]
    assert outcome.errors[0]["errors"][0]["category"] == "targeting"
    provider.assert_not_called()
    assert not (Path(inputs["output"]) / "doc.json").exists()


def test_valid_no_match_above_trigger_is_held_even_when_it_would_fit(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    (Path(inputs["path"]) / "doc.txt").write_text("evidence " * 50, encoding="utf-8")
    _targeting(inputs, trigger_chars=100)
    _sections(monkeypatch)
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert _codes(outcome) == ["targeting_no_usable_scope"]
    provider.assert_not_called()


def test_small_readable_document_is_extracted_without_a_selection(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    _targeting(inputs, trigger_chars=1000)
    _sections(monkeypatch)
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    provider.assert_called_once()


@pytest.mark.parametrize(("limit", "held"), [(15, False), (14, True)])
def test_final_prompt_boundary_is_exact(reviewed_inputs, monkeypatch, limit, held):
    inputs, provider = reviewed_inputs  # the source text is exactly 15 characters
    inputs["max_context"] = limit
    _targeting(inputs, trigger_chars=1000)
    _sections(monkeypatch, default="Source evidence")  # selected scope, exactly 15 characters
    outcome = _execute_extraction(_prepare_extraction(inputs))
    if held:
        assert _codes(outcome) == ["targeting_context_budget_exceeded"]
        provider.assert_not_called()
    else:
        assert outcome.exit_code == 0
        assert provider.call_args.args[0].count("Source evidence") == 1


def test_targeting_triggers_at_max_context_when_trigger_is_larger(reviewed_inputs, monkeypatch):
    from psweep.extraction.section_locator import SectionLocator

    inputs, provider = reviewed_inputs  # 15 characters of source text
    inputs["max_context"] = 5
    _targeting(inputs, trigger_chars=10_000)
    _sections(monkeypatch)
    seen = []
    original = SectionLocator.__init__

    def record(self, *args, **kwargs):
        seen.append(kwargs["trigger_chars"])
        original(self, *args, **kwargs)

    monkeypatch.setattr(SectionLocator, "__init__", record)
    # Targeting selects nothing for a document over the budget: held, not sliced or sent.
    assert _codes(_execute_extraction(_prepare_extraction(inputs))) == ["targeting_no_usable_scope"]
    assert seen == [5]
    provider.assert_not_called()


def test_selected_text_over_the_budget_is_held_not_sliced(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    inputs["max_context"] = 20
    _targeting(inputs, trigger_chars=1)
    _sections(monkeypatch, default="selected section " * 3)
    assert _codes(_execute_extraction(_prepare_extraction(inputs))) == [
        "targeting_context_budget_exceeded"
    ]
    provider.assert_not_called()


def test_source_context_prefix_counts_toward_the_budget(reviewed_inputs, monkeypatch):
    from psweep.extraction.provenance import SourceContext, prepend_source_context

    inputs, provider = reviewed_inputs
    info = SourceContext("https://example.org/source", "target", "1", "Label", {})
    source_only = len("Source evidence")
    with_hints = len(prepend_source_context("Source evidence", info))
    assert with_hints > source_only
    _targeting(inputs, trigger_chars=1000)
    _sections(monkeypatch)
    monkeypatch.setattr(pipeline, "source_context", lambda *_a: info)
    inputs["max_context"] = with_hints - 1  # the source alone would fit
    assert _codes(_execute_extraction(_prepare_extraction(inputs))) == [
        "targeting_context_budget_exceeded"
    ]
    provider.assert_not_called()
    inputs["max_context"] = with_hints
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert len(provider.call_args.args[0]) == with_hints


@pytest.mark.parametrize("pdf", [False, True])
def test_incomplete_but_bounded_selection_runs_and_is_never_reused(
    reviewed_inputs, monkeypatch, pdf
):
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    _targeting(inputs, trigger_chars=1)
    if pdf:
        (root / "doc.txt").unlink()
        (root / "doc.pdf").write_bytes(b"fixture")

        def locate(self, _doc):
            self.incomplete = True
            return PageSelection(((2, 3),), ((9, 9),))

        monkeypatch.setattr("psweep.extraction.page_locator.PageLocator.locate", locate)
        monkeypatch.setattr(
            "psweep.extraction.document_utils.extract_text_from_document",
            lambda *_a, **_k: (
                "pages two and three",
                {
                    "page_ranges": ((2, 3),),
                    "page_count": 9,
                    "selected_pages": [2, 3],
                    "extracted_pages": [2, 3],
                },
            ),
        )
        record_path = Path(inputs["output"]) / "doc.json"
    else:
        _sections(monkeypatch, default="selected section", incomplete=True)
        record_path = Path(inputs["output"]) / "doc.json"
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    coverage = json.loads(record_path.read_text(encoding="utf-8"))["processing_metrics"]["coverage"]
    assert coverage["targeting_incomplete"] is True
    if pdf:
        assert coverage["omitted_page_ranges"] == [[9, 9]]
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert provider.call_count == 2


def test_budget_omissions_with_complete_targeting_are_not_a_failure(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "doc.txt").unlink()
    (root / "doc.pdf").write_bytes(b"fixture")
    _targeting(inputs, trigger_chars=1)
    monkeypatch.setattr(
        "psweep.extraction.page_locator.PageLocator.locate",
        lambda _self, _doc: PageSelection(((2, 3),), ((9, 9),)),
    )
    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document",
        lambda *_a, **_k: (
            "pages two and three",
            {
                "page_ranges": ((2, 3),),
                "page_count": 9,
                "selected_pages": [2, 3],
                "extracted_pages": [2, 3],
            },
        ),
    )
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    coverage = json.loads((Path(inputs["output"]) / "doc.json").read_text("utf-8"))[
        "processing_metrics"
    ]["coverage"]
    assert coverage["omitted_page_ranges"] == [[9, 9]]
    assert "targeting_incomplete" not in coverage
    provider.assert_called_once()


def test_locator_exception_is_a_document_failure_not_a_batch_abort(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "bad.txt").write_text("Source evidence", encoding="utf-8")
    _targeting(inputs, trigger_chars=1000)

    def locate(self, doc):
        self.incomplete = False
        if doc.name == "bad.txt":
            raise RuntimeError("locator exploded")

    monkeypatch.setattr("psweep.extraction.section_locator.SectionLocator.locate", locate)
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert (outcome.total, outcome.successful, outcome.failed) == (2, 1, 1)
    assert outcome.errors[0]["file"] == "bad.txt"
    assert provider.call_count == 1


def test_manual_blank_row_bypasses_section_locator_for_non_pdf(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    pages = Path(inputs["path"]).parent / "pages.csv"
    pages.write_text("file_path,start_page,end_page\ndoc.txt,,\n", encoding="utf-8")
    inputs["pages_csv"] = str(pages)
    _targeting(inputs, trigger_chars=1)
    calls = []
    _sections(monkeypatch, incomplete=True, calls=calls)
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    assert calls == []
    provider.assert_called_once()


def test_manual_mapping_presence_is_part_of_the_record_fingerprint(reviewed_inputs, tmp_path):
    inputs, _ = reviewed_inputs
    _targeting(inputs)
    doc = next(Path(inputs["path"]).glob("doc.txt"))

    def fingerprint(**extra):
        return _prepare_extraction({**inputs, **extra})["fingerprints"][doc]

    def csv_with(name):
        path = tmp_path / name
        path.write_text("file_path,start_page,end_page\ndoc.txt,,\n", encoding="utf-8")
        return str(path)

    automatic = fingerprint()
    manual_full = fingerprint(pages_csv=csv_with("one.csv"))
    assert manual_full != automatic
    assert fingerprint(pages_csv=csv_with("two.csv")) == manual_full
    assert fingerprint() == automatic


def _rewrite_record(output: Path, mutate):
    """Edit a written record and keep the manifest's digest evidence matching."""
    from psweep.utils.file_io import artifact_sha256

    path = output / "doc.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    mutate(record)
    path.write_text(json.dumps(record), encoding="utf-8")
    for manifest in (output / "run_manifests").glob("*.manifest.json"):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for entry in data.get("reuse", {}).values():
            entry["record_sha256"] = artifact_sha256(path)
        manifest.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("flag", ["context_truncated", "targeting_incomplete"])
@pytest.mark.parametrize("protected", [True, False])
def test_matching_proof_unsafe_record_is_rejected_only_in_protected_mode(
    reviewed_inputs, monkeypatch, flag, protected
):
    inputs, _ = reviewed_inputs
    if protected:
        _targeting(inputs, trigger_chars=1000)
        _sections(monkeypatch)
    assert _execute_extraction(_prepare_extraction(inputs)).exit_code == 0
    output = Path(inputs["output"])
    _rewrite_record(output, lambda r: r["processing_metrics"]["coverage"].__setitem__(flag, True))
    plan = _prepare_extraction(inputs)
    assert bool(plan["pending"]) is protected
    assert bool(plan["reused"]) is not protected


def _sentinel_output(inputs):
    output = Path(inputs["output"])
    output.mkdir()
    (output / "doc.json").write_bytes(b"prior record bytes")
    (output / "other.json").write_bytes(b"other prior bytes")
    return output


@pytest.mark.parametrize("cause", ["incomplete", "overflow"])
def test_fresh_safety_block_preserves_prior_payloads(reviewed_inputs, monkeypatch, cause):
    inputs, provider = reviewed_inputs
    output = _sentinel_output(inputs)
    if cause == "incomplete":
        _targeting(inputs, trigger_chars=1000)
        _sections(monkeypatch, incomplete=True)
    else:
        inputs["max_context"] = 3
        _targeting(inputs, trigger_chars=1000)
        _sections(monkeypatch)
    outcome = _execute_extraction(_prepare_extraction(inputs, fresh=True))
    assert (outcome.status, outcome.exit_code) == ("failed", 1)
    assert (output / "doc.json").read_bytes() == b"prior record bytes"
    assert (output / "other.json").read_bytes() == b"other prior bytes"
    provider.assert_not_called()
    manifest = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))
    assert manifest["costs"]["llm_calls"] == 0
    assert manifest["status"]["result"] == "failed"


def test_fresh_preflight_pass_resets_once_and_does_not_retarget(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    output = _sentinel_output(inputs)
    _targeting(inputs, trigger_chars=1000)
    calls = []
    _sections(monkeypatch, calls=calls)
    assert _execute_extraction(_prepare_extraction(inputs, fresh=True)).exit_code == 0
    assert calls == ["doc.txt"]
    assert not (output / "other.json").exists()
    provider.assert_called_once()


def test_blocked_documents_report_no_fabricated_primary_usage(reviewed_inputs, monkeypatch):
    inputs, _ = reviewed_inputs
    root = Path(inputs["path"])
    (root / "held.txt").write_text("Source evidence", encoding="utf-8")
    inputs["max_concurrent"] = 1  # one worker-local locator, so metered calls are exact
    _targeting(inputs, trigger_chars=1000)

    def locate(self, doc):
        self.incomplete = doc.name == "held.txt"
        self._client = SimpleNamespace(
            usage={
                "calls": 2,
                "cost": 0.5,
                "input_tokens": 30,
                "output_tokens": 4,
                "unknown_cost_calls": 0,
            },
            raw_model="locator-model",
        )

    monkeypatch.setattr("psweep.extraction.section_locator.SectionLocator.locate", locate)
    outcome = _execute_extraction(_prepare_extraction(inputs))
    costs = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))["costs"]
    assert costs["llm_calls"] == 1 + 2  # one primary request plus the metered locator calls
    assert costs["documents_billed"] == 1
    assert outcome.status == "failed"


def test_safety_block_is_terminal_even_with_successful_siblings(reviewed_inputs, monkeypatch):
    inputs, _ = reviewed_inputs
    root = Path(inputs["path"])
    (root / "held.txt").write_text("Source evidence", encoding="utf-8")
    output = Path(inputs["output"])
    output.mkdir()
    (output / "held.json").write_bytes(b"historical held record")
    _targeting(inputs, trigger_chars=1000)
    _sections(monkeypatch, by_name={"held.txt": (None, True)})
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert (outcome.successful, outcome.failed, outcome.status) == (1, 1, "failed")
    assert (output / "held.json").read_bytes() == b"historical held record"

    ran = []

    def run(cmd, check=False):
        ran.append(cmd[0])
        return SimpleNamespace(returncode=outcome.exit_code if cmd[0] == "extract" else 0)

    monkeypatch.setattr(pipeline.subprocess, "run", run)
    with pytest.raises(pipeline.PipelineError):
        pipeline.execute_run_stages([("extract", ["extract"]), ("compile", ["compile"])])
    assert ran == ["extract"]


def test_ordinary_provider_failure_keeps_partial_semantics(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "b.txt").write_text("Source evidence", encoding="utf-8")
    ok = provider.return_value
    provider.side_effect = [RuntimeError("provider down"), ok]
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert (outcome.status, outcome.exit_code) == ("partial_failure", 3)
    ran = []

    def run(cmd, check=False):
        ran.append(cmd[0])
        return SimpleNamespace(returncode=outcome.exit_code if cmd[0] == "extract" else 0)

    monkeypatch.setattr(pipeline.subprocess, "run", run)
    stages = [("extract", ["extract"]), ("compile", ["compile"])]
    assert pipeline.execute_run_stages(stages) == ["extract"]
    assert ran == ["extract", "compile"]


def test_saved_ranges_do_not_advertise_a_blocked_selection(reviewed_inputs, monkeypatch):
    inputs, _ = reviewed_inputs
    root = Path(inputs["path"])
    (root / "doc.txt").unlink()
    (root / "a.pdf").write_bytes(b"first")
    inputs["max_context"] = 5
    _targeting(inputs, trigger_chars=1, save_discovered=True)
    monkeypatch.setattr(
        "psweep.extraction.page_locator.PageLocator.locate",
        lambda _self, _doc: PageSelection(((3, 4),)),
    )
    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document",
        lambda *_a, **_k: (
            "far too long for the budget",
            {
                "page_ranges": ((3, 4),),
                "page_count": 9,
                "selected_pages": [3, 4],
                "extracted_pages": [3, 4],
            },
        ),
    )
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert _codes(outcome) == ["targeting_context_budget_exceeded"]
    saved = (Path(inputs["output"]) / "discovered_page_ranges.csv").read_text("utf-8")
    assert "a.pdf" not in saved


def test_incomplete_pdf_targeting_is_held_without_parsing_or_a_primary_call(
    reviewed_inputs, monkeypatch
):
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "doc.txt").unlink()
    (root / "doc.pdf").write_bytes(b"fixture")
    _targeting(inputs, trigger_chars=1)

    def locate(self, _doc):
        self.incomplete = True

    monkeypatch.setattr("psweep.extraction.page_locator.PageLocator.locate", locate)
    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document",
        lambda *_a, **_k: pytest.fail("held document was parsed"),
    )
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert _codes(outcome) == ["targeting_no_usable_scope"]
    provider.assert_not_called()


@pytest.mark.parametrize("kind", ["disabled", "unsupported"])
@pytest.mark.parametrize(("limit", "held"), [(15, False), (14, True)])
def test_untargeted_inputs_are_held_not_sliced(reviewed_inputs, kind, limit, held):
    """Every path obeys max_context exactly; nothing is silently recropped."""
    inputs, provider = reviewed_inputs  # the source text is exactly 15 characters
    inputs["max_context"] = limit
    root = Path(inputs["path"])
    if kind == "unsupported":
        (root / "doc.txt").rename(root / "doc.csv")
        _targeting(inputs, trigger_chars=1)
    elif kind == "manual":
        (root / "pages.csv").write_text("file,pages\ndoc.txt,1-1\n", encoding="utf-8")
        inputs["pages_csv"] = str(root / "pages.csv")
        _targeting(inputs, trigger_chars=1)
    outcome = _execute_extraction(_prepare_extraction(inputs))
    if held:
        assert _codes(outcome) == ["targeting_context_budget_exceeded"]
        provider.assert_not_called()
        assert not (Path(inputs["output"]) / "doc.json").exists()
    else:
        assert outcome.exit_code == 0
        record = json.loads((Path(inputs["output"]) / "doc.json").read_text("utf-8"))
        assert not record["processing_metrics"]["coverage"]["context_truncated"]


@pytest.mark.parametrize(
    ("model", "match"),
    [(None, "auto_locate.model is required"), ("gpt-fixture", "resolves to the extraction model")],
)
def test_auto_targeting_requires_an_independent_locator_model(reviewed_inputs, model, match):
    inputs, provider = reviewed_inputs
    inputs["page_targeting"] = {"enabled": True, "section_description": "evidence"}
    if model:
        inputs["page_targeting"]["model"] = model
    with pytest.raises(ValueError, match=match):
        _prepare_extraction(inputs)
    provider.assert_not_called()


def test_manual_page_scope_cannot_bypass_the_budget(reviewed_inputs, monkeypatch):
    inputs, provider = reviewed_inputs
    root = Path(inputs["path"])
    (root / "doc.txt").unlink()
    (root / "a.pdf").write_bytes(b"first")
    pages = root.parent / "pages.csv"
    pages.write_text("file_path,start_page,end_page\na.pdf,1,2\n", encoding="utf-8")
    inputs["pages_csv"] = str(pages)
    inputs["max_context"] = 5
    monkeypatch.setattr(
        "psweep.extraction.document_utils.extract_text_from_document",
        lambda *_a, **_k: (
            "far too long for the budget",
            {
                "page_ranges": ((1, 2),),
                "page_count": 9,
                "selected_pages": [1, 2],
                "extracted_pages": [1, 2],
            },
        ),
    )
    outcome = _execute_extraction(_prepare_extraction(inputs))
    assert _codes(outcome) == ["targeting_context_budget_exceeded"]
    provider.assert_not_called()


def test_locator_version_changes_enabled_fingerprints_only(reviewed_inputs, monkeypatch):
    from psweep.extraction import page_locator

    inputs, _ = reviewed_inputs
    doc = Path(inputs["path"]) / "doc.txt"

    def fingerprint(enabled):
        data = dict(inputs)
        if enabled:
            _targeting(data)
        return _prepare_extraction(data)["fingerprints"][doc]

    before = fingerprint(False), fingerprint(True)
    monkeypatch.setattr(page_locator, "LOCATOR_BEHAVIOR_VERSION", "future")
    after = fingerprint(False), fingerprint(True)
    assert after[0] == before[0]
    assert after[1] != before[1]
