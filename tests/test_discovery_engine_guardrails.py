"""Regression tests for discovery engine onboarding guardrails."""

from __future__ import annotations

import json
from pathlib import Path

import requests

from psweep.discovery.engine import (
    CandidateScore,
    DiscoveryCandidate,
    DiscoveryEngine,
    DiscoveryRequest,
)


def _read_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_query_discovery_without_candidates_emits_explicit_error(tmp_path):
    """Query-based runs without seeds should fail loudly when discovery cannot produce candidates."""
    documents_dir = tmp_path / "documents"
    manifest_path = tmp_path / "manifest.json"

    request = DiscoveryRequest(
        domain="guardrail_test",
        seed_urls=[],
        query="xcel residential tariff pdf",
        enable_serpapi=False,
        output_documents=documents_dir,
        output_manifest=manifest_path,
        dry_run=True,
    )

    result = DiscoveryEngine().run(request)
    manifest = _read_manifest(result.manifest_path)

    assert manifest["status"] == "scaffold_dry_run_with_errors"
    assert manifest["error_summary"]["total_errors"] >= 1
    assert any(
        record.get("stage") == "discovery.search"
        for record in manifest.get("errors", [])
    )
    assert any(
        "Discovery produced zero candidates" in note
        for note in manifest.get("notes", [])
    )


def test_forbidden_download_retries_via_browser(tmp_path, monkeypatch):
    documents_dir = tmp_path / "documents"
    manifest_path = tmp_path / "manifest.json"
    request = DiscoveryRequest(
        domain="guardrail_test",
        seed_urls=[],
        query=None,
        enable_serpapi=False,
        output_documents=documents_dir,
        output_manifest=manifest_path,
        dry_run=False,
    )
    candidate = DiscoveryCandidate(
        url="https://example.gov/files/attachment.pdf",
        source="seed",
        score=CandidateScore(url_signal=1.0, trust_signal=1.0),
    )

    def fake_get(*_args, **_kwargs):
        response = requests.Response()
        response.status_code = 403
        response.url = candidate.url
        response._content = b""
        return response

    class DummyBrowser:
        def __init__(self) -> None:
            self.closed = False

        def download_bytes(self, url: str):
            assert url == candidate.url
            return b"%PDF-1.4\nfallback", "application/pdf"

        def close(self) -> None:
            self.closed = True

    dummy_browser = DummyBrowser()
    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr(
        DiscoveryEngine,
        "_open_browser_for_download",
        lambda self: (dummy_browser, "browser fallback"),
    )
    monkeypatch.setattr(
        DiscoveryEngine,
        "_evaluate_request_policy",
        lambda self, **_kwargs: type(
            "PolicyResult",
            (),
            {
                "allowed": True,
                "blocking_code": None,
                "messages": [],
                "warning_codes": [],
            },
        )(),
    )
    monkeypatch.setattr(
        DiscoveryEngine,
        "_passes_final_url_selection_guard",
        lambda self, **_kwargs: (True, None),
    )

    idx, download_record, maybe_error, was_downloaded, warnings = (
        DiscoveryEngine()._download_single_candidate(
            idx=0,
            candidate=candidate,
            request=request,
            documents_dir=documents_dir,
            ssl_verify=True,
            retry_policy={
                "max_attempts": 1,
                "initial_backoff_seconds": 0.0,
                "max_backoff_seconds": 0.0,
            },
            rate_limiter=None,
        )
    )

    assert idx == 0
    assert maybe_error is None
    assert was_downloaded is True
    assert warnings == []
    assert download_record["download_method"] == "browser_fallback"
    assert dummy_browser.closed is True
    assert download_record["status"] == "downloaded"
    assert (documents_dir / download_record["relative_path"]).exists()


def test_promote_curated_keeps_existing_when_run_curated_empty(tmp_path):
    domain_dir = tmp_path / "discovered" / "guardrail_test"
    consolidated_partition = domain_dir / "curated" / "host" / "path"
    consolidated_partition.mkdir(parents=True, exist_ok=True)
    prior_file = consolidated_partition / "existing.pdf"
    prior_file.write_text("existing", encoding="utf-8")

    run_curated_dir = (
        domain_dir / "runs" / "run-1" / "curated"
    )
    run_curated_dir.mkdir(parents=True, exist_ok=True)

    DiscoveryEngine._promote_to_consolidated_curated(
        run_curated_dir=run_curated_dir,
        domain_dir=domain_dir,
    )

    assert prior_file.exists()
    assert prior_file.read_text(encoding="utf-8") == "existing"
    assert not list(domain_dir.glob(".curated-*"))


def test_promote_curated_swaps_attempted_and_clears_stale_on_fresh(tmp_path):
    domain_dir = tmp_path / "discovered" / "guardrail_test"
    current_partition = domain_dir / "curated" / "host" / "path"
    current_partition.mkdir(parents=True, exist_ok=True)
    (current_partition / "old.pdf").write_text("old", encoding="utf-8")

    stale_partition = domain_dir / "curated" / "other" / "stale"
    stale_partition.mkdir(parents=True, exist_ok=True)
    (stale_partition / "stale.pdf").write_text("stale", encoding="utf-8")

    run_curated_dir = (
        domain_dir / "runs" / "run-2" / "curated"
    )
    new_partition = run_curated_dir / "host" / "path"
    new_partition.mkdir(parents=True, exist_ok=True)
    (new_partition / "new.pdf").write_text("new", encoding="utf-8")

    DiscoveryEngine._promote_to_consolidated_curated(
        run_curated_dir=run_curated_dir,
        domain_dir=domain_dir,
        attempted_partitions={Path("host/path"), Path("other/stale")},
    )

    assert (domain_dir / "curated" / "host" / "path" / "new.pdf").exists()
    assert not (domain_dir / "curated" / "host" / "path" / "old.pdf").exists()
    assert not (domain_dir / "curated" / "other" / "stale").exists()
    assert not list(domain_dir.glob(".curated-*"))


