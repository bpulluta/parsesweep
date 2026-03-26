from pathlib import Path
import re
import json
import csv

from streamline_extract.acquisition import (
    AcquisitionCandidate,
    AcquisitionEngine,
    AcquisitionManifest,
    AcquisitionRequest,
    CandidateScore,
)


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
    candidate = AcquisitionCandidate(
        url="https://example.org/doc.pdf",
        source="seed_url",
        score=CandidateScore(url_signal=1.0, trust_signal=1.0),
        reasons=["Matched seed URL pattern"],
    )
    manifest = AcquisitionManifest(
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
    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    assert result.documents_dir.as_posix().endswith(f"/runs/{result.run_id}")
    assert payload["lineage"]["run_id"] == result.run_id
    assert payload["lineage"]["documents_dir"] == result.documents_dir.as_posix()
    assert payload["lineage"]["manifest_path"] == result.manifest_path.as_posix()


def test_engine_run_emits_taxonomy_aligned_structured_errors_for_invalid_seeds(tmp_path: Path):
    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    assert all(record["stage"] == "acquisition.seed_validation" for record in records)
    assert all(record["category"] == "input_validation" for record in records)
    assert all(record["code"] == "invalid_input" for record in records)


def test_engine_run_emits_structured_error_when_serpapi_dependency_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda _: None)

    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    assert payload["errors"][0]["stage"] == "acquisition.seeker_init"


def test_engine_run_emits_structured_error_when_serpapi_key_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda _: object())
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_KEY", raising=False)

    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    assert payload["errors"][0]["stage"] == "acquisition.seeker_init"
    assert payload["errors"][0]["category"] == "input_validation"
    assert payload["errors"][0]["code"] == "invalid_input"


def test_engine_run_downloads_supported_candidate_for_non_dry_run(tmp_path: Path, monkeypatch):
    class FakeResponse:
        def __init__(self):
            self.url = "https://example.org/docs/test-ordinance.pdf"
            self.headers = {"Content-Type": "application/pdf"}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size: int = 65536):
            yield b"%PDF-1.4\n"
            yield b"mock-pdf-content"

    def fake_get(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)

    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    class FakeResponse:
        def __init__(self):
            self.url = "https://example.org/landing-page"
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size: int = 65536):
            yield b"<html>not-a-document</html>"

    def fake_get(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)

    engine = AcquisitionEngine()
    request = AcquisitionRequest(
        domain="tariffs",
        seed_urls=["https://example.org/landing-page"],
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
    assert payload["downloads"][0]["status"] == "skipped_unsupported_type"
    assert list((tmp_path / "docs").glob("*")) == []


def test_engine_run_retries_transient_download_failure_then_succeeds(tmp_path: Path, monkeypatch):
    class FakeResponse:
        def __init__(self):
            self.url = "https://example.org/docs/retry-ordinance.pdf"
            self.headers = {"Content-Type": "application/pdf"}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size: int = 65536):
            yield b"%PDF-1.4\n"
            yield b"retry-success"

    attempts = {"count": 0}

    def flaky_get(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary timeout")
        return FakeResponse()

    monkeypatch.setattr("requests.get", flaky_get)

    engine = AcquisitionEngine()
    request = AcquisitionRequest(
        domain="geothermal_ordinances",
        seed_urls=["https://example.org/docs/retry-ordinance.pdf"],
        query=None,
        enable_serpapi=False,
        output_documents=tmp_path / "docs",
        output_manifest=tmp_path / "manifest.json",
        dry_run=False,
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
    class FakeResponse:
        def __init__(self):
            self.url = "https://example.org/docs/test-ordinance.pdf"
            self.headers = {"Content-Type": "application/pdf"}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size: int = 65536):
            yield b"%PDF-1.4\n"
            yield b"controls"

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: FakeResponse())

    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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


