"""Partial search results remain usable without hiding transport failures."""

import csv
import json
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner
from requests.exceptions import HTTPError, ReadTimeout

from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.connectors.base import SeekerInput
from psweep.discovery.connectors.serpapi_seeker import SerpApiSeeker


def _response(name):
    return {"organic_results": [{"link": f"https://example.gov/{name}.pdf"}]}


def _seeker(monkeypatch, search, **kwargs):
    monkeypatch.setattr(SerpApiSeeker, "_build_search_callable", lambda self: search)
    return SerpApiSeeker(
        api_key="fixture-key", retry_initial_backoff_seconds=0, **kwargs
    )


def _queries(*queries):
    return SeekerInput(query="", extra_params={"query_templates": list(queries)})


@pytest.mark.parametrize("timeout", [ReadTimeout("read timed out"), TimeoutError("timed out")])
def test_later_query_timeout_retains_results_and_continues(monkeypatch, timeout):
    search = Mock(side_effect=[_response("first"), timeout, _response("last")])
    seeker = _seeker(monkeypatch, search, retry_max_attempts=1)

    with pytest.raises(RuntimeError) as caught:
        seeker.discover(_queries("first query", "timeout query", "last query"))

    assert [item["url"] for item in caught.value.candidates] == [
        "https://example.gov/first.pdf", "https://example.gov/last.pdf",
    ]
    assert len(caught.value.query_errors) == 1
    assert "timeout query" in caught.value.query_errors[0]
    assert seeker.queries_run == search.call_count == 3


def test_all_queries_timeout_still_raises_and_counts_attempts(monkeypatch):
    search = Mock(side_effect=[ReadTimeout("first timeout"), ReadTimeout("second timeout")])
    seeker = _seeker(monkeypatch, search, retry_max_attempts=1)

    with pytest.raises(RuntimeError) as caught:
        seeker.discover(_queries("first", "second"))

    assert caught.value.candidates == []
    assert len(caught.value.query_errors) == 2
    assert seeker.queries_run == search.call_count == 2


def test_timeout_retries_count_every_live_attempt(monkeypatch):
    search = Mock(side_effect=[_response("first"), ReadTimeout("timeout"), ReadTimeout("timeout")])
    seeker = _seeker(monkeypatch, search, retry_max_attempts=2)

    with pytest.raises(RuntimeError) as caught:
        seeker.discover(_queries("first", "second"))

    assert len(caught.value.candidates) == 1
    assert seeker.queries_run == search.call_count == 3


def test_builtin_timeout_retries_by_type_before_partial_failure(monkeypatch):
    search = Mock(side_effect=[TimeoutError("timed out"), _response("recovered")])
    seeker = _seeker(monkeypatch, search, retry_max_attempts=3)

    candidates = seeker.discover(SeekerInput(query="retry query"))

    assert len(candidates) == 1
    assert search.call_count == seeker.queries_run == 2


def test_cache_hit_is_retained_without_counting_a_live_attempt(monkeypatch, tmp_path):
    search = Mock(side_effect=[_response("cached"), ReadTimeout("timeout api_key=fixture-key")])
    seeker = _seeker(monkeypatch, search, retry_max_attempts=1, cache_dir=str(tmp_path))
    seeker.discover(SeekerInput(query="cached query"))
    seeker.queries_run = 0

    with pytest.raises(RuntimeError) as caught:
        seeker.discover(_queries("cached query", "timeout query"))

    assert len(caught.value.candidates) == 1
    assert seeker.queries_run == 1
    assert "fixture-key" not in str(caught.value)
    assert all("fixture-key" not in error for error in caught.value.query_errors)


@pytest.mark.parametrize("failure", [
    HTTPError("401 Unauthorized"),
    ValueError("invalid configuration"),
    RuntimeError("invalid timeout configuration field"),
])
def test_non_timeout_failures_remain_strict(monkeypatch, failure):
    search = Mock(side_effect=[_response("first"), failure, _response("unreachable")])
    seeker = _seeker(monkeypatch, search, retry_max_attempts=1)

    with pytest.raises(RuntimeError, match="SerpApi search failed") as caught:
        seeker.discover(_queries("first", "second", "third"))

    assert not hasattr(caught.value, "candidates")
    assert search.call_count == seeker.queries_run == 2


def _engine_request(tmp_path, *, dry_run=True):
    return DiscoveryRequest(
        domain="timeout-test",
        seed_urls=[],
        query=None,
        enable_serpapi=True,
        output_documents=tmp_path / "domain/runs/probe/documents",
        output_manifest=tmp_path / "domain/runs/probe/manifest.json",
        dry_run=dry_run,
        targets=[{"label": label} for label in ["first", "partial", "last"]],
        query_templates=["{label} comments", "{label} hearing"],
        selection_primary_per_target=1,
        link_prioritization_mode="off",
        partition_by=["label"],
        retry_max_attempts=1,
        robots_policy_mode="ignore",
        tos_policy_mode="ignore",
    )


