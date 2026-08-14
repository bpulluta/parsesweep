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
