"""Completion helpers preserve whole-target and publication boundaries."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from psweep.discovery.engine import (
    DiscoveryEngine,
    DiscoveryRequest,
    _PartitionIndex,
)
from psweep.discovery.models import DiscoveryManifest
from psweep.discovery.publication import resolve_publication_paths


def _request(tmp_path, **changes):
    values = {
        "domain": "fixture",
        "seed_urls": [],
        "query": None,
        "enable_serpapi": False,
        "output_documents": tmp_path / "domain/runs/current/documents",
        "output_manifest": tmp_path / "domain/runs/current/manifest.json",
        "dry_run": False,
        "partition_by": ["label"],
    }
    values.update(changes)
    return DiscoveryRequest(**values)


def test_failed_shared_partition_blocks_other_target_completion(tmp_path):
    """All failures are blocked before counting successful target records."""
    request = _request(tmp_path, partition_by=["group"])
    first = {"label": "first", "group": "shared"}
    second = {"label": "second", "group": "shared"}
    blocked = _PartitionIndex()
    entries = DiscoveryEngine()._download_completions(
        request=request,
        documents_dir=request.output_documents,
        download_records=[
            {"status": "downloaded", "target_metadata": first},
            {"status": "failed", "target_metadata": second},
        ],
        incomplete_keys=set(),
        blocked=blocked,
        run_id="current",
        started_at=datetime.now(timezone.utc),
    )
    assert not entries
    assert blocked


def test_empty_completion_excludes_incomplete_and_attempted_targets(tmp_path):
    """An exhausted search cannot complete a target with an attempted seed."""
    targets = [{"label": label} for label in ("empty", "failed", "capped")]
    request = _request(tmp_path, targets=targets)
    engine = DiscoveryEngine()
    entries = {}
    parts = engine._empty_target_completions(
        request=request,
        documents_dir=request.output_documents,
        selection_metrics=[
            {
                "target_index": index,
                "search_complete": True,
                "selected_candidates": 0,
            }
            for index in range(len(targets))
        ],
        download_records=[{"status": "failed", "target_metadata": targets[1]}],
        incomplete_keys={engine._target_checkpoint_key(targets[2])},
        entries=entries,
        run_id="current",
        started_at=datetime.now(timezone.utc),
    )
    assert set(entries) == {engine._target_checkpoint_key(targets[0])}
    assert next(iter(entries.values()))["outcome"] == "empty"
    assert len(parts) == 1


def test_reuse_preview_does_not_render_or_write_artifacts(tmp_path):
    """Result preview only resolves already-published corpus locations."""
    request = _request(tmp_path, retention_documents="none")
    paths = resolve_publication_paths(
        request.output_documents, request.output_manifest, request.domain
    )
    result = DiscoveryEngine()._preview_completion_result(
        request=request,
        paths=paths,
        run_id="current",
        candidates=[],
        download_records=[],
        errors=[],
        empty_parts=set(),
        completed_entries={
            "target": {"curated_paths": {"old/document.pdf": "hash"}}
        },
        all_reused=True,
    )
    assert result.status == "reused"
    assert result.curated_count == 1
    assert result.curated_dir == paths.domain_dir / "curated"
    assert result.download_index_path == (
        paths.domain_dir / "download_index.csv"
    )
    assert not list(tmp_path.iterdir())


def test_publication_failure_occurs_before_final_metadata_writes(
    monkeypatch, tmp_path
):
    """Rendered indexes and outcome metadata share one transaction."""
    request = _request(tmp_path)
    paths = resolve_publication_paths(
        request.output_documents, request.output_manifest, request.domain
    )
    engine = DiscoveryEngine()
    records = [{"status": "downloaded", "relative_path": "target/doc.pdf"}]
    result = engine._preview_completion_result(
        request=request,
        paths=paths,
        run_id="current",
        candidates=[],
        download_records=records,
        errors=[],
        empty_parts=set(),
        completed_entries={},
        all_reused=False,
    )
    manifest = DiscoveryManifest(
        run_id="current",
        status=result.status,
        started_at=datetime.now(timezone.utc).isoformat(),
        input={},
        constraints={},
        lineage={"download_index_csv": str(result.download_index_path)},
        downloads=records,
    )
    publish = Mock(side_effect=OSError("publication failed"))
    monkeypatch.setattr(
        "psweep.discovery.engine.publish_manual_curation", publish
    )
    with pytest.raises(OSError, match="publication failed"):
        engine._publish_completion(
            request=request,
            paths=paths,
            result=result,
            manifest=manifest,
            completed_entries={},
            attempted_parts=set(),
            blocked=_PartitionIndex(),
            all_reused=False,
        )
    metadata = publish.call_args.kwargs["metadata_updates"]
    assert paths.run_index in metadata
    assert result.review_index_path in metadata
    assert paths.manifest_path in metadata
    assert paths.manifest_path.parent / "outcomes/current.json" in metadata
    assert not list(tmp_path.iterdir())
