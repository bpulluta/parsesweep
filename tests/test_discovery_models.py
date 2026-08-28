from pathlib import Path
import re
import json
import csv

from psweep.discovery import (
    DiscoveryCandidate,
    DiscoveryEngine,
    DiscoveryManifest,
    DiscoveryRequest,
    CandidateScore,
)

from discovery_helpers import FakeResponse as _FakeResponse
from discovery_helpers import patch_requests_get as _patch_requests_get


def test_candidate_score_weighted_total_and_classification():
    score = CandidateScore(
        url_signal=0.9,
        anchor_signal=0.6,
        content_signal=0.3,
        trust_signal=0.8,
    )

    total = score.weighted_total()
    assert 0.0 <= total <= 1.0
    assert score.acceptance_class() in {"accepted", "needs_review", "rejected"}


def test_manifest_serialization_includes_candidate_score_payload():
    candidate = DiscoveryCandidate(
        url="https://example.org/doc.pdf",
        source="seed_url",
        score=CandidateScore(url_signal=1.0, trust_signal=1.0),
        reasons=["Matched seed URL pattern"],
    )
    manifest = DiscoveryManifest(
        run_id="acq-test",
        status="scaffold",
        started_at="2026-03-26T00:00:00+00:00",
        input={"domain": "geothermal_ordinances"},
        candidates=[candidate],
    )

    payload = manifest.to_dict()

    assert payload["manifest_version"] == "1.0.0"
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["score"]["total"] >= 0.0
    assert payload["candidates"][0]["score"]["acceptance_class"] in {
        "accepted",
        "needs_review",
        "rejected",
    }


def test_engine_run_emits_scaffold_candidates(tmp_path: Path):
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/seed-a", "https://example.org/seed-b"],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
    )

    result = engine.run(request)
    payload = result.manifest_path.read_text(encoding="utf-8")

    assert result.manifest_path.exists()
    assert result.documents_dir.exists()
    assert '"status": "scaffold_dry_run"' in payload
    assert '"acceptance_class"' in payload
    assert payload.count('"source": "seed_url"') == 2


def test_engine_run_uses_deterministic_default_paths_and_lineage():
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/seed-a"],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=None,
        output_manifest=None,
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert re.match(r"^acq-geothermal-ordinances-\d{8}T\d{6}Z-[0-9a-f]{8}$", result.run_id)
    # Each run is one self-contained folder: documents/ lives under the run dir.
    assert result.documents_dir.as_posix().endswith(
        f"/runs/{result.run_id}/documents"
    )
    assert (
        result.manifest_path.parent.as_posix()
        == result.documents_dir.parent.as_posix()
    )
    assert payload["lineage"]["run_id"] == result.run_id
    assert payload["lineage"]["documents_dir"] == result.documents_dir.as_posix()
    assert payload["lineage"]["manifest_path"] == result.manifest_path.as_posix()


def test_engine_run_emits_taxonomy_aligned_structured_errors_for_invalid_seeds(tmp_path: Path):
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["not-a-url", "https://example.org/valid", "ftp://bad"],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["status"] == "scaffold_dry_run_with_errors"
    assert payload["input"]["seed_urls"] == ["https://example.org/valid"]
    assert len(payload["candidates"]) == 1

    summary = payload["error_summary"]
    assert summary["total_errors"] == 2
    assert summary["by_category"]["input_validation"] == 2
    assert summary["by_code"]["invalid_input"] == 2

    records = payload["errors"]
    assert len(records) == 2
    assert all(record["stage"] == "discovery.seed_validation" for record in records)
    assert all(record["category"] == "input_validation" for record in records)
    assert all(record["code"] == "invalid_input" for record in records)


def test_engine_run_emits_structured_error_when_serpapi_dependency_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda _: None)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/valid"],
        query="geothermal ordinance",
        enable_serpapi=True,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["lineage"]["seeker"]["provider"] == "serpapi"
    assert payload["lineage"]["seeker"]["enabled"] is True
    assert payload["lineage"]["seeker"]["available"] is False
    assert payload["error_summary"]["total_errors"] == 1
    assert payload["errors"][0]["stage"] == "discovery.seeker_init"


def test_engine_run_emits_structured_error_when_serpapi_key_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda _: object())
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_KEY", raising=False)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/valid"],
        query="geothermal ordinance",
        enable_serpapi=True,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["lineage"]["seeker"]["available"] is True
    assert payload["error_summary"]["total_errors"] == 1
    assert payload["errors"][0]["stage"] == "discovery.seeker_init"
    assert payload["errors"][0]["category"] == "input_validation"
    assert payload["errors"][0]["code"] == "invalid_input"


