"""Completion means exhausted current work and verified published evidence."""

import json
from unittest.mock import Mock

import pytest

from psweep.discovery import DiscoveryEngine, DiscoveryRequest
from psweep.discovery.connectors.serpapi_seeker import SerpApiSeeker
from discovery_helpers import FakeResponse


def _request(tmp_path, **changes):
    values = {
        "domain": "fixture",
        "seed_urls": [],
        "query": None,
        "enable_serpapi": True,
        "targets": [{"label": "target", "query": "documents"}],
        "output_documents": tmp_path / "domain/runs/current/documents",
        "output_manifest": tmp_path / "domain/runs/current/manifest.json",
        "dry_run": False,
        "partition_by": ["label"],
        "robots_policy_mode": "ignore",
        "tos_policy_mode": "ignore",
        "selection_primary_per_target": 2,
    }
    values.update(changes)
    return DiscoveryRequest(**values)


def _fake_search(monkeypatch, response):
    monkeypatch.setattr(SerpApiSeeker, "_ensure_prepared", lambda _self: None)
    monkeypatch.setattr(
        DiscoveryEngine,
        "_resolve_serpapi_state",
        staticmethod(
            lambda _request: ({"enabled": True, "available": True}, [], [])
        ),
    )
    search = Mock(return_value=response)
    monkeypatch.setattr(
        SerpApiSeeker, "_build_search_callable", lambda _self: search
    )
    return search


def test_empty_exhausted_target_is_completed_without_searching_again(
    monkeypatch, tmp_path
):
    """Empty results finish selection rather than repeating paid searches."""
    search = _fake_search(monkeypatch, {"organic_results": []})
    request = _request(tmp_path)
    first = DiscoveryEngine().run(request)
    assert first.status == "empty"
    assert (tmp_path / "domain/checkpoint.json").exists()
    DiscoveryEngine().run(request)
    assert search.call_count == 1


