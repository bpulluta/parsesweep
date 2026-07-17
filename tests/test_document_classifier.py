"""Tests for the post-download document classifier wiring in the engine.

The classifier reads downloaded files via ContentSampler and annotates each
record with classification_passed / classification_score. In 'filter' mode a
failing record's status becomes 'rejected_classifier'; in 'warn' mode it is
only annotated.
"""

from __future__ import annotations

from pathlib import Path

from streamline_extract.acquisition.engine import AcquisitionEngine


def _write_doc(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _record(path: str) -> dict[str, object]:
    return {"status": "downloaded", "path": path, "url": f"file://{path}"}


class TestClassifierWarnMode:
    def test_passing_doc_is_annotated_and_kept(self, tmp_path: Path):
        path = _write_doc(
            tmp_path,
            "ord.txt",
            "This ordinance establishes geothermal permitting rules. " * 5,
        )
        cfg = {"required_keywords": ["ordinance"], "min_required_matches": 1}

        downloads, notes = AcquisitionEngine._run_post_download_classifier(
            [_record(path)], cfg, []
        )

        assert downloads[0]["classification_passed"] is True
        assert downloads[0]["status"] == "downloaded"
        assert any("classifier" in n.lower() for n in notes)

    def test_failing_doc_is_annotated_but_kept_in_warn(self, tmp_path: Path):
        path = _write_doc(
            tmp_path,
            "minutes.txt",
            "Meeting minutes of the board. Attendance and roll call. " * 5,
        )
        cfg = {
            "required_keywords": ["ordinance"],
            "min_required_matches": 1,
            "action": "warn",
        }

        downloads, _ = AcquisitionEngine._run_post_download_classifier(
            [_record(path)], cfg, []
        )

        assert downloads[0]["classification_passed"] is False
        assert downloads[0]["status"] == "downloaded"  # warn keeps it


class TestClassifierFilterMode:
    def test_failing_doc_is_rejected_in_filter(self, tmp_path: Path):
        path = _write_doc(
            tmp_path,
            "paper.txt",
            "Research paper on subsurface thermal gradients. " * 5,
        )
        cfg = {
            "required_keywords": ["ordinance"],
            "min_required_matches": 1,
            "action": "filter",
        }

        downloads, _ = AcquisitionEngine._run_post_download_classifier(
            [_record(path)], cfg, []
        )

        assert downloads[0]["classification_passed"] is False
        assert downloads[0]["status"] == "rejected_classifier"


class TestClassifierEdgeCases:
    def test_non_downloaded_records_are_skipped(self, tmp_path: Path):
        rec = {"status": "failed", "path": None, "url": "http://x"}
        downloads, _ = AcquisitionEngine._run_post_download_classifier(
            [rec], {"required_keywords": ["ordinance"]}, []
        )
        assert "classification_passed" not in downloads[0]

    def test_missing_file_records_error_not_crash(self, tmp_path: Path):
        rec = _record(str(tmp_path / "does_not_exist.pdf"))
        downloads, _ = AcquisitionEngine._run_post_download_classifier(
            [rec], {"required_keywords": ["ordinance"]}, []
        )
        # Either annotated as failed or carries a classification_error; never raises.
        assert (
            downloads[0].get("classification_passed") is False
            or "classification_error" in downloads[0]
        )
