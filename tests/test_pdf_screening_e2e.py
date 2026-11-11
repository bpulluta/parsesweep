"""Real PDF screening through discovery, curation, and extraction orchestration."""

import csv
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pymupdf
import pytest

from psweep.cli.commands_curate import _curate_run
from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.connectors.serpapi_seeker import SerpApiSeeker
from psweep.outcomes import EXIT_EMPTY
from psweep.pipeline import _execute_extraction, _prepare_extraction

from discovery_helpers import FakeResponse


BASE_URL = "https://agency.gov"


def _pdf_bytes(name):
    """Generate native text and, for the marked PDF, a flattened strike line."""
    with pymupdf.open() as document:
        page = document.new_page()
        phrase = f"Source evidence for {name}."
        page.insert_text((72, 72), phrase, fontsize=12)
        for index in range(20):
            page.insert_text(
                (72, 110 + index * 20),
                f"Section {index + 1}. Public notice is required before work begins.",
                fontsize=11,
            )
        if name == "marked":
            rectangle = page.search_for(phrase)[0]
            middle = rectangle.y0 + rectangle.height * 0.5
            page.draw_line((rectangle.x0, middle), (rectangle.x1, middle), width=0.7)
        return document.tobytes()


@pytest.fixture
def screening_workflow(tmp_path, monkeypatch):
    """Stub only network/model boundaries; keep PDF parsing and policy real."""
    monkeypatch.chdir(tmp_path)
    llm = {"provider": "openai", "model": "gpt-fixture", "api_key": "fixture"}
    monkeypatch.setattr("psweep.utils.config.get_config", lambda: SimpleNamespace(llm_config=llm))
    monkeypatch.setenv("SERPAPI_API_KEY", "fixture")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "socket.socket.connect", Mock(side_effect=AssertionError("Unexpected external connection"))
    )
    payloads = {name: _pdf_bytes(name) for name in ("marked", "clean")}
    search = Mock(
        side_effect=lambda params: {
            "organic_results": [{"link": f"{BASE_URL}/{name}.pdf"} for name in payloads]
        }
    )
    monkeypatch.setattr(SerpApiSeeker, "_build_search_callable", lambda self: search)

    def fetch(url, **kwargs):
        name = Path(url).stem
        assert url == f"{BASE_URL}/{name}.pdf"
        return FakeResponse(url=url, chunks=[payloads[name]])

    downloads = Mock(side_effect=fetch)
    monkeypatch.setattr("psweep.discovery.network.request_get", downloads)
    review = Mock(
        return_value={
            "data": {"grade": 4, "doc_kind": "source", "reason": "Relevant source evidence"},
            "cost": 0.0,
            "input_tokens": 10,
            "output_tokens": 5,
        }
    )

    def extract(text, schema):
        name = next(name for name in payloads if f"Source evidence for {name}." in text)
        return {
            "data": {"source": {"id": name}, "items": [{"text": f"Source evidence for {name}."}]},
            "cost": 0.0,
            "input_tokens": 10,
            "output_tokens": 5,
        }

    extraction = Mock(side_effect=extract)

    def model_call(self, text, schema, **kwargs):
        assert "Section 1." in text  # Evidence must come through the real native PDF parser.
        if "grade" in schema["properties"]:
            return review(text, schema)
        return extraction(text, schema)

    monkeypatch.setattr("psweep.extraction.llm_client.LLMClient.extract", model_call)
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "$metadata": {"extraction": {"main_data_array": "items"}},
                "type": "object",
                "required": ["source", "items"],
                "properties": {
                    "source": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {"id": {"type": "string"}},
                    },
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["text"],
                            "properties": {"text": {"type": "string"}},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return SimpleNamespace(
        payloads=payloads,
        search=search,
        downloads=downloads,
        review=review,
        extraction=extraction,
        root=tmp_path / "outputs/synthetic/discovered/curated",
        inputs={
            "path": str(tmp_path / "outputs/synthetic/discovered/curated"),
            "schema": str(schema),
            "output": str(tmp_path / "outputs/synthetic/extracted"),
            "llm_config": llm,
            "max_concurrent": 1,
        },
    )


def _discover(mode, *, retention="all", configured=None):
    sources = {"queries": ["{label}"], "targets": [{"label": "sample"}]}
    if configured == "shared":
        sources = {"sources": [f"{BASE_URL}/marked.pdf"]}
    elif configured == "target":
        sources = {"targets": [{"label": "sample", "urls": [f"{BASE_URL}/marked.pdf"]}]}
    return DiscoveryEngine().run(
        DiscoveryRequest(
            domain="synthetic",
            **sources,
            partition_by=["label"],
            robots_policy_mode="ignore",
            tos_policy_mode="ignore",
            retry_max_attempts=1,
            max_concurrent_downloads=1,
            selection_primary_per_target=10,
            document_classifier={"pdf_screening": mode},
            document_review={
                "document_description": "Source evidence records",
                "model": "gpt-fixture",
                "keep_top": 10,
                "deduplicate_redundant": False,
            },
            retention_documents=retention,
        )
    )


def _downloads(result):
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    return {Path(record["url"]).stem: record for record in manifest["downloads"]}


def _ledger(result):
    with result.review_index_path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _extract(workflow):
    # Pass the authoritative corpus root to production planning, never a hand-picked list.
    return _execute_extraction(_prepare_extraction(workflow.inputs))


