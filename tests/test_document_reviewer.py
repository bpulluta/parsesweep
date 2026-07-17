"""Tests for the LLM document reviewer (grading orchestration + promotion).

The live LLM call is stubbed; these verify per-target ranking, promotion into
a ``reviewed/`` subfolder (move mode), flag-only mode, and graceful skips.
"""

from __future__ import annotations

from pathlib import Path

from streamline_extract.acquisition.document_reviewer import DocumentReviewer


def _record(tmp_path: Path, name: str, label: str) -> dict:
    path = tmp_path / name
    path.write_text("dummy", encoding="utf-8")
    return {
        "status": "downloaded",
        "path": path.as_posix(),
        "relative_path": name,
        "target_metadata": {"label": label},
        "target_label": label,
    }


def _stub_grades(reviewer: DocumentReviewer, grades: dict[str, dict]) -> None:
    """Replace the LLM grade call with canned results keyed by filename."""
    reviewer._ensure_client = lambda: object()  # type: ignore[method-assign]
    reviewer._grade = lambda p: grades.get(Path(p).name)  # type: ignore[method-assign]


class TestReviewPromotion:
    def test_primary_moved_to_reviewed_subfolder(self, tmp_path: Path):
        ordinance = _record(tmp_path, "ordinance.pdf", "County A")
        deck = _record(tmp_path, "slides.pdf", "County A")
        reviewer = DocumentReviewer(
            document_description="the enacted ordinance", keep_top=1
        )
        _stub_grades(
            reviewer,
            {
                "ordinance.pdf": {
                    "is_primary": True,
                    "relevance": 0.95,
                    "doc_kind": "ordinance",
                    "reason": "enacted code",
                },
                "slides.pdf": {
                    "is_primary": False,
                    "relevance": 0.2,
                    "doc_kind": "presentation",
                    "reason": "deck",
                },
            },
        )
        downloads, notes = reviewer.review([ordinance, deck], [])

        assert ordinance["review_selected"] is True
        assert "reviewed/ordinance.pdf" in ordinance["path"]
        assert (tmp_path / "reviewed" / "ordinance.pdf").exists()
        # non-primary stays put
        assert deck.get("review_selected") is False
        assert (tmp_path / "slides.pdf").exists()
        assert any("Document review" in n for n in notes)

    def test_per_target_promotion_is_independent(self, tmp_path: Path):
        a = _record(tmp_path, "a_ord.pdf", "County A")
        b = _record(tmp_path, "b_ord.pdf", "County B")
        reviewer = DocumentReviewer(
            document_description="the ordinance", keep_top=1
        )
        _stub_grades(
            reviewer,
            {
                "a_ord.pdf": {"is_primary": True, "relevance": 0.9, "doc_kind": "ordinance", "reason": "x"},
                "b_ord.pdf": {"is_primary": True, "relevance": 0.8, "doc_kind": "ordinance", "reason": "y"},
            },
        )
        reviewer.review([a, b], [])
        # each target keeps its own primary
        assert a["review_selected"] is True
        assert b["review_selected"] is True


class TestReviewFlagMode:
    def test_flag_mode_does_not_move(self, tmp_path: Path):
        rec = _record(tmp_path, "ord.pdf", "County A")
        reviewer = DocumentReviewer(
            document_description="the ordinance", keep_top=1, action="flag"
        )
        _stub_grades(
            reviewer,
            {"ord.pdf": {"is_primary": True, "relevance": 0.9, "doc_kind": "ordinance", "reason": "x"}},
        )
        reviewer.review([rec], [])
        assert rec["review_selected"] is True
        assert (tmp_path / "ord.pdf").exists()  # not moved
        assert "reviewed" not in rec["path"]


class TestReviewSkips:
    def test_no_downloaded_files_is_noop(self):
        reviewer = DocumentReviewer(document_description="x")
        downloads = [{"status": "failed", "path": None}]
        out, notes = reviewer.review(downloads, [])
        assert out == downloads
