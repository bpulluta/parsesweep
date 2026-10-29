"""Mocked integration regressions for safe discovery consolidation."""

import csv
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from discovery_helpers import FakeResponse, make_candidate

from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.document_reviewer import DocumentReviewer
from psweep.discovery.publication import (
    artifact_sha256, effective_discovery_fingerprint,
)
from psweep.extraction.provenance import (
    build_source_context_map,
    prepend_source_context,
)


def _request(tmp_path, **overrides):
    values = dict(
        domain="synthetic", seed_urls=[], query=None, enable_serpapi=False,
        output_documents=tmp_path / "domain/runs/probe/documents",
        output_manifest=tmp_path / "domain/runs/probe/manifest.json",
        dry_run=False, robots_policy_mode="ignore", tos_policy_mode="ignore",
    )
    values.update(overrides)
    return DiscoveryRequest(**values)


def _download(engine, request, candidate, browser=None):
    return engine._download_single_candidate(
        idx=1, candidate=candidate, request=request,
        documents_dir=request.output_documents, ssl_verify=True,
        retry_policy={"max_attempts": 1, "initial_backoff_seconds": 0,
                      "max_backoff_seconds": 0},
        rate_limiter=None, browser=browser,
    )


@pytest.mark.parametrize("url", [
    "https://agency.gov/conf/comment.pdf",
    "https://agency.gov/%63onf/comment.pdf",
    "https://agency.gov/comment.pdf?token=blocked",
    "https://agency.gov/comment.pdf?token\\u003dblocked",
])
def test_excluded_seed_never_fetches(monkeypatch, tmp_path, url):
    fetch = Mock(side_effect=AssertionError("excluded seed was fetched"))
    monkeypatch.setattr("requests.get", fetch)
    request = _request(tmp_path, seed_urls=[url],
                       selection_exclude_url_patterns=[r"/conf/", r"token=blocked"])
    result = DiscoveryEngine().run(request)
    assert fetch.call_count == 0
    assert json.loads(result.manifest_path.read_text())["candidates"] == []


@pytest.mark.parametrize("browser", [False, True])
def test_final_redirect_policy_blocks_download(monkeypatch, tmp_path, browser):
    final = "https://unapproved.example.org/conf/comment.pdf"
    request = _request(tmp_path, allowed_domains=["agency.gov"],
                       selection_exclude_url_patterns=[r"/conf/"])
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: FakeResponse(url=final))
    session = SimpleNamespace(download_bytes=lambda url: (b"PDF", "application/pdf", final))
    _, record, error, downloaded, _ = _download(
        DiscoveryEngine(), request, make_candidate("https://agency.gov/comment.pdf"),
        session if browser else None,
    )
    assert not downloaded and error is None
    assert record["final_url"] == final
    assert record["status"] == "skipped_selection_filter"
    assert not list(tmp_path.rglob("*.pdf"))


def test_browser_unknown_final_origin_is_not_invented(tmp_path):
    browser = SimpleNamespace(download_bytes=lambda url: (b"PDF", "application/pdf", ""))
    _, record, error, downloaded, _ = _download(
        DiscoveryEngine(), _request(tmp_path), make_candidate("https://agency.gov/comment.pdf"), browser,
    )
    assert not downloaded
    assert error is not None
    assert record.get("final_url") != "https://agency.gov/comment.pdf"


def test_label_collision_does_not_share_checkpoint_identity():
    first = {"label": "shared", "state": "CA"}
    second = {"label": "shared", "state": "CO"}
    assert DiscoveryEngine._target_checkpoint_key(first) != DiscoveryEngine._target_checkpoint_key(second)


def test_host_only_seed_attribution_is_unresolved(tmp_path):
    candidates = [make_candidate("https://agency.gov/GetDocument.pdf")]
    DiscoveryEngine._attribute_seeds_to_targets(candidates, [{"label": "Agency", "agency": "agency.gov"}])
    assert not candidates[0].target_metadata


def test_shared_host_seed_attribution_is_ambiguous(tmp_path):
    targets = [
        {"label": "Project Alpha", "agency": "agency.gov"},
        {"label": "Project Beta", "agency": "agency.gov"},
    ]
    candidates = [make_candidate("https://agency.gov/GetDocument.pdf")]
    DiscoveryEngine._attribute_seeds_to_targets(candidates, targets)
    assert not candidates[0].target_metadata


