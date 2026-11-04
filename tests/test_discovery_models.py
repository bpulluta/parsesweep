import pytest
from pathlib import Path
import re
import json
import csv
import sys
from unittest.mock import Mock

from psweep.discovery import (
    DiscoveryCandidate,
    DiscoveryEngine,
    DiscoveryManifest,
    DiscoveryRequest,
    CandidateScore,
)
from psweep.workspace import Workspace

from discovery_helpers import FakeResponse as _FakeResponse
from discovery_helpers import patch_requests_get as _patch_requests_get


@pytest.fixture(autouse=True)
def _outputs_in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


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
        status="success",
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
        sources=["https://example.org/seed-a", "https://example.org/seed-b"],
        dry_run=True,
    )

    result = engine.run(request)
    payload = result.manifest_path.read_text(encoding="utf-8")

    assert result.manifest_path.exists()
    assert result.documents_dir.exists()
    assert '"status": "dry_run"' in payload
    assert '"acceptance_class"' in payload
    assert payload.count('"source": "configured"') == 2


def test_engine_run_uses_deterministic_default_paths_and_lineage(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=["https://example.org/seed-a"],
        queries=["geothermal ordinance"],
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert re.match(
        r"^acq-geothermal-ordinances-\d{8}T\d{12}Z$",
        result.run_id,
    )
    # Each run is one self-contained folder: documents/ lives under the run dir.
    assert result.documents_dir == (
        Workspace("geothermal_ordinances").discovered.resolve()
        / "runs" / result.run_id / "documents"
    )
    assert (
        result.manifest_path.parent.as_posix()
        == result.documents_dir.parent.as_posix()
    )
    assert payload["lineage"]["run_id"] == result.run_id
    assert payload["lineage"]["documents_dir"] == result.documents_dir.as_posix()
    assert payload["lineage"]["manifest_path"] == result.manifest_path.as_posix()


def test_engine_run_records_invalid_direct_sources_as_skipped(tmp_path: Path):
    """Direct API callers bypass config validation; bad URLs are skipped, not fetched."""
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=["not-a-url", "ftp://bad"],
        dry_run=False,
        robots_policy_mode="ignore",
        tos_policy_mode="ignore",
    )
    payload = json.loads(DiscoveryEngine().run(request).manifest_path.read_text(encoding="utf-8"))
    assert [d["status"] for d in payload["downloads"]] == ["skipped_invalid_url"] * 2


