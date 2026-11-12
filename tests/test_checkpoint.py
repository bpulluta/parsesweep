"""Tests for discovery checkpointing: key generation and load/save/resume.

Large runs must resume without redoing completed targets. Completed target
keys are persisted to checkpoint.json and pruned from request.targets before
discovery runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from psweep.discovery import publication
from psweep.discovery.engine import DiscoveryEngine
from psweep.discovery.targets import identify_target
from psweep.discovery.publication import (
    PublicationError,
    artifact_sha256,
    effective_discovery_fingerprint,
    load_prior_outcomes,
)


def _count_checkpoint_hash_work(monkeypatch):
    """Count actual digest calls and bytes, without replacing file hashing."""
    original = publication.hashlib.sha256
    work = {"calls": 0, "bytes": 0}

    class CountingDigest:
        def __init__(self):
            work["calls"] += 1
            self.digest = original()

        def update(self, chunk):
            work["bytes"] += len(chunk)
            self.digest.update(chunk)

        def hexdigest(self):
            return self.digest.hexdigest()

    monkeypatch.setattr(publication.hashlib, "sha256", CountingDigest)
    return work


class TestCheckpointHashScale:
    @pytest.mark.parametrize("owners", [20, 1000, 10000])
    def test_shared_artifacts_hash_once_per_load(self, tmp_path, monkeypatch, owners):
        common = _evidence(tmp_path, "common")
        entries = {}
        expected_bytes = sum(Path(raw).stat().st_size for raw in common["artifacts"])
        expected_bytes += sum(Path(raw).stat().st_size for raw in common["curated_paths"])
        for owner in range(owners):
            document = tmp_path / f"owner-{owner}.pdf"
            document.write_bytes(f"record-{owner}".encode())
            expected_bytes += document.stat().st_size
            entries[str(owner)] = {
                **common,
                "curated_paths": {
                    **common["curated_paths"],
                    str(document): artifact_sha256(document),
                },
            }
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_text(json.dumps({"version": 2, "entries": entries}))
        work = _count_checkpoint_hash_work(monkeypatch)

        assert len(publication.load_checkpoint(checkpoint)) == owners
        assert work == {"calls": owners + 3, "bytes": expected_bytes}

    @pytest.mark.parametrize("wrong_first", [True, False])
    def test_digest_memo_is_not_expected_hash_validity(self, tmp_path, monkeypatch, wrong_first):
        entry = _evidence(tmp_path, "common")
        work = _count_checkpoint_hash_work(monkeypatch)
        manifest = next(iter(entry["artifacts"]))
        wrong = {**entry, "artifacts": {**entry["artifacts"], manifest: "0" * 64}}
        entries = (
            {"wrong": wrong, "right": entry} if wrong_first else {"right": entry, "wrong": wrong}
        )
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_text(json.dumps({"version": 2, "entries": entries}))

        assert set(publication.load_checkpoint(checkpoint)) == {"right"}
        assert work["calls"] == 3

    def test_digest_memo_does_not_survive_load(self, tmp_path, monkeypatch):
        entry = _evidence(tmp_path, "common")
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_text(json.dumps({"version": 2, "entries": {"owner": entry}}))
        work = _count_checkpoint_hash_work(monkeypatch)

        assert publication.load_checkpoint(checkpoint)
        assert publication.load_checkpoint(checkpoint)
        assert work["calls"] == 6
        Path(next(iter(entry["curated_paths"]))).write_bytes(b"edited")
        assert publication.load_checkpoint(checkpoint) == {}
        assert work["calls"] == 9

    @pytest.mark.parametrize(
        "change", ["size", "mtime_ns", "ctime_ns", "inode", "symlink", "missing"]
    )
    def test_changed_shared_artifact_is_not_reused(self, tmp_path, monkeypatch, change):
        entry = _evidence(tmp_path, "common")
        document = Path(next(iter(entry["curated_paths"])))
        before = document.stat()
        original = publication.checkpoint_artifacts_valid
        checks = 0

        def validate_and_change(current, **kwargs):
            nonlocal checks
            valid = original(current, **kwargs)
            checks += 1
            if checks == 1:
                if change == "inode":
                    replacement = tmp_path / "replacement.pdf"
                    replacement.write_bytes(b"edited")
                    replacement.replace(document)
                    assert document.stat().st_ino != before.st_ino
                elif change in {"symlink", "missing"}:
                    document.unlink()
                    if change == "symlink":
                        replacement = tmp_path / "replacement.pdf"
                        replacement.write_bytes(b"report")
                        document.symlink_to(replacement)
                else:
                    document.write_bytes(b"longer edit" if change == "size" else b"edited")
                    if change == "ctime_ns":
                        os.utime(document, ns=(before.st_atime_ns, before.st_mtime_ns))
                        assert document.stat().st_size == before.st_size
                        assert document.stat().st_mtime_ns == before.st_mtime_ns
                        assert document.stat().st_ctime_ns != before.st_ctime_ns
                    elif change == "mtime_ns":
                        os.utime(document, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
                        assert document.stat().st_size == before.st_size
                        assert document.stat().st_mtime_ns != before.st_mtime_ns
                    else:
                        assert document.stat().st_size != before.st_size
            return valid

        monkeypatch.setattr(publication, "checkpoint_artifacts_valid", validate_and_change)
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_text(
            json.dumps({"version": 2, "entries": {"before": entry, "after": entry}})
        )

        assert set(publication.load_checkpoint(checkpoint)) == {"before"}

    def test_mutation_during_digest_fails_closed(self, tmp_path, monkeypatch):
        entry = _evidence(tmp_path, "common")
        document = Path(next(iter(entry["curated_paths"])))
        original = publication.artifact_sha256

        def digest_then_change(path):
            digest = original(path)
            if path == document:
                before = path.stat()
                path.write_bytes(b"edited")
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                assert path.stat().st_size == before.st_size
                assert path.stat().st_mtime_ns == before.st_mtime_ns
                assert path.stat().st_ctime_ns != before.st_ctime_ns
            return digest

        monkeypatch.setattr(publication, "artifact_sha256", digest_then_change)
        assert not publication.checkpoint_artifacts_valid(entry)

    def test_standalone_validation_shares_digest_across_evidence(self, tmp_path, monkeypatch):
        entry = _evidence(tmp_path, "common")
        entry["curated_paths"].update(entry["artifacts"])
        work = _count_checkpoint_hash_work(monkeypatch)

        assert publication.checkpoint_artifacts_valid(entry)
        assert work["calls"] == 3


def _evidence(tmp_path: Path, name: str) -> dict:
    manifest = tmp_path / f"{name}-manifest.json"
    index = tmp_path / f"{name}-index.csv"
    curated = tmp_path / f"{name}.pdf"
    manifest.write_text(json.dumps({"run_id": name}))
    index.write_text("relative_path,url\\nreport.pdf,https://agency.gov/report.pdf\\n")
    curated.write_bytes(b"report")
    return {
        "run_id": name,
        "artifacts": {str(path): artifact_sha256(path) for path in (manifest, index)},
        "curated_paths": {str(curated): artifact_sha256(curated)},
    }


class TestTargetId:
    def test_identity_is_partition_by_or_non_input_fields(self):
        target = {
            "label": "Aurora CO",
            "state": "co",
            "cost": "300",
            "urls": ["https://a.gov/1.pdf"],
        }
        assert identify_target(target, ["label", "state"]) == identify_target(
            {"state": "co", "label": "Aurora CO", "cost": "999"}, ["label", "state"]
        )
        assert identify_target(target) != identify_target({**target, "state": "oh"})
        assert identify_target(target) == identify_target({**target, "urls": [], "query": "x"})

    def test_id_is_short_path_safe_and_order_stable(self):
        key = identify_target({"x": "1", "y": "2"})
        assert len(key) == 12 and key.isalnum()
        assert key == identify_target({"y": "2", "x": "1"})


class TestCheckpointIO:
    def test_load_missing_returns_empty(self, tmp_path: Path):
        assert DiscoveryEngine._load_checkpoint(tmp_path / "none.json") == {}

    def test_save_then_load_roundtrip(self, tmp_path: Path):
        path = tmp_path / "checkpoint.json"
        entries = {"Aurora CO": {**_evidence(tmp_path, "r1"), "download_count": 2}}
        DiscoveryEngine._save_checkpoint_entries(path, entries)

        loaded = DiscoveryEngine._load_checkpoint(path)
        assert loaded["Aurora CO"]["download_count"] == 2

    def test_save_merges_without_clobbering(self, tmp_path: Path):
        path = tmp_path / "checkpoint.json"
        DiscoveryEngine._save_checkpoint_entries(path, {"Aurora CO": _evidence(tmp_path, "r1")})
        DiscoveryEngine._save_checkpoint_entries(
            path, {"Shaker Heights OH": _evidence(tmp_path, "r2")}
        )

        loaded = DiscoveryEngine._load_checkpoint(path)
        assert set(loaded) == {"Aurora CO", "Shaker Heights OH"}

    def test_corrupt_file_loads_as_empty(self, tmp_path: Path):
        path = tmp_path / "checkpoint.json"
        path.write_text("{ not valid json", encoding="utf-8")
        assert DiscoveryEngine._load_checkpoint(path) == {}

    @pytest.mark.parametrize("version", [None, True, "2", 3])
    def test_unknown_checkpoint_version_is_not_overwritten(self, tmp_path, version):
        path = tmp_path / "checkpoint.json"
        path.write_text(json.dumps({"version": version, "entries": {}}))
        before = path.read_bytes()
        assert DiscoveryEngine._load_checkpoint(path) == {}
        with pytest.raises(PublicationError, match="Invalid checkpoint version"):
            DiscoveryEngine._save_checkpoint_entries(path, {"target": {}})
        assert path.read_bytes() == before

    def test_invalid_fingerprint_fails_explicitly(self, tmp_path):
        with pytest.raises(PublicationError, match="fingerprint"):
            DiscoveryEngine._load_checkpoint(tmp_path / "checkpoint.json", fingerprint="")

    def test_v1_completions_are_invalidated(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        path.write_text(json.dumps({"version": 1, "entries": {"target": {"run_id": "old"}}}))
        assert DiscoveryEngine._load_checkpoint(path) == {}

    def test_effective_fingerprint_filters_entries(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        DiscoveryEngine._save_checkpoint_entries(
            path,
            {"target": _evidence(tmp_path, "r1")},
            fingerprint="settings-a",
        )
        assert "target" in DiscoveryEngine._load_checkpoint(path, fingerprint="settings-a")
        assert DiscoveryEngine._load_checkpoint(path, fingerprint="settings-b") == {}
        assert json.loads(path.read_text())["version"] == 2

    @pytest.mark.parametrize("artifact", ["manifest", "index", "curated"])
    def test_changed_or_missing_artifact_invalidates_completion(self, tmp_path, artifact):
        entry = _evidence(tmp_path, "r1")
        path = tmp_path / "checkpoint.json"
        DiscoveryEngine._save_checkpoint_entries(path, {"target": entry}, fingerprint="settings")
        output = (
            next(iter(entry["curated_paths"]))
            if artifact == "curated"
            else next(raw for raw in entry["artifacts"] if artifact in raw)
        )
        Path(output).write_bytes(b"changed")
        assert DiscoveryEngine._load_checkpoint(path, fingerprint="settings") == {}
        Path(output).unlink()
        assert DiscoveryEngine._load_checkpoint(path, fingerprint="settings") == {}

    def test_empty_completion_requires_manifest_index_evidence(self, tmp_path):
        entry = _evidence(tmp_path, "r1")
        entry["curated_paths"] = {}
        path = tmp_path / "checkpoint.json"
        DiscoveryEngine._save_checkpoint_entries(
            path,
            {"empty": entry, "no-evidence": {"run_id": "bad"}},
            fingerprint="settings",
        )
        assert set(DiscoveryEngine._load_checkpoint(path, fingerprint="settings")) == {"empty"}

    def test_corrupt_checkpoint_save_refuses_overwrite(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        path.write_text("{")
        with pytest.raises(PublicationError, match="Corrupt checkpoint"):
            DiscoveryEngine._save_checkpoint_entries(path, {"target": _evidence(tmp_path, "r1")})
        assert path.read_text() == "{"

    def test_incomplete_entries_are_prior_outcomes_not_completions(self, tmp_path):
        """Downloads outlive a settings change; other outcomes do not."""
        document = tmp_path / "a.pdf"
        document.write_bytes(b"a")
        downloaded = {
            "status": "downloaded",
            "final_url": "https://a.gov/a.pdf",
            "path": str(document),
            "sha256": artifact_sha256(document),
        }
        missing = {"status": "failed", "error": "404 Client Error"}
        path = tmp_path / "checkpoint.json"
        DiscoveryEngine._save_checkpoint_entries(
            path,
            {
                "done": _evidence(tmp_path, "r1"),
                "open": {
                    "incomplete": True,
                    "run_id": "r1",
                    "outcomes": {
                        "https://a.gov/a.pdf": downloaded,
                        "https://a.gov/gone.pdf": missing,
                        "https://a.gov/bad.pdf": {"status": "downloaded"},
                    },
                },
            },
            fingerprint="settings-a",
        )
        assert set(DiscoveryEngine._load_checkpoint(path, validate_artifacts=False)) == {"done"}
        assert load_prior_outcomes(path, fingerprint="settings-a") == {
            "open": {
                "https://a.gov/a.pdf": downloaded,
                "https://a.gov/gone.pdf": missing,
            }
        }
        assert load_prior_outcomes(path, fingerprint="settings-b") == {
            "open": {"https://a.gov/a.pdf": downloaded}
        }
        path.write_text("{")
        assert load_prior_outcomes(path, fingerprint="settings-a") == {}

    @pytest.mark.parametrize("entries", [[], {"key": []}, {"key": None}])
    def test_malformed_v2_entries_fail_closed(self, tmp_path, entries):
        path = tmp_path / "checkpoint.json"
        path.write_text(json.dumps({"version": 2, "entries": entries}))
        assert DiscoveryEngine._load_checkpoint(path) == {}
        with pytest.raises(PublicationError):
            DiscoveryEngine._save_checkpoint_entries(path, {"target": {}})


class TestEffectiveFingerprint:
    def test_order_stable_and_targets_are_separate_identity(self):
        assert effective_discovery_fingerprint(
            {
                "max_files": 2,
                "document_review": {"model": "fixture"},
                "targets": [{"label": "first"}],
            }
        ) == effective_discovery_fingerprint(
            {
                "targets": [{"label": "second"}],
                "document_review": {"model": "fixture"},
                "max_files": 2,
            }
        )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("max_files", 5),
            ("max_pages", 5),
            ("max_depth", 3),
            ("timeout_seconds", 10),
            ("selection_primary_per_target", 3),
            ("output_documents", Path("another-root")),
            ("document_review", {"model": "new-model"}),
            ("retention_documents", "none"),
        ],
    )
    def test_resource_policy_and_output_changes_invalidate(self, field, value):
        assert effective_discovery_fingerprint({field: None}) != effective_discovery_fingerprint(
            {field: value}
        )

    def test_models_do_not_invalidate_discovery_without_review(self):
        first = {"models": {"primary": "extract-a", "secondary": "locate-a"}}
        second = {"models": {"primary": "extract-b", "secondary": "locate-b"}}
        assert effective_discovery_fingerprint(first) == effective_discovery_fingerprint(second)

    def test_review_fingerprints_only_its_resolved_model(self):
        first = {
            "document_review": {"document_description": "records", "model": "secondary"},
            "models": {"primary": "extract-a", "secondary": "review-a"},
        }
        assert effective_discovery_fingerprint(first) == effective_discovery_fingerprint(
            {**first, "models": {"primary": "extract-b", "secondary": "review-a"}}
        )
        assert effective_discovery_fingerprint(first) != effective_discovery_fingerprint(
            {**first, "models": {"primary": "extract-a", "secondary": "review-b"}}
        )

    def test_escalation_model_alias_invalidates_discovery(self):
        first = {
            "document_review": {"model": "secondary", "escalate_model": "primary"},
            "models": {"secondary": "cheap", "primary": "strong-a"},
        }
        second = {**first, "models": {"secondary": "cheap", "primary": "strong-b"}}
        assert effective_discovery_fingerprint(first) != effective_discovery_fingerprint(second)

    @pytest.mark.parametrize("value", [object(), float("nan"), float("inf"), {1: "invalid-key"}])
    def test_invalid_effective_configuration_is_not_stringified(self, value):
        with pytest.raises(PublicationError):
            effective_discovery_fingerprint({"document_review": value})