def test_resumed_global_seeds_do_not_reintroduce_completed_target(tmp_path):
    engine = DiscoveryEngine()
    request = _request(
        tmp_path,
        dry_run=True,
        seed_urls=[
            "https://agency.gov/alpha.pdf",
            "https://agency.gov/beta.pdf",
        ],
        targets=[{"label": "alpha"}, {"label": "beta"}],
    )
    manifest = tmp_path / "old-manifest.json"
    index = tmp_path / "old-index.csv"
    manifest.write_text("{}")
    index.write_text("path\n")
    engine._save_checkpoint_entries(
        tmp_path / "domain/checkpoint.json",
        {
            "alpha": {
                "run_id": "old",
                "artifacts": {
                    str(manifest): artifact_sha256(manifest),
                    str(index): artifact_sha256(index),
                },
                "curated_paths": {},
            }
        },
        fingerprint=effective_discovery_fingerprint(request),
    )
    result = engine.run(request)
    candidates = json.loads(result.manifest_path.read_text())["candidates"]
    assert [candidate["url"] for candidate in candidates] == [
        "https://agency.gov/beta.pdf"
    ]
    assert candidates[0]["target_metadata"] == {"label": "beta"}


def test_all_completed_targets_do_not_trigger_unscoped_search(
    monkeypatch, tmp_path
):
    """Validated complete targets cannot trigger an unscoped search."""
    request = _request(tmp_path, dry_run=True, targets=[{"label": "alpha"}])
    engine = DiscoveryEngine()
    manifest = tmp_path / "old-manifest.json"
    index = tmp_path / "old-index.csv"
    manifest.write_text("{}")
    index.write_text("path\n")
    engine._save_checkpoint_entries(
        tmp_path / "domain/checkpoint.json",
        {
            "alpha": {
                "run_id": "old",
                "artifacts": {
                    str(manifest): artifact_sha256(manifest),
                    str(index): artifact_sha256(index),
                },
                "curated_paths": {},
            }
        },
        fingerprint=effective_discovery_fingerprint(request),
    )
    seeker = Mock(side_effect=AssertionError("nothing remains to search"))
    monkeypatch.setattr(engine, "_run_seeker", seeker)
    result = engine.run(request)
    assert seeker.call_count == 0
    assert json.loads(result.manifest_path.read_text())["candidates"] == []


def test_promotion_index_only_tracks_promoted_files_and_prunes_cleared(
    tmp_path,
):
    """Only published evidence can replace consolidated provenance."""
    domain = tmp_path / "domain"
    curated = domain / "curated/partition"
    curated.mkdir(parents=True)
    (curated / "same.pdf").write_bytes(b"old selected document")
    (curated / "stale.pdf").write_bytes(b"stale document")
    domain_index = domain / "download_index.csv"
    domain_index.write_text(
        "relative_path,url,status,review_selected\n"
        "partition/same.pdf,https://agency.gov/old.pdf,downloaded,True\n"
        "partition/stale.pdf,https://agency.gov/stale.pdf,downloaded,True\n"
    )
    run_curated = domain / "runs/probe/curated"
    run_curated.mkdir(parents=True)
    run_index = run_curated.parent / "download_index.csv"
    run_index.write_text(
        "relative_path,url,status,review_selected\n"
        "partition/same.pdf,https://unrelated.gov/rejected.pdf,downloaded,False\n"
    )
    DiscoveryEngine._promote_to_consolidated_curated(
        run_curated_dir=run_curated, domain_dir=domain,
    )
    with domain_index.open() as stream:
        by_path = {row["relative_path"]: row for row in csv.DictReader(stream)}
    assert by_path["partition/same.pdf"]["url"] == "https://agency.gov/old.pdf"
    DiscoveryEngine._promote_to_consolidated_curated(
        run_curated_dir=run_curated, domain_dir=domain,
        attempted_partitions={Path("partition")},
    )
    with domain_index.open() as stream:
        assert list(csv.DictReader(stream)) == []


def test_source_context_ambiguous_basename_never_picks_last_row(tmp_path):
    index = tmp_path / "download_index.csv"
    index.write_text(
        "path,relative_path,url\n"
        "documents/a/report.pdf,a/report.pdf,https://agency.gov/a.pdf\n"
        "documents/b/report.pdf,b/report.pdf,https://agency.gov/b.pdf\n"
    )
    context = build_source_context_map(None, str(index))
    assert "report.pdf" not in context
    assert prepend_source_context("BODY", tmp_path / "elsewhere/report.pdf", context) == "BODY"
    assert "https://agency.gov/a.pdf" in prepend_source_context(
        "BODY", tmp_path / "curated/a/../a/report.pdf", context,
    )


