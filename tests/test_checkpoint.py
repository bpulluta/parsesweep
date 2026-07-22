"""Tests for acquisition checkpointing: key generation and load/save/resume.

Large runs must resume without redoing completed targets. Completed target
keys are persisted to checkpoint.json and pruned from request.targets before
discovery runs.
"""

from __future__ import annotations

from pathlib import Path

from psweep.acquisition.engine import AcquisitionEngine


class TestCheckpointKey:
    def test_label_is_used_when_present(self):
        key = AcquisitionEngine._target_checkpoint_key(
            {"label": "Aurora CO", "state": "co"}
        )
        assert key == "Aurora CO"

    def test_hash_fallback_when_no_label(self):
        key = AcquisitionEngine._target_checkpoint_key(
            {"state": "co", "utility_name": "Xcel"}
        )
        assert len(key) == 12
        assert key.isalnum()

    def test_hash_is_stable_across_key_order(self):
        a = AcquisitionEngine._target_checkpoint_key({"x": "1", "y": "2"})
        b = AcquisitionEngine._target_checkpoint_key({"y": "2", "x": "1"})
        assert a == b

    def test_different_targets_get_different_hashes(self):
        a = AcquisitionEngine._target_checkpoint_key({"state": "co"})
        b = AcquisitionEngine._target_checkpoint_key({"state": "oh"})
        assert a != b


class TestCheckpointIO:
    def test_load_missing_returns_empty(self, tmp_path: Path):
        assert AcquisitionEngine._load_checkpoint(tmp_path / "none.json") == {}

    def test_save_then_load_roundtrip(self, tmp_path: Path):
        path = tmp_path / "checkpoint.json"
        entries = {"Aurora CO": {"run_id": "r1", "download_count": 2}}
        AcquisitionEngine._save_checkpoint_entries(path, entries)

        loaded = AcquisitionEngine._load_checkpoint(path)
        assert loaded["Aurora CO"]["download_count"] == 2

    def test_save_merges_without_clobbering(self, tmp_path: Path):
        path = tmp_path / "checkpoint.json"
        AcquisitionEngine._save_checkpoint_entries(
            path, {"Aurora CO": {"run_id": "r1"}}
        )
        AcquisitionEngine._save_checkpoint_entries(
            path, {"Shaker Heights OH": {"run_id": "r2"}}
        )

        loaded = AcquisitionEngine._load_checkpoint(path)
        assert set(loaded) == {"Aurora CO", "Shaker Heights OH"}

    def test_corrupt_file_loads_as_empty(self, tmp_path: Path):
        path = tmp_path / "checkpoint.json"
        path.write_text("{ not valid json", encoding="utf-8")
        assert AcquisitionEngine._load_checkpoint(path) == {}