def _mock_engine_search(monkeypatch):
    def search(params):
        if params["q"] == "partial hearing":
            raise ReadTimeout("read timed out")
        return _response(params["q"].replace(" ", "-"))

    monkeypatch.setenv("SERPAPI_API_KEY", "fixture-key")
    monkeypatch.setattr(SerpApiSeeker, "_build_search_callable", lambda self: search)
    monkeypatch.setattr(
        DiscoveryEngine, "_resolve_serpapi_state",
        staticmethod(lambda request: (
            {"provider": "serpapi", "enabled": True, "available": True}, [], [],
        )),
    )


def test_engine_keeps_earlier_and_later_targets_with_explicit_partial_status(monkeypatch, tmp_path):
    _mock_engine_search(monkeypatch)
    result = DiscoveryEngine().run(_engine_request(tmp_path))
    manifest = json.loads(result.manifest_path.read_text())

    assert len(manifest["candidates"]) == 3
    assert manifest["status"] == "scaffold_dry_run_with_errors"
    assert manifest["stage_summaries"]["seeker"]["queries_executed"] == 6
    assert manifest["stage_summaries"]["seeker"]["candidates_discovered"] == 5
    assert len(manifest["errors"]) == 1
    assert "partial hearing" in manifest["errors"][0]["message"]
    metrics = manifest["stage_summaries"]["acceptance_metrics"]["unknown_target_discovery"]["targets"]
    assert metrics[1]["search_complete"] is False
    assert [item["input_candidates"] for item in metrics] == [2, 1, 2]


def test_engine_non_timeout_error_does_not_turn_into_partial_success(monkeypatch, tmp_path):
    _mock_engine_search(monkeypatch)
    search = Mock(side_effect=[
        _response("first-comments"), _response("first-hearing"),
        HTTPError("401 Unauthorized"),
    ])
    monkeypatch.setattr(SerpApiSeeker, "_build_search_callable", lambda self: search)
    result = DiscoveryEngine().run(_engine_request(tmp_path))
    manifest = json.loads(result.manifest_path.read_text())

    assert [candidate["url"] for candidate in manifest["candidates"]] == [
        "https://example.gov/first-comments.pdf"
    ]
    assert manifest["status"] == "scaffold_dry_run_with_errors"
    assert manifest["stage_summaries"]["seeker"]["queries_executed"] == 4
    assert any("401 Unauthorized" in error["message"] for error in manifest["errors"])
    metrics = manifest["stage_summaries"]["acceptance_metrics"]["unknown_target_discovery"]["targets"]
    assert metrics[1]["search_complete"] is False
    assert metrics[2]["search_complete"] is False
    assert search.call_count == 4


def test_partial_target_is_not_checkpointed_as_complete(monkeypatch, tmp_path):
    _mock_engine_search(monkeypatch)
    engine = DiscoveryEngine()

    def download(*, request, candidates, documents_dir, max_downloads):
        return [
            {"url": candidate.url, "status": "downloaded", "target_metadata": candidate.target_metadata}
            for candidate in candidates
        ], [], []

    monkeypatch.setattr(engine, "_download_candidates", download)
    engine.run(_engine_request(tmp_path, dry_run=False))
    checkpoint = json.loads((tmp_path / "domain/checkpoint.json").read_text())

    assert set(checkpoint["entries"]) == {"first", "last"}


@pytest.mark.parametrize("fresh", [False, True])
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("partial_hit", [False, True])
def test_incomplete_shared_partition_stays_run_local(
    monkeypatch, tmp_path, fresh, selected, partial_hit
):
    _mock_engine_search(monkeypatch)
    if not partial_hit:
        def search(params):
            if params["q"].startswith("partial "):
                raise ReadTimeout("read timed out")
            return _response(params["q"].replace(" ", "-"))

        monkeypatch.setattr(SerpApiSeeker, "_build_search_callable", lambda self: search)
    request = _engine_request(tmp_path, dry_run=False)
    request.reprocess = fresh
    request.partition_by = ["state"]
    request.targets = [
        {"label": "partial", "state": "Washington"},
        {"label": "first", "state": "Washington"},
        {"label": "last", "state": "California"},
    ]
    domain = tmp_path / "domain"
    prior = domain / "curated/by_state/washington/partial-comments.pdf"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"prior completed public comments")
    old_index = domain / "download_index.csv"
    old_index.write_text(
        "relative_path,url\n"
        "by_state/washington/partial-comments.pdf,https://example.gov/prior.pdf\n"
    )
    engine = DiscoveryEngine()

    def download(*, request, candidates, documents_dir, max_downloads):
        records = []
        for candidate in candidates:
            relative = (
                "by_state/" + candidate.target_metadata["state"].lower()
                + "/" + candidate.url.rsplit("/", 1)[-1]
            )
            path = documents_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"new partial or complete public comments")
            records.append({
                "url": candidate.url,
                "status": "downloaded",
                "path": str(path),
                "relative_path": relative,
                "target_metadata": candidate.target_metadata,
                "review_selected": selected,
            })
        return records, [], []

    monkeypatch.setattr(engine, "_download_candidates", download)
    result = engine.run(request)

    assert prior.read_bytes() == b"prior completed public comments"
    assert not (domain / "curated/by_state/washington/first-comments.pdf").exists()
    assert (result.documents_dir / "by_state/washington/partial-comments.pdf").exists() is partial_hit
    if selected:
        assert (result.curated_dir / "by_state/washington/partial-comments.pdf").exists() is partial_hit
        assert (domain / "curated/by_state/california/last-comments.pdf").exists()
    with old_index.open() as stream:
        rows = list(csv.DictReader(stream))
    by_path = {row["relative_path"]: row for row in rows}
    assert by_path["by_state/washington/partial-comments.pdf"]["url"] == "https://example.gov/prior.pdf"
    assert "by_state/washington/first-comments.pdf" not in by_path
    checkpoint = json.loads((domain / "checkpoint.json").read_text())
    assert set(checkpoint["entries"]) == {
        DiscoveryEngine._target_checkpoint_key({"label": "last", "state": "California"})
    }
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == "scaffold_with_errors"