def test_source_context_canonical_collision_is_explicit(tmp_path):
    index = tmp_path / "download_index.csv"
    index.write_text(
        "path,url\n"
        "documents/report.pdf,https://agency.gov/a.pdf\n"
        "documents/../documents/report.pdf,https://agency.gov/b.pdf\n"
    )
    with pytest.raises(ValueError, match="[Cc]onflict|[Aa]mbiguous"):
        build_source_context_map(None, str(index))


@pytest.mark.parametrize("change", ["target", "model_alias", "max_chars", "keywords"])
def test_grade_cache_includes_all_effective_inputs(tmp_path, change):
    path = tmp_path / "comment.pdf"
    path.write_text("synthetic content")
    record = {"status": "downloaded", "path": str(path), "url": "https://agency.gov/comment.pdf",
              "target_metadata": {"label": "alpha"}}
    config = dict(document_description="public comments", model="review", models={"review": "gpt-fixture-a"})
    grade = {"is_primary": True, "relevance": 0.9, "doc_kind": "comment", "reason": "raw grade"}
    first = DocumentReviewer(**config)
    first._ensure_client = lambda: object()
    first._grade = lambda *args, **kwargs: grade
    first.review([dict(record)], [])
    if change == "target":
        record["target_metadata"] = {"label": "beta"}
    elif change == "model_alias":
        config["models"] = {"review": "gpt-fixture-b"}
    elif change == "max_chars":
        config["max_chars"] = 1500
    else:
        config["review_keywords"] = ["deep evidence"]
    second = DocumentReviewer(**config)
    second._ensure_client = lambda: object()
    second._grade = Mock(return_value=grade)
    second.review([record], [])
    assert second._grade.call_count == 1
    assert not record.get("review_cached")


def test_llm_urls_hide_credentials_but_retain_document_identifiers(monkeypatch, tmp_path):
    from psweep.extraction import document_utils

    monkeypatch.setattr(document_utils, "extract_text_from_document", lambda path: "comment " * 100)
    path = tmp_path / "comment.pdf"
    path.write_text("synthetic")
    url = "https://user:password@agency.gov/view?id=42&X-Amz-Signature=secret&token=credential#access_token=fragment"
    client = SimpleNamespace(extract=Mock(return_value={"data": {
        "is_primary": True, "relevance": 0.9, "doc_kind": "comment", "reason": "raw",
    }}))
    reviewer = DocumentReviewer(document_description="comments")
    reviewer._client = client
    record = {"status": "downloaded", "path": str(path), "url": url,
              "target_metadata": {"label": "alpha", "source": url}}
    reviewer.review([record], [])
    prompt = client.extract.call_args.kwargs["user_prompt"]
    assert "id=42" in prompt
    for secret in ("user:password", "secret", "credential", "fragment"):
        assert secret not in prompt
    sidecar = json.loads((tmp_path / ".review/comment.pdf.json").read_text())
    assert sidecar["source_url"] == url
    assert sidecar["llm"]["reason"] == "raw"
    assert sidecar["coverage"]["sampled_chars"] <= sidecar["coverage"]["document_chars"]


