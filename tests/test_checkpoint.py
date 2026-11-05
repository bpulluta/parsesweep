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
from psweep.discovery.targets import identify_target
from psweep.discovery.publication import (
    PublicationError,
    artifact_sha256,
    effective_discovery_fingerprint,
    load_prior_outcomes,
)


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

    @pytest.mark.parametrize("value", [object(), float("nan"), float("inf"), {1: "invalid-key"}])
    def test_invalid_effective_configuration_is_not_stringified(self, value):
        with pytest.raises(PublicationError):
            effective_discovery_fingerprint({"document_review": value})
