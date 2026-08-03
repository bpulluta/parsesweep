"""Tests for the LLM document reviewer (grading orchestration + selection).

The live LLM call is stubbed; these verify per-target ranking, in-place
selection (files are NOT moved — the engine materializes curated/ separately),
per-file ``.review`` sidecars, and graceful skips.
"""

from __future__ import annotations

import json
from pathlib import Path

from psweep.discovery.document_reviewer import DocumentReviewer


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
    reviewer._grade = lambda p, **kw: grades.get(Path(p).name)  # type: ignore[method-assign]


class TestReviewSelection:
    def test_primary_selected_in_place_with_sidecar(self, tmp_path: Path):
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
        # Files are NOT moved — they stay in place; curated/ is built by engine.
        assert ordinance["path"] == (tmp_path / "ordinance.pdf").as_posix()
        assert (tmp_path / "ordinance.pdf").exists()
        assert "reviewed" not in ordinance["path"]
        # A per-file review sidecar captures the verdict for human review.
        sidecar = tmp_path / ".review" / "ordinance.json"
        assert sidecar.exists()
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["llm"]["is_primary"] is True
        assert payload["llm"]["selected"] is True
        assert payload["human"]["decision"] is None
        # non-primary stays put and is not selected
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

    def test_filename_variants_are_deduplicated(self, tmp_path: Path):
        a = _record(
            tmp_path,
            "PUCO-13-Schedule-of-Rates-for-Electric-Service.pdf",
            "County A",
        )
        b = _record(
            tmp_path,
            "PUCO-13-Scheduleof-Rates-for-Electric-Service.pdf",
            "County A",
        )
        reviewer = DocumentReviewer(
            document_description="the electric tariff book",
            keep_top=2,
            dedup_key_fields=["path_basename", "review_doc_kind"],
        )
        _stub_grades(
            reviewer,
            {
                "PUCO-13-Schedule-of-Rates-for-Electric-Service.pdf": {
                    "is_primary": True,
                    "relevance": 0.95,
                    "doc_kind": "electric tariff book",
                    "reason": "official tariff",
                },
                "PUCO-13-Scheduleof-Rates-for-Electric-Service.pdf": {
                    "is_primary": True,
                    "relevance": 0.94,
                    "doc_kind": "electric tariff book",
                    "reason": "official tariff",
                },
            },
        )
        reviewer.review([a, b], [])

        assert {a["review_selected"], b["review_selected"]} == {True, False}
        assert a.get("review_redundant") or b.get("review_redundant")


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


class TestReviewGradeCache:
    def test_second_run_reuses_cached_grade_without_llm(self, tmp_path: Path):
        rec = _record(tmp_path, "ordinance.pdf", "County A")
        grades = {
            "ordinance.pdf": {
                "is_primary": True,
                "relevance": 0.95,
                "doc_kind": "ordinance",
                "reason": "enacted code",
            }
        }

        # First run: grades via the (stubbed) LLM and writes a sidecar w/ cache_key.
        r1 = DocumentReviewer(document_description="the enacted ordinance")
        _stub_grades(r1, grades)
        _, notes1 = r1.review([dict(rec)], [])
        assert any("reused from cache" in n for n in notes1)
        sidecar = tmp_path / ".review" / "ordinance.json"
        assert "cache_key" in json.loads(sidecar.read_text(encoding="utf-8"))

        # Second run: file unchanged → grade reused, LLM must NOT be called.
        r2 = DocumentReviewer(document_description="the enacted ordinance")

        def _boom(_p):  # pragma: no cover - asserts no LLM call on cache hit
            raise AssertionError("LLM grade should not be called on cache hit")

        r2._ensure_client = lambda: object()  # type: ignore[method-assign]
        r2._grade = _boom  # type: ignore[method-assign]
        rec2 = dict(rec)
        _, notes2 = r2.review([rec2], [])
        assert rec2["review_relevance"] == 0.95
        assert rec2.get("review_cached") is True
        assert any("1 reused from cache" in n for n in notes2)

    def test_changed_description_invalidates_cache(self, tmp_path: Path):
        rec = _record(tmp_path, "ordinance.pdf", "County A")
        grades = {
            "ordinance.pdf": {
                "is_primary": True, "relevance": 0.9,
                "doc_kind": "ordinance", "reason": "x",
            }
        }
        r1 = DocumentReviewer(document_description="desc one")
        _stub_grades(r1, grades)
        r1.review([dict(rec)], [])

        # Different description → different cache key → must re-grade.
        r2 = DocumentReviewer(document_description="a different description")
        _stub_grades(r2, grades)
        rec2 = dict(rec)
        _, notes2 = r2.review([rec2], [])
        assert rec2.get("review_cached") is not True
        assert any("graded 1 file" in n for n in notes2)