def test_full_mocked_discover_curate_preserves_partial_and_distinct_sources(monkeypatch, tmp_path):
    from requests.exceptions import ReadTimeout

    from psweep.discovery.connectors.serpapi_seeker import SerpApiSeeker

    monkeypatch.setenv("SERPAPI_API_KEY", "fixture")
    monkeypatch.setattr(DiscoveryEngine, "_resolve_serpapi_state", staticmethod(
        lambda request: ({"provider": "serpapi", "enabled": True, "available": True}, [], [])
    ))

    def search(params):
        label = params["q"]
        if label == "partial":
            raise ReadTimeout("sustained fixture timeout")
        return {"organic_results": [{"link": f"https://agency.gov/{label}/report.pdf"}]}

    monkeypatch.setattr(SerpApiSeeker, "_build_search_callable", lambda self: search)

    def fetch(url, **kwargs):
        final = "https://outside.example.org/report.pdf" if "/redirect/" in url else url
        return FakeResponse(url=final, chunks=[b"%PDF\n" + url.encode()])

    monkeypatch.setattr("requests.get", fetch)
    monkeypatch.setattr(DocumentReviewer, "_ensure_client", lambda self: object())
    monkeypatch.setattr(DocumentReviewer, "_grade", lambda self, *args, **kwargs: {
        "is_primary": True, "relevance": 0.9, "doc_kind": "synthetic", "reason": "raw grade",
    })
    request = _request(
        tmp_path, enable_serpapi=True, query_templates=["{label}"],
        targets=[{"label": name} for name in ("first", "second", "partial", "redirect")],
        seed_urls=["https://agency.gov/partial/report.pdf"],
        allowed_domains=["agency.gov"], partition_by=["label"], retry_max_attempts=1,
        document_review={"document_description": "synthetic records", "keep_top": 1,
                         "deduplicate_redundant": False},
    )
    prior = tmp_path / "domain/curated/by_label/partial/report.pdf"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"prior complete evidence")
    (tmp_path / "domain/download_index.csv").write_text(
        "relative_path,url\nby_label/partial/report.pdf,https://agency.gov/prior.pdf\n"
    )
    result = DiscoveryEngine().run(request)
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["status"] == "partial_failure"
    assert prior.read_bytes() == b"prior complete evidence"
    assert result.curated_count == 3
    assert not (tmp_path / "domain/curated/by_label/redirect/report.pdf").exists()
    context = build_source_context_map(tmp_path / "domain/curated", None)
    assert "report.pdf" not in context
    for label in ("first", "second"):
        path = tmp_path / f"domain/curated/by_label/{label}/report.pdf"
        assert path.exists()
        assert f"https://agency.gov/{label}/report.pdf" in prepend_source_context("BODY", path, context)
    assert "https://agency.gov/prior.pdf" in prepend_source_context("BODY", prior, context)
    checkpoint = DiscoveryEngine._load_checkpoint(tmp_path / "domain/checkpoint.json")
    assert set(checkpoint) == {"first", "second"}

    # Continue the same mocked run through the shared CLI extraction boundary.
    import pandas as pd
    from _helpers import synthetic_evidence_schema

    from psweep.cli.commands_extract import _extract_one_document
    from psweep.extraction.document_extractor import DocumentExtractor
    from psweep.pipeline import compile_extractions

    schema = synthetic_evidence_schema()
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema))
    extractor = DocumentExtractor(api_key="fixture", model="gpt-fixture")
    seen_origins = []

    def extract(text, schema, **kwargs):
        url = text.split("SOURCE_URL: ", 1)[1].splitlines()[0]
        seen_origins.append(url)
        return {"data": {"source": {"url": url}, "comments": [{
            "type": "statement", "summary": "Synthetic statement",
            "evidence_quote": "Exact fixture statement", "evidence_locator": "Page 1",
        }]}, "cost": 0.0}

    monkeypatch.setattr(extractor.client, "extract", extract)
    monkeypatch.setattr("psweep.cli.commands_extract.extract_text_from_document", lambda *args, **kwargs: "Exact fixture statement")
    output = tmp_path / "extracted"
    output.mkdir()
    for label in ("first", "second", "partial"):
        (output / label).mkdir()
        document = tmp_path / f"domain/curated/by_label/{label}/report.pdf"
        result = _extract_one_document(
            document, extractor=extractor, loaded_schema=schema,
            page_range_map={document: (1, 1)}, section_text_map={}, source_context_map=context,
            file_output_dirs={document: output / label}, output_dir=output,
            category="synthetic", actual_model="gpt-fixture", enable_validation=False,
            runtime_artifact=None, run_id="run://integration", provider="openai", schema_path=schema_path,
        )
        assert result["success"], result
    assert set(seen_origins) == {
        "https://agency.gov/first/report.pdf", "https://agency.gov/second/report.pdf",
        "https://agency.gov/prior.pdf",
    }
    compiled = compile_extractions(
        output, schema_path, output_dir=tmp_path / "compiled", report_format="csv"
    )
    frame = pd.read_csv(compiled.output_files[0])
    assert len(frame) == 3 and frame["url"].nunique() == 3
    assert set(frame["url"]) == set(seen_origins)
    assert not frame["url"].str.contains("outside.example.org").any()
    assert set(frame["Run Id"]) == {"run://integration"}
    assert set(frame["evidence_quote"]) == {"Exact fixture statement"}
    for path in output.rglob("report.json"):
        audit = json.loads(path.read_text())
        assert audit["lineage"]["run_id"] == "run://integration"
        assert any("selected pages 1-1" in warning for warning in audit["quality"]["warnings"])
        assert audit["processing_metrics"]["coverage"]["selected_page_range"] == [1, 1]