def test_engine_run_downloads_supported_candidate_for_non_dry_run(tmp_path: Path, monkeypatch):
    _patch_requests_get(
        monkeypatch,
        url="https://example.org/docs/test-ordinance.pdf",
        chunks=[b"%PDF-1.4\n", b"mock-pdf-content"],
    )
    monkeypatch.setattr(
        DiscoveryEngine,
        "_escalate_js_shells_to_browser",
        lambda self, downloads, notes, request: (downloads, notes),
    )
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/docs/test-ordinance.pdf"],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["error_summary"]["total_errors"] == 0
    assert len(payload["downloads"]) == 1
    assert payload["downloads"][0]["status"] == "downloaded"
    assert payload["downloads"][0]["partition_mode"] == "host"
    assert payload["downloads"][0]["source_host"] == "example.org"
    assert payload["downloads"][0]["relative_path"].startswith("by_host/example.org/")
    downloaded_path = Path(payload["downloads"][0]["path"])
    assert downloaded_path.exists()
    assert downloaded_path.suffix.lower() == ".pdf"
    assert downloaded_path.read_bytes().startswith(b"%PDF-1.4")


def test_engine_run_skips_unsupported_content_type_for_non_dry_run(tmp_path: Path, monkeypatch):
    _patch_requests_get(
        monkeypatch,
        url="https://example.org/landing-page",
        content_type="text/html; charset=utf-8",
        chunks=[b"<html>not-a-document</html>"],
    )
    # This test only asserts that unsupported HTML is still recorded as a
    # download. Browser escalation of the tiny shell page is out of scope and
    # would otherwise launch a real browser and sleep for ~10s.
    monkeypatch.setattr(
        DiscoveryEngine,
        "_escalate_js_shells_to_browser",
        lambda self, downloads, notes, request: (downloads, notes),
    )

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="tariffs",
        seed_urls=["https://example.org/landing-page"],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
        min_request_interval_ms=0,
        robots_policy_mode="ignore",
        tos_policy_mode="ignore",
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["error_summary"]["total_errors"] == 0
    assert len(payload["downloads"]) == 1
    assert payload["downloads"][0]["status"] == "downloaded"
    assert payload["downloads"][0]["path"].endswith(".html")


def test_engine_run_download_stage_skips_rejected_discovered_candidates(tmp_path: Path, monkeypatch):
    def fake_resolve_serpapi_state(_request: DiscoveryRequest):
        return ({"provider": "serpapi", "enabled": True, "available": True}, [], [])

    def fake_run_seeker(_request, _state, notes, errors):
        return (
            [
                DiscoveryCandidate(
                    url="https://example.org/rejected.pdf",
                    source="serpapi_google",
                    score=CandidateScore(url_signal=0.1, anchor_signal=0.0, content_signal=0.0, trust_signal=0.1),
                ),
                DiscoveryCandidate(
                    url="https://example.org/review.pdf",
                    source="serpapi_google",
                    score=CandidateScore(url_signal=1.0, anchor_signal=1.0, content_signal=0.0, trust_signal=1.0),
                ),
            ],
            notes,
            errors,
            [
                {"url": "https://example.org/rejected.pdf", "confidence": 0.1},
                {"url": "https://example.org/review.pdf", "confidence": 0.6},
            ],
            [],
            0,
        )

    monkeypatch.setattr("requests.get", lambda url, **kwargs: _FakeResponse(url=url))
    monkeypatch.setattr(DiscoveryEngine, "_resolve_serpapi_state", staticmethod(fake_resolve_serpapi_state))
    monkeypatch.setattr(DiscoveryEngine, "_run_seeker", staticmethod(fake_run_seeker))

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=[],
        query="geothermal ordinance",
        enable_serpapi=True,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["candidate_summary"]["total"] == 2
    assert len(payload["downloads"]) == 1
    assert payload["downloads"][0]["url"] == "https://example.org/review.pdf"
    assert any("Download simplification skipped 1 rejected discovered candidate" in note for note in payload["notes"])


def test_engine_run_download_stage_preserves_rejected_when_no_better_discovered_candidates(tmp_path: Path, monkeypatch):
    def fake_resolve_serpapi_state(_request: DiscoveryRequest):
        return ({"provider": "serpapi", "enabled": True, "available": True}, [], [])

    def fake_run_seeker(_request, _state, notes, errors):
        return (
            [
                DiscoveryCandidate(
                    url="https://example.org/ordinance-a.pdf",
                    source="serpapi_google",
                    score=CandidateScore(url_signal=0.1, anchor_signal=0.0, content_signal=0.0, trust_signal=0.1),
                ),
                DiscoveryCandidate(
                    url="https://example.org/ordinance-b.pdf",
                    source="serpapi_google",
                    score=CandidateScore(url_signal=0.1, anchor_signal=0.0, content_signal=0.0, trust_signal=0.1),
                ),
            ],
            notes,
            errors,
            [
                {"url": "https://example.org/ordinance-a.pdf", "confidence": 0.1},
                {"url": "https://example.org/ordinance-b.pdf", "confidence": 0.1},
            ],
            [],
            0,
        )

    monkeypatch.setattr("requests.get", lambda url, **kwargs: _FakeResponse(url=url))
    monkeypatch.setattr(DiscoveryEngine, "_resolve_serpapi_state", staticmethod(fake_resolve_serpapi_state))
    monkeypatch.setattr(DiscoveryEngine, "_run_seeker", staticmethod(fake_run_seeker))

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=[],
        query="geothermal ordinance",
        enable_serpapi=True,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert len(payload["downloads"]) == 2
    assert any("All discovered candidates scored as rejected" in note for note in payload["notes"])