def test_engine_run_applies_request_rate_limiter_for_downloads(tmp_path: Path, monkeypatch):
    class FakeResponse:
        def __init__(self, url: str):
            self.url = url
            self.headers = {"Content-Type": "application/pdf"}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size: int = 65536):
            yield b"%PDF-1.4\n"
            yield b"rate-limit"

    sleep_calls: list[float] = []
    monotonic_values = iter([0.0, 0.0, 0.03, 0.03, 0.05, 0.05, 0.10, 0.10])

    monkeypatch.setattr("requests.get", lambda url, **kwargs: FakeResponse(url))
    monkeypatch.setattr("time.sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setattr("time.monotonic", lambda: next(monotonic_values, 0.10))

    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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


def test_engine_run_routes_centralized_topology_via_http_digger_live_hub_page(tmp_path: Path, monkeypatch):
    class FakeResponse:
        status_code = 200

        def __init__(self):
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
            self.text = (
                '<html><body>'
                '<a href="/docs/geothermal-ordinance.pdf">Geothermal Ordinance</a>'
                '<a href="/docs/notice.html">Notice</a>'
                '</body></html>'
            )

        def raise_for_status(self):
            return None

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: FakeResponse())

    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    class FakeResponse:
        def __init__(self):
            self.url = "https://example.org/docs/geothermal.pdf"
            self.headers = {"Content-Type": "application/pdf"}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size: int = 65536):
            yield b"%PDF-1.4\n"
            yield b"jurisdiction-layout"

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: FakeResponse())

    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    class FakeResponse:
        def __init__(self):
            self.url = "https://www.chaffeecounty.org/documents/53007.pdf"
            self.headers = {"Content-Type": "application/pdf"}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size: int = 65536):
            yield b"%PDF-1.4\n"
            yield b"query-inference"

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: FakeResponse())

    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["url"] == "https://county.gov/ordinance.pdf"
    assert "Routed via distributed digger path" in payload["candidates"][0]["reasons"]
    assert payload["lineage"]["routing"]["mode"] == "distributed"
    assert payload["lineage"]["routing"]["applied"] is True
    assert payload["lineage"]["routing"]["artifact_count"] == 1
    assert payload["lineage"]["routing"]["discovery_modes"] == ["seed_only"]
    assert any(
        note == "Distributed routing staged 1 candidate(s) through digger."
        for note in payload["notes"]
    )


def test_engine_run_routes_centralized_topology_through_hub_sweep(tmp_path: Path):
    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    assert any(
        note == "Centralized routing staged 1 candidate(s) through hub sweep."
        for note in payload["notes"]
    )


def test_engine_run_centralized_routing_falls_back_when_no_sweep_matches(tmp_path: Path):
    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
    assert urls == [
        "https://docs.county.gov/hub-geothermal-ordinance.pdf",
        "https://county.gov/distributed-ordinance.pdf",
    ]
    assert "Routed via centralized hub sweep" in payload["candidates"][0]["reasons"]
    assert "Routed via distributed digger path" in payload["candidates"][1]["reasons"]
    assert payload["lineage"]["routing"]["mode"] == "hybrid"
    assert payload["lineage"]["routing"]["centralized_candidate_count"] == 1
    assert payload["lineage"]["routing"]["distributed_candidate_count"] == 1
    assert payload["lineage"]["routing"]["final_candidate_count"] == 2
    assert len(payload["lineage"]["routing"]["stages"]) == 2
    assert any(
        note == "Hybrid routing produced 2 candidate(s) after centralized-plus-distributed sequencing."
        for note in payload["notes"]
    )


def test_engine_run_hybrid_routing_falls_back_to_distributed_when_hub_empty(tmp_path: Path):
    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
        note == "Distributed routing staged 1 candidate(s) through digger."
        for note in payload["notes"]
    )


def test_engine_builds_seeker_inputs_from_targets_and_query_family():
    request = AcquisitionRequest(
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

    seeker_inputs = AcquisitionEngine._build_seeker_inputs(request)

    assert len(seeker_inputs) == 2
    assert all(item.query == "" for item in seeker_inputs)
    assert all(item.max_results == 8 for item in seeker_inputs)
    assert seeker_inputs[0].extra_params["template_context"]["manufacturer"] == "Generac"
    assert seeker_inputs[1].extra_params["template_context"]["manufacturer"] == "John Deere"
    assert seeker_inputs[0].extra_params["use_query_family"] == "generator_similar_power"


def test_engine_run_records_targeted_query_constraints(tmp_path: Path):
    engine = AcquisitionEngine()
    request = AcquisitionRequest(
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