def test_browser_render_redirect_cannot_bypass_source_policy(monkeypatch, tmp_path):
    shell = tmp_path / "shell.html"
    shell.write_text("<html><body>shell</body></html>")
    browser = SimpleNamespace(
        fetch_html=Mock(return_value="<html><body>" + "rendered " * 2000 + "</body></html>"),
        current_url="https://outside.example.org/document", close=lambda: None,
    )
    monkeypatch.setattr(DiscoveryEngine, "_open_browser_for_download", lambda self: (browser, "fixture"))
    request = _request(
        tmp_path, allowed_domains=["agency.gov"],
        browser_escalation={"enabled": True},
    )
    records = [{"path": str(shell), "status": "downloaded", "url": "https://agency.gov/document"}]
    DiscoveryEngine()._escalate_js_shells_to_browser(records, [], request)
    assert records[0]["status"] == "skipped_selection_filter"
    assert shell.read_text() == "<html><body>shell</body></html>"
    assert not records[0].get("browser_escalated")


def test_snapshot_write_failure_preserves_prior_checkpoint(monkeypatch, tmp_path):
    path = tmp_path / "checkpoint.json"
    engine = DiscoveryEngine()
    engine._save_checkpoint_entries(path, {"prior": {"run_id": "old"}})
    before = path.read_bytes()
    original_replace = Path.replace

    def replace(source, target):
        if source.name.startswith(".checkpoint-"):
            raise OSError("fixture atomic replace failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(OSError, match="atomic replace"):
        engine._save_checkpoint_entries(path, {"new": {"run_id": "new"}})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".checkpoint-*"))


@pytest.mark.parametrize("failure", ["unavailable", "unreadable", "stale_grade"])
def test_review_failure_does_not_curate_every_download_or_reuse_stale_grade(tmp_path, failure):
    path = tmp_path / "record.pdf"
    path.write_text("fixture")
    record = {"path": str(path), "status": "downloaded", "relative_path": "record.pdf"}
    if failure == "stale_grade":
        record.update(review_relevance=0.9, review_is_primary=True, review_selected=True)
    reviewer = DocumentReviewer(document_description="synthetic")
    if failure == "unavailable":
        reviewer._ensure_client = Mock(side_effect=ValueError("no credentials"))
    else:
        reviewer._ensure_client = lambda: object()
        reviewer._grade = lambda *args, **kwargs: None
    reviewer.review([record], [])
    assert record.get("review_failed") is True
    assert not record.get("review_selected")
    _, count, _ = DiscoveryEngine._materialize_curated(
        documents_dir=tmp_path, download_records=[record],
    )
    assert count == 0


def test_cached_raw_grade_is_independent_of_pdf_dedup_selection(tmp_path):
    records = []
    for name, score in (("one.pdf", 0.9), ("two.pdf", 0.8)):
        path = tmp_path / name
        path.write_text(name)
        records.append({"path": str(path), "status": "downloaded", "target_metadata": {"label": "alpha"}})
    reviewer = DocumentReviewer(document_description="records", keep_top=2)
    reviewer._ensure_client = lambda: object()
    reviewer._grade = lambda path, **kwargs: {
        "is_primary": True, "relevance": 0.9 if path.endswith("one.pdf") else 0.8,
        "doc_kind": "record", "reason": "original raw grade",
    }
    reviewer._find_redundant_docs = lambda records, context: {1}
    reviewer.review(records, [])
    assert sum(record["review_selected"] for record in records) == 1
    sidecar = json.loads((tmp_path / ".review/two.pdf.json").read_text())
    assert sidecar["llm"]["reason"] == "original raw grade"
    assert sidecar["selection"]["redundant"]
    reviewer._deduplicate_redundant = False
    reviewer._grade = Mock(side_effect=AssertionError("must reuse raw grade"))
    reviewer.review(records, [])
    assert all(record["review_selected"] and record["review_cached"] for record in records)
    assert not any(record.get("review_redundant") for record in records)