@pytest.mark.parametrize(("mode", "expected"), [("warn", {"marked", "clean"}), ("hold", {"clean"})])
def test_screening_controls_real_discovery_to_extraction(screening_workflow, mode, expected):
    workflow = screening_workflow
    result = _discover(mode)
    assert not result.errors
    records = _downloads(result)
    assert records["marked"]["document_eligibility"]["state"] == "flagged"
    assert records["clean"]["document_eligibility"]["state"] == "no_findings"
    assert records["clean"]["review_selected"] is True
    if mode == "warn":
        assert records["marked"]["review_selected"] is True
        assert workflow.review.call_count == 2
    assert {path.stem for path in workflow.root.rglob("*.pdf")} == expected
    rows = {Path(row["relative_path"]).stem: row for row in _ledger(result)}
    assert rows["marked"]["eligibility_mode"] == mode
    assert rows["marked"]["eligibility_state"] == "flagged"
    assert rows["marked"]["curated_selected"] == str(mode == "warn")

    outcome = _extract(workflow)
    assert outcome.exit_code == 0, outcome.errors
    assert outcome.successful == len(expected)
    assert {Path(record["file"]).stem for record in outcome.records} == expected
    assert workflow.extraction.call_count == len(expected)
    for call in workflow.extraction.call_args_list:
        assert "SOURCE_URL:" in call.args[0]
        assert "Section 1." in call.args[0]


def test_policy_change_invalidates_reuse_and_withdraws_old_extraction(screening_workflow):
    workflow = screening_workflow
    assert _discover("warn").curated_count == 2
    first = _extract(workflow)
    assert first.exit_code == 0
    marked_output = next(
        Path(record["output_path"]) for record in first.records if record["file"] == "marked.pdf"
    )
    assert marked_output.is_file()
    workflow.downloads.reset_mock()
    workflow.search.reset_mock()
    workflow.extraction.reset_mock()
    assert _discover("warn").status == "reused"
    assert _extract(workflow).status == "reused"
    workflow.downloads.assert_not_called()
    workflow.search.assert_not_called()
    workflow.extraction.assert_not_called()

    held = _discover("hold")
    assert not held.errors
    assert held.curated_count == 1
    assert _downloads(held)["marked"]["document_eligibility"]["mode"] == "hold"
    assert {path.stem for path in workflow.root.rglob("*.pdf")} == {"clean"}
    outcome = _extract(workflow)
    assert outcome.exit_code == 0, outcome.errors
    assert outcome.withdrawn == [marked_output]
    assert not marked_output.exists()
    assert {Path(record["file"]).stem for record in outcome.records} == {"clean"}
    assert all(
        "Source evidence for marked." not in call.args[0]
        for call in workflow.extraction.call_args_list
    )


@pytest.mark.parametrize("retention", ["curated", "none"])
def test_all_held_retains_reviewable_original_and_extracts_nothing(screening_workflow, retention):
    workflow = screening_workflow
    workflow.payloads.pop("clean")
    result = _discover("hold", retention=retention)
    assert not result.errors
    assert result.curated_count == 0
    record = _downloads(result)["marked"]
    assert Path(record["path"]).read_bytes() == workflow.payloads["marked"]
    (row,) = _ledger(result)
    assert row["eligibility_state"] == "flagged"
    assert row["curated_selected"] == "False"
    assert not list(workflow.root.rglob("*.pdf"))
    outcome = _extract(workflow)
    assert outcome.exit_code == EXIT_EMPTY
    assert outcome.total == 0
    assert not outcome.records and not outcome.errors
    workflow.extraction.assert_not_called()


def test_human_keep_releases_retained_held_original(screening_workflow):
    workflow = screening_workflow
    workflow.payloads.pop("clean")
    result = _discover("hold", retention="curated")
    record = _downloads(result)["marked"]
    original = Path(record["path"])
    assert original.read_bytes() == workflow.payloads["marked"]
    assert not list(workflow.root.rglob("*.pdf"))
    rows = _ledger(result)
    rows[0].update(human_decision="keep", human_notes="Reviewed original source")
    with result.review_index_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    _curated, count, reviewed, overridden = _curate_run(result.manifest_path.parent)
    assert (count, reviewed, overridden) == (1, 1, 1)
    (published,) = workflow.root.rglob("*.pdf")
    assert published.read_bytes() == original.read_bytes() == workflow.payloads["marked"]
    updated = _downloads(result)["marked"]
    assert updated["human_decision"] == "keep"
    assert updated["document_eligibility"]["state"] == "flagged"
    outcome = _extract(workflow)
    assert outcome.exit_code == 0, outcome.errors
    assert outcome.successful == 1
    workflow.extraction.assert_called_once()
    assert "Source evidence for marked." in workflow.extraction.call_args.args[0]


@pytest.mark.parametrize("configured", ["shared", "target"])
@pytest.mark.parametrize(("mode", "expected_count"), [("off", 1), ("warn", 1), ("hold", 0)])
def test_explicit_sources_obey_enabled_screening(
    screening_workflow, configured, mode, expected_count
):
    workflow = screening_workflow
    workflow.payloads.pop("clean")
    result = _discover(mode, configured=configured)
    assert not result.errors
    record = _downloads(result)["marked"]
    assert record["source"] == "configured"
    assert record["status"] == "downloaded"
    assert result.curated_count == expected_count
    if mode == "off":
        assert "document_eligibility" not in record
    else:
        assert record["document_eligibility"]["state"] == "flagged"
    outcome = _extract(workflow)
    assert outcome.exit_code == (0 if expected_count else EXIT_EMPTY), outcome.errors
    assert outcome.successful == expected_count
    assert workflow.extraction.call_count == expected_count
    workflow.search.assert_not_called()