def test_same_named_counties_keep_independent_review_caps(monkeypatch, tmp_path):
    from discovery_helpers import FakeResponse

    from psweep.discovery.document_reviewer import DocumentReviewer

    _mock_engine_search(monkeypatch)

    def search(params):
        state = "CO" if " CO " in params["q"] else "PA"
        return {"organic_results": [
            {"link": f"https://example.gov/{state}-{number}.pdf"}
            for number in range(2)
        ]}

    monkeypatch.setattr(SerpApiSeeker, "_build_search_callable", lambda self: search)
    monkeypatch.setattr(
        "requests.get",
        lambda url, **kwargs: FakeResponse(url=url, chunks=[b"%PDF-1.4\n" + url.encode()]),
    )
    monkeypatch.setattr(DocumentReviewer, "_ensure_client", lambda self: object())
    contexts = []

    def grade(self, path, *, target_context="", source_url=""):
        contexts.append((path, target_context))
        return {"is_primary": True, "relevance": 0.9, "doc_kind": "public comment", "reason": "fixture"}

    monkeypatch.setattr(DocumentReviewer, "_grade", grade)
    request = _engine_request(tmp_path, dry_run=False)
    request.targets = [
        {"county": "Adams County", "state": "CO", "fips": "08001"},
        {"county": "Adams County", "state": "PA", "fips": "42001"},
    ]
    request.query_templates = ["{county} {state} wind public comments"]
    request.partition_by = ["state", "county"]
    request.selection_primary_per_target = 2
    request.document_review = {
        "document_description": "actual public wind comments in the target county",
        "keep_top": 1,
        "deduplicate_redundant": False,
    }
    result = DiscoveryEngine().run(request)
    manifest = json.loads(result.manifest_path.read_text())
    records = manifest["downloads"]
    selected = [record for record in records if record.get("review_selected")]

    assert len(records) == 4
    assert Counter(record["target_metadata"]["state"] for record in selected) == {"CO": 1, "PA": 1}
    assert {record["target_label"] for record in records} == {"Adams County, CO", "Adams County, PA"}
    assert all("label" not in record["target_metadata"] for record in records)
    groups = DiscoveryEngine._group_records_by_target_label(records)
    assert {key: len(value) for key, value in groups.items()} == {
        DiscoveryEngine._target_checkpoint_key(target): 2
        for target in request.targets
    }
    for path, context in contexts:
        state = "CO" if "/CO-" in path else "PA"
        assert f"state={state}" in context
    assert result.curated_count == 2


@pytest.mark.parametrize("target,expected", [
    ({"county": "Adams County", "state": "CO"}, "Adams County, CO"),
    ({"jurisdiction": "Adams County", "state": "PA"}, "Adams County, PA"),
    ({"label": "Chosen label", "county": "Adams County", "state": "CO"}, "Chosen label"),
    ({"site_name": "Wind Portal", "county": "Adams County", "state": "CO"}, "Wind Portal"),
    ({"company_name": "Wind Company", "state": "CO"}, "Wind Company"),
    ({"county": "Adams County"}, "Adams County"),
])
def test_implicit_geographic_target_labels_are_state_qualified(target, expected):
    assert DiscoveryEngine._target_label(target, 0) == expected


@pytest.mark.parametrize("with_errors", [False, True])
def test_cli_does_not_label_partial_discovery_complete(monkeypatch, tmp_path, with_errors):
    from psweep.cli.commands_discover import discover

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "status": "scaffold_dry_run_with_errors" if with_errors else "scaffold_dry_run",
        "notes": [],
        "errors": [{"stage": "discovery.seeker_discover", "message": "query timed out"}] if with_errors else [],
    }))
    monkeypatch.setattr(DiscoveryEngine, "run", lambda self, request: SimpleNamespace(
        manifest_path=manifest_path,
        documents_dir=tmp_path / "documents",
        run_id="timeout-cli-test",
        curated_dir=None,
        curated_count=0,
        review_index_path=None,
    ))
    result = CliRunner().invoke(discover, ["--query", "test", "--dry-run"])

    assert result.exit_code == 0, result.output
    if with_errors:
        assert "results may be partial" in result.output
        assert "Discovery complete" not in result.output
        assert "scaffold_dry_run_with_errors" in result.output
    else:
        assert "Discovery complete" in result.output