def test_engine_run_retries_transient_download_failure_then_succeeds(tmp_path: Path, monkeypatch):
    attempts = {"count": 0}

    def flaky_get(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary timeout")
        return _FakeResponse(url="https://example.org/docs/retry-ordinance.pdf")

    monkeypatch.setattr("requests.get", flaky_get)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/docs/retry-ordinance.pdf"],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
        # This test isolates download-retry behavior; keep policy checks off so
        # the mocked requests.get counts only download attempts, not the
        # robots.txt fetch that warn/enforce modes would perform.
        robots_policy_mode="ignore",
        tos_policy_mode="ignore",
        retry_max_attempts=3,
        retry_initial_backoff_seconds=0,
        retry_max_backoff_seconds=0,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert attempts["count"] == 2
    assert payload["downloads"][0]["status"] == "downloaded"
    assert payload["downloads"][0]["attempt_count"] == 2
    assert not any(record.get("status") == "failed" for record in payload["downloads"])


def test_engine_run_includes_rate_limit_and_concurrency_constraints(tmp_path: Path, monkeypatch):
    _patch_requests_get(
        monkeypatch,
        url="https://example.org/docs/test-ordinance.pdf",
        chunks=[b"%PDF-1.4\n", b"controls"],
    )

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/docs/test-ordinance.pdf"],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
        max_concurrent_downloads=4,
        min_request_interval_ms=125,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["constraints"]["max_concurrent_downloads"] == 4
    assert payload["constraints"]["min_request_interval_ms"] == 125
    assert any("Download throughput controls:" in note for note in payload["notes"])


def test_engine_run_enforces_tos_acknowledgement_for_downloads(tmp_path: Path, monkeypatch):
    request_urls: list[str] = []

    def fake_get(url: str, *args, **kwargs):
        request_urls.append(url)
        raise AssertionError("download request should not be attempted when ToS policy blocks it")

    monkeypatch.setattr("requests.get", fake_get)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/docs/policy-test.pdf"],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
        tos_policy_mode="enforce",
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["downloads"][0]["status"] == "skipped_tos_unacknowledged"
    assert payload["downloads"][0]["policy_blocking_code"] == "tos_unacknowledged"
    assert payload["constraints"]["policy"]["tos_policy_mode"] == "enforce"
    assert request_urls == []
    assert any("missing ToS acknowledgement" in note for note in payload["notes"])


def test_engine_run_enforces_robots_policy_for_downloads(tmp_path: Path, monkeypatch):
    request_urls: list[str] = []

    def fake_get(url: str, *args, **kwargs):
        request_urls.append(url)
        text = "User-agent: *\nDisallow: /docs/" if url.endswith("/robots.txt") else ""
        return _FakeResponse(
            url=url,
            content_type="text/plain",
            chunks=[b"blocked"],
            text=text,
        )

    monkeypatch.setattr("requests.get", fake_get)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/docs/blocked.pdf"],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
        robots_policy_mode="enforce",
        tos_policy_mode="ignore",
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["downloads"][0]["status"] == "skipped_robots_disallowed"
    assert payload["downloads"][0]["policy_blocking_code"] == "robots_disallowed"
    assert payload["constraints"]["policy"]["robots_policy_mode"] == "enforce"
    assert request_urls == ["https://example.org/robots.txt"]
    assert any("robots.txt disallow rules" in note for note in payload["notes"])


def test_engine_run_applies_request_rate_limiter_for_downloads(tmp_path: Path, monkeypatch):
    sleep_calls: list[float] = []
    monotonic_values = iter([0.0, 0.0, 0.03, 0.03, 0.05, 0.05, 0.10, 0.10])

    monkeypatch.setattr("requests.get", lambda url, **kwargs: _FakeResponse(url=url))
    monkeypatch.setattr("time.sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr("time.monotonic", lambda: next(monotonic_values, 0.10))

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=[
            "https://example.org/docs/a.pdf",
            "https://example.org/docs/b.pdf",
        ],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
        max_concurrent_downloads=1,
        min_request_interval_ms=80,
    )

    engine.run(request)

    assert sleep_calls
    assert all(call > 0 for call in sleep_calls)


def test_engine_run_uses_configured_request_headers_for_downloads(tmp_path: Path, monkeypatch):
    captured_headers: list[dict[str, str]] = []

    def fake_get(url: str, **kwargs):
        captured_headers.append(dict(kwargs.get("headers") or {}))
        return _FakeResponse(
            url=url,
            content_type="text/html; charset=utf-8",
            chunks=[b"<html><body><h1>Apple filing summary</h1></body></html>"],
        )

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="apple_sec_filings",
        seed_urls=["https://example.org/docs/test-filing.html"],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
        min_request_interval_ms=0,
        robots_policy_mode="ignore",
        tos_policy_mode="ignore",
        request_headers={
            "User-Agent": "ParseSweep/2.0 (Apple SEC validation; contact: example@example.com)",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["downloads"][0]["status"] == "downloaded"
    assert payload["downloads"][0]["path"].endswith(".html")
    assert captured_headers
    assert captured_headers[0]["User-Agent"].startswith("ParseSweep/2.0 (Apple SEC validation")
    assert captured_headers[0]["Accept-Language"] == "en-US,en;q=0.9"


def test_engine_run_routes_centralized_topology_via_http_digger_live_hub_page(tmp_path: Path, monkeypatch):
    _patch_requests_get(
        monkeypatch,
        url="https://county.gov/index.html",
        content_type="text/html; charset=utf-8",
        text=(
            '<html><body>'
            '<a href="/docs/geothermal-ordinance.pdf">Geothermal Ordinance</a>'
            '<a href="/docs/notice.html">Notice</a>'
            '</body></html>'
        ),
    )

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://county.gov/fallback.pdf"],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        topology_mode="centralized",
        digger_provider="http",
        hub_pages=["https://county.gov/index.html"],
        allowed_domains=["county.gov"],
        include_url_patterns=[r"\.pdf$"],
        include_link_text_patterns=[r"ordinance"],
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["lineage"]["routing"]["connector"] == "http"
    assert payload["lineage"]["routing"]["artifact_count"] >= 1
    assert payload["candidates"][0]["url"] == "https://county.gov/docs/geothermal-ordinance.pdf"
    assert payload["lineage"]["routing"]["discovery_modes"] == ["centralized_index_sweep"]


def test_engine_run_downloads_partitioned_by_explicit_jurisdiction(tmp_path: Path, monkeypatch):
    _patch_requests_get(
        monkeypatch,
        url="https://example.org/docs/geothermal.pdf",
        chunks=[b"%PDF-1.4\n", b"jurisdiction-layout"],
    )

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/docs/geothermal.pdf"],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
        state="California",
        jurisdiction="Imperial County",
        partition_mode="jurisdiction",
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["error_summary"]["total_errors"] == 0
    record = payload["downloads"][0]
    assert record["partition_mode"] == "jurisdiction"
    assert record["source_state"] == "ca"
    assert record["source_jurisdiction"] == "imperial-county"
    assert record["relative_path"].startswith("by_jurisdiction/ca/imperial-county/")
    index_path = Path(payload["lineage"]["download_index_csv"])
    assert index_path.exists()
    with index_path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["run_id"] == result.run_id
    assert rows[0]["domain"] == "geothermal_ordinances"
    assert rows[0]["partition_mode"] == "jurisdiction"
    assert rows[0]["source_state"] == "ca"
    assert rows[0]["source_jurisdiction"] == "imperial-county"


def test_engine_run_auto_partition_infers_jurisdiction_from_query(tmp_path: Path, monkeypatch):
    _patch_requests_get(
        monkeypatch,
        url="https://www.chaffeecounty.org/documents/53007.pdf",
        chunks=[b"%PDF-1.4\n", b"query-inference"],
    )

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://www.chaffeecounty.org/documents/53007.pdf"],
        query="Chaffee County Colorado geothermal ordinance pdf",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
        partition_mode="auto",
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["error_summary"]["total_errors"] == 0
    record = payload["downloads"][0]
    assert record["partition_mode"] == "jurisdiction"
    assert record["source_state"] == "co"
    assert record["source_jurisdiction"] == "chaffee-county"
    assert record["relative_path"].startswith("by_jurisdiction/co/chaffee-county/")


def test_engine_run_dry_run_has_no_download_index(tmp_path: Path):
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/seed-a"],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert result.download_index_path is None
    assert payload["lineage"]["download_index_csv"] is None


def test_engine_run_routes_distributed_topology_through_digger_filters(tmp_path: Path):
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=[
            "https://county.gov/ordinance.pdf",
            "https://external.org/ordinance.pdf",
        ],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        topology_mode="distributed",
        allowed_domains=["county.gov"],
        include_url_patterns=[r"\.pdf$"],
        max_depth=4,
        max_pages=25,
        max_files=8,
        timeout_seconds=12,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["constraints"]["topology_mode"] == "distributed"
    assert payload["constraints"]["allowed_domains"] == ["county.gov"]
    assert payload["constraints"]["max_depth"] == 4
    # PDF seeds bypass routing (go directly to download) — routing only
    # activates for crawlable HTML pages from code-hosting domains.
    # external.org is excluded by allowed_domains.
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["url"] == "https://county.gov/ordinance.pdf"
    assert payload["lineage"]["routing"]["mode"] == "distributed"
    # Routing was not applied because all candidates are PDFs
    assert payload["lineage"]["routing"]["applied"] is False
    assert any(
        "no crawlable HTML pages" in note
        for note in payload["notes"]
    )


def test_engine_run_routes_centralized_topology_through_hub_sweep(tmp_path: Path):
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://seed.example.org/fallback"],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        topology_mode="centralized",
        hub_pages=["https://docs.county.gov/index.html"],
        include_url_patterns=[r"\.pdf$"],
        include_link_text_patterns=[r"geothermal|ordinance"],
        index_links=[
            {
                "url": "https://docs.county.gov/geothermal-ordinance.pdf",
                "text": "Geothermal Ordinance",
            },
            {
                "url": "https://docs.county.gov/general-notice.html",
                "text": "General Notice",
            },
        ],
        max_depth=3,
        max_pages=20,
        max_files=10,
        timeout_seconds=15,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["constraints"]["topology_mode"] == "centralized"
    assert payload["constraints"]["hub_pages"] == ["https://docs.county.gov/index.html"]
    assert payload["constraints"]["index_link_count"] == 2
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["url"] == "https://docs.county.gov/geothermal-ordinance.pdf"
    assert "Routed via centralized hub sweep" in payload["candidates"][0]["reasons"]
    assert payload["lineage"]["routing"]["mode"] == "centralized"
    assert payload["lineage"]["routing"]["applied"] is True
    assert payload["lineage"]["routing"]["artifact_count"] == 1
    assert payload["lineage"]["routing"]["hub_page_count"] == 1
    assert payload["lineage"]["routing"]["index_link_count"] == 2
    assert payload["lineage"]["routing"]["discovery_modes"] == ["centralized_index_sweep"]
    centralized_metrics = payload["stage_summaries"]["acceptance_metrics"]["centralized_page_sweep"]
    assert centralized_metrics["applicable"] is True
    assert centralized_metrics["measurement_mode"] == "seeded_index_links"
    assert centralized_metrics["qualifying_link_count"] == 1
    assert centralized_metrics["recovered_link_count"] == 1
    assert centralized_metrics["recovery_rate"] == 1.0
    assert centralized_metrics["meets_coverage_threshold"] is True
    assert any(
        note == "Centralized routing staged 1 candidate(s) through hub sweep."
        for note in payload["notes"]
    )


def test_engine_run_centralized_routing_falls_back_when_no_sweep_matches(tmp_path: Path):
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://seed.example.org/fallback"],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        topology_mode="centralized",
        hub_pages=["https://docs.county.gov/index.html"],
        include_url_patterns=[r"\.pdf$"],
        include_link_text_patterns=[r"geothermal|ordinance"],
        index_links=[
            {
                "url": "https://docs.county.gov/general-notice.html",
                "text": "General Notice",
            }
        ],
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["url"] == "https://seed.example.org/fallback"
    assert payload["lineage"]["routing"]["mode"] == "centralized"
    assert payload["lineage"]["routing"]["applied"] is True
    assert payload["lineage"]["routing"]["artifact_count"] == 0
    assert any(
        note == "Centralized routing produced no sweep artifacts; falling back to unrouted candidates."
        for note in payload["notes"]
    )


def test_engine_run_routes_hybrid_topology_with_centralized_then_distributed(tmp_path: Path):
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=[
            "https://county.gov/distributed-ordinance.pdf",
            "https://external.org/outside.pdf",
        ],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        topology_mode="hybrid",
        hub_pages=["https://docs.county.gov/index.html"],
        allowed_domains=["county.gov", "docs.county.gov"],
        include_url_patterns=[r"\.pdf$"],
        include_link_text_patterns=[r"geothermal|ordinance"],
        index_links=[
            {
                "url": "https://docs.county.gov/hub-geothermal-ordinance.pdf",
                "text": "Geothermal Ordinance",
            },
            {
                "url": "https://docs.county.gov/general-notice.html",
                "text": "General Notice",
            },
        ],
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["constraints"]["topology_mode"] == "hybrid"
    assert len(payload["candidates"]) == 2
    urls = [candidate["url"] for candidate in payload["candidates"]]
    # Centralized finds the hub-linked PDF; distributed preserves the
    # original PDF seed from an allowed domain (external.org is excluded
    # by allowed_domains).
    assert "https://docs.county.gov/hub-geothermal-ordinance.pdf" in urls
    assert "https://county.gov/distributed-ordinance.pdf" in urls
    assert "https://external.org/outside.pdf" not in urls
    centralized_candidate = next(
        c for c in payload["candidates"]
        if c["url"] == "https://docs.county.gov/hub-geothermal-ordinance.pdf"
    )
    assert "Routed via centralized hub sweep" in centralized_candidate["reasons"]
    assert payload["lineage"]["routing"]["mode"] == "hybrid"
    assert payload["lineage"]["routing"]["centralized_candidate_count"] == 1
    assert payload["lineage"]["routing"]["distributed_candidate_count"] == 1
    assert payload["lineage"]["routing"]["final_candidate_count"] == 2
    assert len(payload["lineage"]["routing"]["stages"]) == 2
    hybrid_metrics = payload["stage_summaries"]["acceptance_metrics"]["hybrid_mixed_source_resolution"]
    assert hybrid_metrics["applicable"] is True
    assert hybrid_metrics["centralized_candidate_count"] == 1
    # Distributed preserved the PDF fallback but didn't produce new crawled
    # artifacts — the metric correctly reports only centralized resolved.
    assert hybrid_metrics["distributed_candidate_count"] == 0
    assert hybrid_metrics["both_paths_resolved"] is False
    assert any(
        note == "Hybrid routing produced 2 candidate(s) after centralized-plus-distributed sequencing."
        for note in payload["notes"]
    )


def test_engine_run_hybrid_routing_falls_back_to_distributed_when_hub_empty(tmp_path: Path):
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=[
            "https://county.gov/distributed-ordinance.pdf",
            "https://external.org/outside.pdf",
        ],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        topology_mode="hybrid",
        hub_pages=["https://docs.county.gov/index.html"],
        allowed_domains=["county.gov", "docs.county.gov"],
        include_url_patterns=[r"\.pdf$"],
        include_link_text_patterns=[r"geothermal|ordinance"],
        index_links=[
            {
                "url": "https://docs.county.gov/general-notice.html",
                "text": "General Notice",
            }
        ],
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["url"] == "https://county.gov/distributed-ordinance.pdf"
    assert payload["lineage"]["routing"]["mode"] == "hybrid"
    assert payload["lineage"]["routing"]["centralized_candidate_count"] == 0
    assert payload["lineage"]["routing"]["distributed_candidate_count"] == 1
    assert payload["lineage"]["routing"]["final_candidate_count"] == 1
    assert any(
        note == "Centralized routing produced no sweep artifacts; falling back to unrouted candidates."
        for note in payload["notes"]
    )
    assert any(
        note == "Distributed routing skipped: no crawlable HTML pages from allowed domains."
        for note in payload["notes"]
    )


def test_engine_builds_seeker_inputs_from_targets_and_query_family():
    request = DiscoveryRequest(
        domain="generator_manuals",
        seed_urls=[],
        query=None,
        enable_serpapi=True,
        output_documents=None,
        output_manifest=None,
        dry_run=True,
        targets=[
            {
                "manufacturer": "Generac",
                "power_class_kw": "200-300",
            },
            {
                "manufacturer": "John Deere",
                "power_class_kw": "200-300",
            },
        ],
        query_families={
            "generator_similar_power": [
                "{manufacturer} {power_class_kw} kW generator spec pdf",
            ]
        },
        use_query_family="generator_similar_power",
        seeker_max_results=8,
    )

    seeker_inputs = DiscoveryEngine._build_seeker_inputs(request)

    assert len(seeker_inputs) == 2
    assert all(item.query == "" for item in seeker_inputs)
    assert all(item.max_results == 8 for item in seeker_inputs)
    assert seeker_inputs[0].extra_params["template_context"]["manufacturer"] == "Generac"
    assert seeker_inputs[1].extra_params["template_context"]["manufacturer"] == "John Deere"
    assert seeker_inputs[0].extra_params["use_query_family"] == "generator_similar_power"


def test_engine_run_records_targeted_query_constraints(tmp_path: Path):
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="generator_manuals",
        seed_urls=[],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        targets=[{"manufacturer": "Generac", "power_class_kw": "200-300"}],
        query_templates=["{manufacturer} {power_class_kw} generator pdf"],
        query_families={"generator_similar_power": ["{manufacturer} generator"]},
        use_query_family="generator_similar_power",
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["constraints"]["target_count"] == 1
    assert payload["constraints"]["query_template_count"] == 1
    assert payload["constraints"]["query_family_count"] == 1
    assert payload["constraints"]["use_query_family"] == "generator_similar_power"


# ---------------------------------------------------------------------------
# Observability field tests
# ---------------------------------------------------------------------------


def test_manifest_to_dict_includes_observability_keys():
    """to_dict() must always emit timing, stage_summaries, and candidate_summary."""
    manifest = DiscoveryManifest(
        run_id="acq-obs-test",
        status="scaffold",
        started_at="2026-03-26T00:00:00+00:00",
        input={"domain": "test"},
    )
    payload = manifest.to_dict()
    assert "timing" in payload
    assert "stage_summaries" in payload
    assert "candidate_summary" in payload


def test_engine_run_emits_timing_fields(tmp_path: Path):
    """Manifest must include started_at, completed_at, and elapsed_seconds >= 0."""
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/seed-a"],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    timing = payload["timing"]
    assert "started_at" in timing
    assert "completed_at" in timing
    assert "elapsed_seconds" in timing
    assert timing["elapsed_seconds"] >= 0.0
    assert timing["completed_at"] >= timing["started_at"]


def test_engine_run_emits_stage_summaries_seeker_section(tmp_path: Path):
    """stage_summaries.seeker must reflect provider, counts, and prioritization config."""
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/seed-a", "https://example.org/seed-b"],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        link_prioritization_mode="heuristic",
        link_top_k=3,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    seeker = payload["stage_summaries"]["seeker"]
    assert seeker["enabled"] is False
    assert isinstance(seeker["candidates_discovered"], int)
    assert isinstance(seeker["candidates_after_prioritization"], int)
    assert seeker["link_prioritization_mode"] == "heuristic"
    assert seeker["link_top_k"] == 3


def test_engine_run_seeker_applies_prioritization_and_emits_lineage(tmp_path: Path, monkeypatch):
    """Seeker results should be ranked and capped before manifest emission."""

    class FakeSerpApiSeeker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.queries_run = 0

        def discover(self, seeker_input):
            return [
                {
                    "url": "https://amazon.com/generator-accessory-catalog.pdf",
                    "source": "serpapi_google",
                    "title": "Shopping catalog",
                    "snippet": "Accessory catalog",
                    "reasons": ["shopping_source"],
                },
                {
                    "url": "https://generac.com/manuals/250kw-installation-manual.pdf",
                    "source": "serpapi_google",
                    "title": "Generac 250kW Installation Manual",
                    "snippet": "Industrial generator installation manual",
                    "reasons": ["manufacturer_manual"],
                },
                {
                    "url": "https://example.com/files/250kw-generator-spec.pdf",
                    "source": "serpapi_google",
                    "title": "Generator spec",
                    "snippet": "250kW generator specifications",
                    "reasons": ["spec_sheet"],
                },
                {
                    "url": "https://reddit.com/r/generators/comments/abc123/250kw_manual.pdf",
                    "source": "serpapi_google",
                    "title": "Forum thread",
                    "snippet": "User discussion",
                    "reasons": ["forum_thread"],
                },
            ]

    def fake_resolve_serpapi_state(_request: DiscoveryRequest):
        return ({"provider": "serpapi", "enabled": True, "available": True}, [], [])

    monkeypatch.setattr(
        DiscoveryEngine,
        "_resolve_serpapi_state",
        staticmethod(fake_resolve_serpapi_state),
    )
    monkeypatch.setattr(
        "psweep.discovery.connectors.serpapi_seeker.SerpApiSeeker",
        FakeSerpApiSeeker,
    )

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="generator_manuals",
        seed_urls=[],
        query="250kW generator manual",
        enable_serpapi=True,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        link_prioritization_mode="heuristic",
        link_top_k=2,
        link_prioritization_keywords=["manual", "installation", "spec"],
        selection_primary_per_target=4,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert [candidate["url"] for candidate in payload["candidates"]] == [
        "https://generac.com/manuals/250kw-installation-manual.pdf",
        "https://example.com/files/250kw-generator-spec.pdf",
    ]
    assert payload["stage_summaries"]["seeker"]["enabled"] is True
    assert payload["stage_summaries"]["seeker"]["candidates_discovered"] == 4
    assert payload["stage_summaries"]["seeker"]["candidates_after_prioritization"] == 2
    assert payload["lineage"]["link_prioritization"]["mode"] == "heuristic"
    assert payload["lineage"]["link_prioritization"]["top_k"] == 2
    assert len(payload["lineage"]["link_prioritization"]["candidates"]) == 4
    assert sorted(
        candidate["original_rank"]
        for candidate in payload["lineage"]["link_prioritization"]["candidates"]
    ) == [0, 1, 2, 3]
    assert any(
        "Link prioritizer (heuristic) ranked 4 candidate(s)"
        in note
        for note in payload["notes"]
    )


def test_engine_run_emits_unknown_target_acceptance_metrics(tmp_path: Path, monkeypatch):
    class FakeSerpApiSeeker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.queries_run = 0

        def discover(self, seeker_input):
            template_context = seeker_input.extra_params.get("template_context") or {}
            jurisdiction = template_context.get("jurisdiction")
            if jurisdiction == "Imperial County":
                return [
                    {
                        "url": "https://imperialcounty.gov/geothermal-ordinance.pdf",
                        "source": "serpapi_google",
                        "title": "Imperial County Geothermal Ordinance",
                        "snippet": "County geothermal ordinance pdf",
                        "reasons": ["county_ordinance"],
                    }
                ]
            return []

    def fake_resolve_serpapi_state(_request: DiscoveryRequest):
        return ({"provider": "serpapi", "enabled": True, "available": True}, [], [])

    monkeypatch.setattr(
        DiscoveryEngine,
        "_resolve_serpapi_state",
        staticmethod(fake_resolve_serpapi_state),
    )
    monkeypatch.setattr(
        "psweep.discovery.connectors.serpapi_seeker.SerpApiSeeker",
        FakeSerpApiSeeker,
    )

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=[],
        query="geothermal ordinance pdf",
        enable_serpapi=True,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        targets=[
            {"jurisdiction": "Imperial County", "state": "California"},
            {"jurisdiction": "Mono County", "state": "California"},
        ],
        query_templates=["{jurisdiction} {state} geothermal ordinance pdf"],
        link_prioritization_mode="off",
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    unknown_target_metrics = payload["stage_summaries"]["acceptance_metrics"]["unknown_target_discovery"]
    assert unknown_target_metrics["applicable"] is True
    assert unknown_target_metrics["measurement_mode"] == "target_matrix"
    assert unknown_target_metrics["target_count"] == 2
    assert unknown_target_metrics["targets_with_staged_candidates"] == 1
    assert unknown_target_metrics["success_rate"] == 0.5
    assert unknown_target_metrics["meets_coverage_threshold"] is False
    assert len(unknown_target_metrics["targets"]) == 2


def test_engine_run_emits_stage_summaries_routing_section(tmp_path: Path):
    """stage_summaries.routing must reflect mode, applied flag, and candidate flow counts."""
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=[
            "https://county.gov/ordinance.pdf",
            "https://external.org/other.pdf",
        ],
        query="geothermal ordinance",
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
        topology_mode="distributed",
        allowed_domains=["county.gov"],
        include_url_patterns=[r"\.pdf$"],
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    routing = payload["stage_summaries"]["routing"]
    assert routing["mode"] == "distributed"
    assert routing["applied"] is False
    assert isinstance(routing["candidates_in"], int)
    assert isinstance(routing["candidates_out"], int)
    assert routing["candidates_out"] <= routing["candidates_in"]


def test_engine_run_emits_stage_summaries_downloads_section_dry_run(tmp_path: Path):
    """In dry-run mode, downloads stage_summary must report zero totals."""
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/seed-a"],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    downloads = payload["stage_summaries"]["downloads"]
    assert downloads["total"] == 0
    assert downloads["downloaded"] == 0
    assert downloads["failed"] == 0
    assert downloads["total_bytes"] == 0


def test_engine_run_emits_candidate_summary_by_acceptance_class(tmp_path: Path):
    """candidate_summary must aggregate by acceptance class and source."""
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        seed_urls=[
            "https://example.org/seed-a",
            "https://example.org/seed-b",
            "https://example.org/seed-c",
        ],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    summary = payload["candidate_summary"]
    assert summary["total"] == 3
    assert isinstance(summary["by_acceptance_class"], dict)
    assert isinstance(summary["by_source"], dict)
    # All seed_url candidates must appear under seed_url source key
    assert summary["by_source"].get("seed_url", 0) == 3
    # All acceptance classes must be valid
    for cls in summary["by_acceptance_class"]:
        assert cls in {"accepted", "needs_review", "rejected"}


def test_build_download_summary_aggregates_status_counts():
    """_build_download_summary must correctly bucket status counts and total bytes."""
    records = [
        {"status": "downloaded", "bytes": 1024},
        {"status": "downloaded", "bytes": 2048},
        {"status": "skipped_unsupported_type", "bytes": None},
        {"status": "failed", "bytes": None},
        {"status": "skipped_policy", "bytes": None},
    ]
    summary = DiscoveryEngine._build_download_summary(records)

    assert summary["total"] == 5
    assert summary["downloaded"] == 2
    assert summary["failed"] == 1
    assert summary["skipped"] == 2
    assert summary["total_bytes"] == 3072
    assert summary["by_status"]["downloaded"] == 2
    assert summary["by_status"]["failed"] == 1


def test_build_candidate_summary_counts_by_acceptance_class_and_source():
    """_build_candidate_summary must correctly count by class and source."""
    candidates = [
        DiscoveryCandidate(
            url="https://a.com/doc.pdf",
            source="serpapi_google",
            score=CandidateScore(url_signal=1.0, trust_signal=1.0),
        ),
        DiscoveryCandidate(
            url="https://b.com/doc.pdf",
            source="serpapi_google",
            score=CandidateScore(url_signal=0.0, trust_signal=0.0),
        ),
        DiscoveryCandidate(
            url="https://c.com/doc.pdf",
            source="seed_url",
            score=CandidateScore(url_signal=0.5, trust_signal=0.5),
        ),
    ]
    summary = DiscoveryEngine._build_candidate_summary(candidates)

    assert summary["total"] == 3
    assert summary["by_source"]["serpapi_google"] == 2
    assert summary["by_source"]["seed_url"] == 1
    total_class_count = sum(summary["by_acceptance_class"].values())
    assert total_class_count == 3
