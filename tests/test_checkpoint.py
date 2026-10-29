"""Tests for discovery checkpointing: key generation and load/save/resume.

Large runs must resume without redoing completed targets. Completed target
keys are persisted to checkpoint.json and pruned from request.targets before
discovery runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from psweep.discovery.engine import DiscoveryEngine
from psweep.discovery.publication import (
    PublicationError,
    artifact_sha256,
    effective_discovery_fingerprint,
)


def _evidence(tmp_path: Path, name: str) -> dict:
    manifest = tmp_path / f"{name}-manifest.json"
    index = tmp_path / f"{name}-index.csv"
    curated = tmp_path / f"{name}.pdf"
    manifest.write_text(json.dumps({"run_id": name}))
    index.write_text(
        "relative_path,url\\nreport.pdf,https://agency.gov/report.pdf\\n"
    )
    curated.write_bytes(b"report")
    return {
        "run_id": name,
        "artifacts": {
            str(path): artifact_sha256(path) for path in (manifest, index)
        },
        "curated_paths": {str(curated): artifact_sha256(curated)},
    }


class TestCheckpointKey:
    def test_label_only_target_is_its_complete_identity(self):
        assert (
            DiscoveryEngine._target_checkpoint_key({"label": "Aurora CO"})
            == "Aurora CO"
        )

    def test_label_does_not_discard_other_identity_fields(self):
        key = DiscoveryEngine._target_checkpoint_key(
            {"label": "Aurora CO", "state": "co"}
        )
        assert key != DiscoveryEngine._target_checkpoint_key(
            {"label": "Aurora CO", "state": "oh"}
        )

    def test_hash_fallback_when_no_label(self):
        key = DiscoveryEngine._target_checkpoint_key(
            {"state": "co", "utility_name": "Xcel"}
        )
        assert len(key) == 12
        assert key.isalnum()

    def test_hash_is_stable_across_key_order(self):
        a = DiscoveryEngine._target_checkpoint_key({"x": "1", "y": "2"})
        b = DiscoveryEngine._target_checkpoint_key({"y": "2", "x": "1"})
        assert a == b

    def test_different_targets_get_different_hashes(self):
        a = DiscoveryEngine._target_checkpoint_key({"state": "co"})
        b = DiscoveryEngine._target_checkpoint_key({"state": "oh"})
        assert a != b


class TestCheckpointIO:
    def test_load_missing_returns_empty(self, tmp_path: Path):
        assert DiscoveryEngine._load_checkpoint(tmp_path / "none.json") == {}

    def test_save_then_load_roundtrip(self, tmp_path: Path):
        path = tmp_path / "checkpoint.json"
        entries = {
            "Aurora CO": {**_evidence(tmp_path, "r1"), "download_count": 2}
        }
        DiscoveryEngine._save_checkpoint_entries(path, entries)

        loaded = DiscoveryEngine._load_checkpoint(path)
        assert loaded["Aurora CO"]["download_count"] == 2

    def test_save_merges_without_clobbering(self, tmp_path: Path):
        path = tmp_path / "checkpoint.json"
        DiscoveryEngine._save_checkpoint_entries(
            path, {"Aurora CO": _evidence(tmp_path, "r1")}
        )
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
    def test_unknown_checkpoint_version_is_not_overwritten(
        self, tmp_path, version
    ):
        path = tmp_path / "checkpoint.json"
        path.write_text(json.dumps({"version": version, "entries": {}}))
        before = path.read_bytes()
        assert DiscoveryEngine._load_checkpoint(path) == {}
        with pytest.raises(
            PublicationError, match="Invalid checkpoint version"
        ):
            DiscoveryEngine._save_checkpoint_entries(path, {"target": {}})
        assert path.read_bytes() == before

    def test_invalid_fingerprint_fails_explicitly(self, tmp_path):
        with pytest.raises(PublicationError, match="fingerprint"):
            DiscoveryEngine._load_checkpoint(
                tmp_path / "checkpoint.json", fingerprint=""
            )

    def test_v1_completions_are_invalidated(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        path.write_text(
            json.dumps(
                {"version": 1, "entries": {"target": {"run_id": "old"}}}
            )
        )
        assert DiscoveryEngine._load_checkpoint(path) == {}

    def test_effective_fingerprint_filters_entries(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        DiscoveryEngine._save_checkpoint_entries(
            path,
            {"target": _evidence(tmp_path, "r1")},
            fingerprint="settings-a",
        )
        assert "target" in DiscoveryEngine._load_checkpoint(
            path, fingerprint="settings-a"
        )
        assert (
            DiscoveryEngine._load_checkpoint(path, fingerprint="settings-b")
            == {}
        )
        assert json.loads(path.read_text())["version"] == 2

    @pytest.mark.parametrize("artifact", ["manifest", "index", "curated"])
    def test_changed_or_missing_artifact_invalidates_completion(
        self, tmp_path, artifact
    ):
        entry = _evidence(tmp_path, "r1")
        path = tmp_path / "checkpoint.json"
        DiscoveryEngine._save_checkpoint_entries(
            path, {"target": entry}, fingerprint="settings"
        )
        output = (
            next(iter(entry["curated_paths"]))
            if artifact == "curated"
            else next(raw for raw in entry["artifacts"] if artifact in raw)
        )
        Path(output).write_bytes(b"changed")
        assert (
            DiscoveryEngine._load_checkpoint(path, fingerprint="settings")
            == {}
        )
        Path(output).unlink()
        assert (
            DiscoveryEngine._load_checkpoint(path, fingerprint="settings")
            == {}
        )

    def test_empty_completion_requires_manifest_index_evidence(self, tmp_path):
        entry = _evidence(tmp_path, "r1")
        entry["curated_paths"] = {}
        path = tmp_path / "checkpoint.json"
        DiscoveryEngine._save_checkpoint_entries(
            path,
            {"empty": entry, "no-evidence": {"run_id": "bad"}},
            fingerprint="settings",
        )
        assert set(
            DiscoveryEngine._load_checkpoint(path, fingerprint="settings")
        ) == {"empty"}

    def test_corrupt_checkpoint_save_refuses_overwrite(self, tmp_path):
        path = tmp_path / "checkpoint.json"
        path.write_text("{")
        with pytest.raises(PublicationError, match="Corrupt checkpoint"):
            DiscoveryEngine._save_checkpoint_entries(
                path, {"target": _evidence(tmp_path, "r1")}
            )
        assert path.read_text() == "{"

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
        assert effective_discovery_fingerprint(
            {field: None}
        ) != effective_discovery_fingerprint({field: value})

    @pytest.mark.parametrize(
        "value", [object(), float("nan"), float("inf"), {1: "invalid-key"}]
    )
    def test_invalid_effective_configuration_is_not_stringified(self, value):
        with pytest.raises(PublicationError):
            effective_discovery_fingerprint({"document_review": value})