def test_engine_search_uses_http_without_serpapi_sdk(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(sys.modules, "serpapi", None)
    monkeypatch.setenv("SERPAPI_API_KEY", "synthetic-search-key")
    response = Mock(status_code=200, headers={}, url="https://serpapi.com/search.json")
    response.json.return_value = {"organic_results": [{
        "link": "https://example.org/ordinance.pdf",
        "title": "Geothermal ordinance",
    }]}
    request_get = Mock(return_value=response)
    monkeypatch.setattr("psweep.discovery.network.request_get", request_get)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=[],
        queries=["geothermal ordinance"],
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["lineage"]["seeker"]["provider"] == "serpapi"
    assert payload["lineage"]["seeker"]["enabled"] is True
    assert payload["lineage"]["seeker"]["available"] is True
    assert payload["status"] == "dry_run", payload["errors"]
    assert payload["error_summary"]["total_errors"] == 0
    assert payload["candidates"][0]["url"] == "https://example.org/ordinance.pdf"
    request_get.assert_called_once()
    response.close.assert_called_once()


def test_engine_run_emits_structured_error_when_serpapi_key_missing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=["https://example.org/valid"],
        queries=["geothermal ordinance"],
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
        sources=["https://example.org/docs/test-ordinance.pdf"],
        dry_run=False,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert payload["error_summary"]["total_errors"] == 0
    assert len(payload["downloads"]) == 1
    assert payload["downloads"][0]["status"] == "downloaded"
    assert payload["downloads"][0]["source_scope"] == "shared"
    assert payload["downloads"][0]["target_id"] is None
    assert payload["downloads"][0]["relative_path"].startswith("_shared/")
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
        sources=["https://example.org/landing-page"],
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

    monkeypatch.setattr("psweep.discovery.network.request_get", lambda url, **kwargs: _FakeResponse(url=url))
    monkeypatch.setattr(DiscoveryEngine, "_resolve_serpapi_state", staticmethod(fake_resolve_serpapi_state))
    monkeypatch.setattr(DiscoveryEngine, "_run_seeker", staticmethod(fake_run_seeker))

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=[],
        queries=["geothermal ordinance"],
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

    monkeypatch.setattr("psweep.discovery.network.request_get", lambda url, **kwargs: _FakeResponse(url=url))
    monkeypatch.setattr(DiscoveryEngine, "_resolve_serpapi_state", staticmethod(fake_resolve_serpapi_state))
    monkeypatch.setattr(DiscoveryEngine, "_run_seeker", staticmethod(fake_run_seeker))

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=[],
        queries=["geothermal ordinance"],
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

    monkeypatch.setattr("psweep.discovery.network.request_get", flaky_get)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=["https://example.org/docs/retry-ordinance.pdf"],
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
        sources=["https://example.org/docs/test-ordinance.pdf"],
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

    monkeypatch.setattr("psweep.discovery.network.request_get", fake_get)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=["https://example.org/docs/policy-test.pdf"],
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

    monkeypatch.setattr("psweep.discovery.network.request_get", fake_get)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=["https://example.org/docs/blocked.pdf"],
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

    monkeypatch.setattr("psweep.discovery.network.request_get", lambda url, **kwargs: _FakeResponse(url=url))
    monkeypatch.setattr("time.sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr("time.monotonic", lambda: next(monotonic_values, 0.10))

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=[
            "https://example.org/docs/a.pdf",
            "https://example.org/docs/b.pdf",
        ],
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

    monkeypatch.setattr("psweep.discovery.network.request_get", fake_get)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="apple_sec_filings",
        sources=["https://example.org/docs/test-filing.html"],
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


def test_engine_run_dry_run_has_no_download_index(tmp_path: Path):
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=["https://example.org/seed-a"],
        queries=["geothermal ordinance"],
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert result.download_index_path is None
    assert payload["lineage"]["download_index_csv"] is None


def test_engine_builds_seeker_inputs_from_targets():
    """Row ``query`` overrides ``queries``; ``urls`` never reach the template context."""
    request = DiscoveryRequest(
        domain="generator_manuals",
        queries=["{manufacturer} {power_class_kw} kW generator spec pdf"],
        dry_run=True,
        targets=[
            {"manufacturer": "Generac", "power_class_kw": "200-300",
             "urls": ["https://generac.com/a.pdf"]},
            {"manufacturer": "John Deere", "query": "deere manual"},
        ],
        seeker_max_results=8,
    )

    seeker_inputs = DiscoveryEngine._build_seeker_inputs(request)

    assert [item.queries for item in seeker_inputs] == [
        ["{manufacturer} {power_class_kw} kW generator spec pdf"],
        ["deere manual"],
    ]
    assert all(item.max_results == 8 for item in seeker_inputs)
    assert seeker_inputs[0].context == {"manufacturer": "Generac", "power_class_kw": "200-300"}
    assert seeker_inputs[1].context == {"manufacturer": "John Deere"}


def test_engine_run_records_input_constraints(tmp_path: Path):
    request = DiscoveryRequest(
        domain="generator_manuals",
        sources=["https://example.org/a.pdf"],
        dry_run=True,
        targets=[{"manufacturer": "Generac", "urls": ["https://generac.com/a.pdf"]}],
    )
    payload = json.loads(DiscoveryEngine().run(request).manifest_path.read_text(encoding="utf-8"))
    assert payload["constraints"]["target_count"] == 1
    assert payload["constraints"]["source_count"] == 1
    assert "query_count" not in payload["constraints"]


# ---------------------------------------------------------------------------
# Observability field tests
# ---------------------------------------------------------------------------


def test_manifest_to_dict_includes_observability_keys():
    """to_dict() must always emit timing, stage_summaries, and candidate_summary."""
    manifest = DiscoveryManifest(
        run_id="acq-obs-test",
        status="success",
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
        sources=["https://example.org/seed-a"],
        queries=["geothermal ordinance"],
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
        sources=["https://example.org/seed-a", "https://example.org/seed-b"],
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
        sources=[],
        queries=["250kW generator manual"],
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
            jurisdiction = (seeker_input.context or {}).get("jurisdiction")
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
        sources=[],
        queries=["{jurisdiction} {state} geothermal ordinance pdf"],
        dry_run=True,
        targets=[
            {"jurisdiction": "Imperial County", "state": "California"},
            {"jurisdiction": "Mono County", "state": "California"},
        ],
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


def test_engine_run_emits_stage_summaries_crawl_section(tmp_path: Path):
    """stage_summaries.crawl reports provider, depth, and candidate flow."""
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=["https://county.gov/ordinance.pdf"],
        dry_run=True,
        max_depth=2,
    )
    payload = json.loads(DiscoveryEngine().run(request).manifest_path.read_text(encoding="utf-8"))
    crawl = payload["stage_summaries"]["crawl"]
    assert crawl["provider"] == "http"
    assert crawl["max_depth"] == 2
    assert crawl["applied"] is False
    assert crawl["candidates_in"] == crawl["candidates_out"] == 1


def test_engine_run_emits_stage_summaries_downloads_section_dry_run(tmp_path: Path):
    """In dry-run mode, downloads stage_summary must report zero totals."""
    engine = DiscoveryEngine()
    request = DiscoveryRequest(
        domain="geothermal_ordinances",
        sources=["https://example.org/seed-a"],
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
        sources=[
            "https://example.org/seed-a",
            "https://example.org/seed-b",
            "https://example.org/seed-c",
        ],
        dry_run=True,
    )

    result = engine.run(request)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    summary = payload["candidate_summary"]
    assert summary["total"] == 3
    assert isinstance(summary["by_acceptance_class"], dict)
    assert isinstance(summary["by_source"], dict)
    assert summary["by_source"].get("configured", 0) == 3
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