def test_failed_download_retains_target_and_prior_curation(monkeypatch, tmp_path):
    from requests.exceptions import ConnectionError

    target = {"label": "alpha"}
    engine = DiscoveryEngine()
    request = _request(tmp_path, targets=[target], partition_by=["label"], retry_max_attempts=1)
    candidates = [make_candidate("https://agency.gov/good.pdf"), make_candidate("https://agency.gov/bad.pdf")]
    for candidate in candidates:
        candidate.target_metadata = target
    monkeypatch.setattr(engine, "_run_seeker", lambda *args: (candidates, [], [], [], [], 0))

    def fetch(url, **kwargs):
        if url.endswith("bad.pdf"):
            raise ConnectionError("fixture failure")
        return FakeResponse(url=url, chunks=[b"%PDF good"])

    monkeypatch.setattr("requests.get", fetch)
    prior = tmp_path / "domain/curated/by_label/alpha/prior.pdf"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"prior complete")
    result = engine.run(request)
    records = json.loads(result.manifest_path.read_text())["downloads"]
    failed = next(record for record in records if record["status"] == "failed")
    assert failed["target_metadata"] == target
    assert prior.read_bytes() == b"prior complete"
    assert not engine._load_checkpoint(tmp_path / "domain/checkpoint.json")


def test_review_failure_is_local_to_failed_records(tmp_path):
    good = tmp_path / "documents/good.pdf"
    good.parent.mkdir()
    good.write_bytes(b"selected")
    bad = good.parent / "bad.pdf"
    bad.write_bytes(b"ungraded")
    records = [
        {"path": str(good), "relative_path": "good.pdf", "status": "downloaded", "review_selected": True},
        {"path": str(bad), "relative_path": "bad.pdf", "status": "downloaded", "review_selected": False, "review_failed": True},
    ]
    curated, count, errors = DiscoveryEngine._materialize_curated(documents_dir=good.parent, download_records=records)
    assert count == 1 and errors == 0
    assert (curated / "good.pdf").exists()
    assert not (curated / "bad.pdf").exists()
    DiscoveryEngine._apply_document_retention(mode="curated", documents_dir=good.parent, curated_dir=curated, download_records=records)
    assert good.exists()


def test_root_promotion_does_not_clear_new_descendants(tmp_path):
    domain = tmp_path / "domain"
    run_curated = domain / "runs/probe/curated"
    (run_curated / "partition").mkdir(parents=True)
    (run_curated / "root.pdf").write_bytes(b"root")
    (run_curated / "partition/report.pdf").write_bytes(b"report")
    DiscoveryEngine._promote_to_consolidated_curated(run_curated_dir=run_curated, domain_dir=domain, attempted_partitions={Path("partition")})
    assert (domain / "curated/partition/report.pdf").read_bytes() == b"report"


def test_nested_promotion_keeps_root_provenance(tmp_path):
    domain = tmp_path / "domain"
    run_curated = domain / "runs/probe/curated"
    (run_curated / "county").mkdir(parents=True)
    (run_curated / "county/new.pdf").write_bytes(b"new")
    (domain / "download_index.csv").write_text("relative_path,url\nroot.pdf,https://agency.gov/root.pdf\ncounty/old.pdf,https://agency.gov/old.pdf\n")
    (run_curated.parent / "download_index.csv").write_text("relative_path,url\ncounty/new.pdf,https://agency.gov/new.pdf\n")
    DiscoveryEngine._promote_to_consolidated_curated(run_curated_dir=run_curated, domain_dir=domain)
    with (domain / "download_index.csv").open() as stream:
        rows = {row["relative_path"] for row in csv.DictReader(stream)}
    assert rows == {"root.pdf", "county/new.pdf"}


def test_embedded_redaction_handles_uppercase_schemes_and_commas():
    from psweep.discovery.urls import redact_urls_in_text

    text = "source=HTTPS://user:password@agency.gov/view?id=42&token=secret,remaining"
    redacted = redact_urls_in_text(text)
    assert "id=42" in redacted
    assert "password" not in redacted and "secret" not in redacted and "remaining" not in redacted


def test_retry_groups_use_complete_target_identity():
    records = [
        {"target_metadata": {"label": "shared", "state": "CA"}, "review_selected": True},
        {"target_metadata": {"label": "shared", "state": "CO"}, "review_selected": False},
    ]
    assert len(DiscoveryEngine._group_records_by_target_label(records)) == 2