def test_code_hosting_retry_respects_fresh_cache_refresh(monkeypatch, tmp_path):
    request = DiscoveryRequest(
        domain="guardrail_test",
        seed_urls=[],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "documents",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
        link_prioritization_domain_scores={"municode.com": 0.25},
        seeker_cache=True,
        seeker_cache_ttl_minutes=30.0,
        reprocess=True,
        retry_max_attempts=4,
        retry_initial_backoff_seconds=2.0,
        retry_max_backoff_seconds=10.0,
        min_request_interval_ms=250,
    )
    download_records = [
        {
            "url": "https://library.municode.com/tx/city/example/code",
            "target_metadata": {"label": "example-target"},
            "review_selected": False,
        }
    ]
    captured_kwargs: dict[str, object] = {}

    class DummySeeker:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

        def discover(self, _seeker_input):
            return []

    monkeypatch.setattr(
        "psweep.discovery.connectors.serpapi_seeker.SerpApiSeeker",
        DummySeeker,
    )

    DiscoveryEngine()._retry_failed_targets_on_code_hosting(
        request=request,
        download_records=download_records,
        notes=[],
        documents_dir=tmp_path / "documents",
        review_cfg={},
        classifier_keywords=["pipeline"],
    )

    assert captured_kwargs["cache_dir"] == "discovered/guardrail_test/.serpapi_cache"
    assert captured_kwargs["cache_ttl_seconds"] == 1800.0
    assert captured_kwargs["cache_refresh"] is True
    assert captured_kwargs["retry_max_attempts"] == 4
    assert captured_kwargs["retry_initial_backoff_seconds"] == 2.0
    assert captured_kwargs["retry_max_backoff_seconds"] == 10.0
    assert captured_kwargs["min_request_interval_seconds"] == 0.25


def test_promote_curated_clears_attempted_partition_when_run_curated_empty(tmp_path):
    domain_dir = tmp_path / "discovered" / "guardrail_test"
    stale_partition = domain_dir / "curated" / "host" / "path"
    stale_partition.mkdir(parents=True, exist_ok=True)
    stale_file = stale_partition / "stale.pdf"
    stale_file.write_text("stale", encoding="utf-8")

    run_curated_dir = domain_dir / "runs" / "run-3" / "curated"
    run_curated_dir.mkdir(parents=True, exist_ok=True)

    DiscoveryEngine._promote_to_consolidated_curated(
        run_curated_dir=run_curated_dir,
        domain_dir=domain_dir,
        attempted_partitions={Path("host/path")},
    )

    assert not stale_file.exists()


def test_download_single_candidate_decodes_escaped_url_before_fetch(
    tmp_path, monkeypatch
):
    documents_dir = tmp_path / "documents"
    manifest_path = tmp_path / "manifest.json"
    request = DiscoveryRequest(
        domain="guardrail_test",
        seed_urls=[],
        query=None,
        enable_serpapi=False,
        output_documents=documents_dir,
        output_manifest=manifest_path,
        dry_run=False,
    )
    candidate = DiscoveryCandidate(
        url="https://example.org/doc.pdf?article\\u003d1034\\u0026context\\u003dtest",
        source="seed",
        score=CandidateScore(url_signal=1.0, trust_signal=1.0),
    )

    captured_url: dict[str, str] = {}

    class _Response:
        def __init__(self, url: str):
            self.url = url
            self.headers = {"Content-Type": "application/pdf"}

        def iter_content(self, chunk_size=65536):
            del chunk_size
            yield b"%PDF-1.4\nmock"

    def fake_download_with_retry(self, *, url, **kwargs):
        del kwargs
        captured_url["value"] = url
        return _Response(url), 1

    monkeypatch.setattr(
        DiscoveryEngine,
        "_download_with_retry",
        fake_download_with_retry,
    )
    monkeypatch.setattr(
        DiscoveryEngine,
        "_evaluate_request_policy",
        lambda self, **_kwargs: type(
            "PolicyResult",
            (),
            {
                "allowed": True,
                "blocking_code": None,
                "messages": [],
                "warning_codes": [],
            },
        )(),
    )

    idx, record, maybe_error, was_downloaded, warnings = (
        DiscoveryEngine()._download_single_candidate(
            idx=0,
            candidate=candidate,
            request=request,
            documents_dir=documents_dir,
            ssl_verify=True,
            retry_policy={
                "max_attempts": 1,
                "initial_backoff_seconds": 0.0,
                "max_backoff_seconds": 0.0,
            },
            rate_limiter=None,
        )
    )

    assert idx == 0
    assert maybe_error is None
    assert warnings == []
    assert was_downloaded is True
    assert captured_url["value"] == "https://example.org/doc.pdf?article=1034&context=test"
    assert record["status"] == "downloaded"
    assert record["url"] == "https://example.org/doc.pdf?article=1034&context=test"