def test_file_cap_does_not_checkpoint_truncated_target(monkeypatch, tmp_path):
    """A successful first download does not finish a two-document target."""
    _fake_search(
        monkeypatch,
        {
            "organic_results": [
                {"link": "https://example.org/a.pdf"},
                {"link": "https://example.org/b.pdf"},
            ]
        },
    )
    monkeypatch.setattr(
        "requests.get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    result = DiscoveryEngine().run(_request(tmp_path, max_files=1))
    assert result.curated_count == 1
    checkpoint = tmp_path / "domain/checkpoint.json"
    if checkpoint.exists():
        assert json.loads(checkpoint.read_text())["entries"] == {}


def test_deleted_curated_file_invalidates_completion(monkeypatch, tmp_path):
    """Resume verifies published bytes rather than trusting a target label."""
    search = _fake_search(
        monkeypatch,
        {"organic_results": [{"link": "https://example.org/a.pdf"}]},
    )
    monkeypatch.setattr(
        "requests.get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    request = _request(tmp_path, selection_primary_per_target=1)
    DiscoveryEngine().run(request)
    curated = tmp_path / "domain/curated/by_label/target/a.pdf"
    assert curated.exists()
    curated.unlink()
    DiscoveryEngine().run(request)
    assert search.call_count == 2


def test_changed_policy_invalidates_completion(monkeypatch, tmp_path):
    """Effective behavior changes cannot reuse a stale completed target."""
    search = _fake_search(
        monkeypatch,
        {"organic_results": [{"link": "https://example.org/a.pdf"}]},
    )
    monkeypatch.setattr(
        "requests.get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    request = _request(tmp_path, selection_primary_per_target=1)
    DiscoveryEngine().run(request)
    request.selection_exclude_url_patterns = ["archive"]
    DiscoveryEngine().run(request)
    assert search.call_count == 2


def test_cap_preserves_prior_complete_partition(monkeypatch, tmp_path):
    """Capped samples stay local rather than replacing prior evidence."""
    _fake_search(
        monkeypatch,
        {
            "organic_results": [
                {"link": "https://example.org/a.pdf"},
                {"link": "https://example.org/b.pdf"},
            ]
        },
    )
    monkeypatch.setattr(
        "requests.get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    previous = tmp_path / "domain/curated/by_label/target/prior.pdf"
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"previous complete evidence")
    DiscoveryEngine().run(_request(tmp_path, max_files=1))
    assert previous.read_bytes() == b"previous complete evidence"
    assert not (previous.parent / "a.pdf").exists()


def test_seed_failure_without_metadata_preserves_prior_host(
    monkeypatch, tmp_path
):
    """Unattributed seed errors protect their host's existing curation."""
    prior = tmp_path / "domain/curated/by_host/example.org/old.pdf"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"old complete evidence")

    def get(url, **_kwargs):
        if url.endswith("bad.pdf"):
            raise OSError("connection failed")
        return FakeResponse(url=url)

    monkeypatch.setattr("requests.get", get)
    request = _request(
        tmp_path,
        targets=None,
        enable_serpapi=False,
        partition_by=None,
        seed_urls=[
            "https://example.org/good.pdf",
            "https://example.org/bad.pdf",
        ],
    )
    result = DiscoveryEngine().run(request)
    assert result.status == "partial_failure"
    assert prior.read_bytes() == b"old complete evidence"


def test_empty_search_cannot_complete_a_failed_seed(monkeypatch, tmp_path):
    """Whole-target outcome, not search-only outcome, governs completion."""
    _fake_search(monkeypatch, {"organic_results": []})
    monkeypatch.setattr(
        "requests.get", Mock(side_effect=OSError("failed seed"))
    )
    request = _request(tmp_path, seed_urls=["https://example.org/target.pdf"])
    result = DiscoveryEngine().run(request)
    assert result.status == "failed"
    checkpoint = tmp_path / "domain/checkpoint.json"
    if checkpoint.exists():
        assert not json.loads(checkpoint.read_text())["entries"]


def test_fixed_empty_reuse_remains_cached_on_third_run(monkeypatch, tmp_path):
    """Immutable outcome evidence survives canonical manifest updates."""
    search = _fake_search(monkeypatch, {"organic_results": []})
    request = _request(tmp_path)
    for _ in range(3):
        assert DiscoveryEngine().run(request).status == "empty"
    assert search.call_count == 1


def test_reused_none_retention_never_deletes_consolidated(
    monkeypatch, tmp_path
):
    """Retention applies to fresh run-local data, not reused corpus files."""
    search = _fake_search(
        monkeypatch,
        {"organic_results": [{"link": "https://example.org/a.pdf"}]},
    )
    monkeypatch.setattr(
        "requests.get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    request = _request(
        tmp_path, selection_primary_per_target=1, retention_documents="none"
    )
    DiscoveryEngine().run(request)
    prior = tmp_path / "domain/curated/by_label/target/a.pdf"
    assert prior.exists()
    result = DiscoveryEngine().run(request)
    assert result.status == "reused"
    assert prior.exists() and search.call_count == 1
    manifest = json.loads(result.manifest_path.read_text())
    assert (
        manifest["status"] == "reused" and manifest["run_id"] == result.run_id
    )
    editable = json.loads(
        (tmp_path / "domain/runs/current/manifest.json").read_text()
    )
    assert editable["status"] == "success" and editable["downloads"]


def test_failed_final_publication_preserves_fixed_local_outputs(
    monkeypatch, tmp_path
):
    """Local artifacts are not written before final coherent publication."""
    _fake_search(
        monkeypatch,
        {"organic_results": [{"link": "https://example.org/a.pdf"}]},
    )
    monkeypatch.setattr(
        "requests.get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    request = _request(tmp_path, selection_primary_per_target=1)
    first = DiscoveryEngine().run(request)
    before_manifest = first.manifest_path.read_bytes()
    before_index = first.download_index_path.read_bytes()
    before_curated = (first.curated_dir / "by_label/target/a.pdf").read_bytes()
    request.selection_exclude_url_patterns = ["archive"]
    monkeypatch.setattr(
        "psweep.discovery.engine.publish_manual_curation",
        Mock(side_effect=OSError("publication failed")),
    )
    with pytest.raises(OSError, match="publication failed"):
        DiscoveryEngine().run(request)
    assert first.manifest_path.read_bytes() == before_manifest
    assert first.download_index_path.read_bytes() == before_index
    assert (
        first.curated_dir / "by_label/target/a.pdf"
    ).read_bytes() == before_curated


def test_fixed_partial_resume_does_not_invalidate_previous_target(
    monkeypatch, tmp_path
):
    """Each completion references immutable manifest and index evidence."""
    targets = [{"label": label, "query": label} for label in ("alpha", "beta")]
    search = _fake_search(monkeypatch, {})
    search.side_effect = lambda params: {
        "organic_results": [{"link": f"https://example.org/{params['q']}.pdf"}]
    }
    fail_beta = [True]

    def get(url, **_kwargs):
        if url.endswith("beta.pdf") and fail_beta[0]:
            raise OSError("beta unavailable")
        return FakeResponse(url=url)

    monkeypatch.setattr("requests.get", get)
    request = _request(tmp_path, targets=targets, retry_max_attempts=1)
    assert DiscoveryEngine().run(request).status == "partial_failure"
    fail_beta[0] = False
    assert DiscoveryEngine().run(request).status == "success"
    assert DiscoveryEngine().run(request).status == "reused"
    assert search.call_count == 3


def test_unattributed_seed_still_publishes_with_completed_targets(
    monkeypatch, tmp_path
):
    """Completed target work cannot suppress independent seed acquisition."""
    _fake_search(
        monkeypatch,
        {"organic_results": [{"link": "https://example.org/a.pdf"}]},
    )
    monkeypatch.setattr(
        "requests.get", lambda url, **_kwargs: FakeResponse(url=url)
    )
    request = _request(
        tmp_path,
        selection_primary_per_target=1,
        seed_urls=["https://other.org/independent.pdf"],
    )
    DiscoveryEngine().run(request)
    result = DiscoveryEngine().run(request)
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["downloads"]
    assert manifest["status"] == "success"
    assert result.review_index_path.exists()
    assert result.curated_dir != tmp_path / "domain/curated"
