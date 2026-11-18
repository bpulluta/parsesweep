"""Tests for the LLM document reviewer (grading orchestration + selection).

The live LLM call is stubbed; these verify per-target ranking, in-place
selection (files are NOT moved — the engine materializes curated/ separately),
the content-addressed grade cache, and graceful skips.
"""

from __future__ import annotations

import json
from pathlib import Path

from discovery_helpers import MeteredReviewClient
import pytest

from psweep.extraction import document_utils
from psweep.discovery import document_reviewer
from psweep.discovery.document_reviewer import DocumentReviewer
from psweep.discovery.publication import record_is_curated
from psweep.extraction import llm_factory


@pytest.fixture(autouse=True)
def _fake_model_resolution(monkeypatch):
    """Keep reviewer tests independent of credentials and ambient config."""
    monkeypatch.setattr(
        llm_factory,
        "resolve_llm_kwargs",
        lambda *_args, **_kwargs: {"model": "fixture", "provider": "fixture"},
    )


def _cached_grades(cache: Path) -> list[Path]:
    """Cache entries that hold a grade; a terminal negative entry never counts."""
    return [p for p in cache.glob("*.json") if "llm" in json.loads(p.read_text())]


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


def _evidence(quote: str) -> dict:
    return {
        "identity_quote": quote,
        "content_quote": quote,
        "scope": {"value": "fixture scope", "quote": quote},
        "claim_kind": {"value": "direct qualified claim", "quote": quote},
        "source_date": None,
        "coverage": {},
    }


def _stub_grades(reviewer: DocumentReviewer, grades: dict[str, dict]) -> None:
    """Replace grading with intentional complete-contract orchestration fixtures."""
    for grade in grades.values():
        grade["evidence"] = _evidence("dummy")
    reviewer._document_text = lambda _path: "dummy " * 100
    reviewer._ensure_client = object  # type: ignore[method-assign]
    reviewer._grade = lambda p, **kw: grades.get(Path(p).name)  # type: ignore[method-assign]


def test_seed_only_review_preserves_shared_cap(tmp_path):
    """Seed-only files share the configured selection cap."""
    records = [_record(tmp_path, name, "unused") for name in ("one.pdf", "two.pdf")]
    for record in records:
        record.pop("target_metadata")
        record.pop("target_label")
    reviewer = DocumentReviewer(
        document_description="records", keep_top=1, deduplicate_redundant=False
    )
    _stub_grades(
        reviewer,
        {
            name: {"is_primary": True, "relevance": 0.9, "doc_kind": "record", "reason": "fixture"}
            for name in ("one.pdf", "two.pdf")
        },
    )
    reviewer.review(records, [])
    assert sum(record["review_selected"] for record in records) == 1


def test_malformed_cache_shape_is_a_miss(tmp_path):
    """An array-shaped grade cache is rejected and regraded."""
    record = _record(tmp_path, "one.pdf", "target")
    reviewer = DocumentReviewer(
        document_description="records", cache_dir=tmp_path / ".review_cache"
    )
    cache = tmp_path / ".review_cache" / f"{reviewer._cache_key(record['path'])}.json"
    cache.parent.mkdir()
    cache.write_text("[]")
    _stub_grades(
        reviewer,
        {
            "one.pdf": {
                "is_primary": True,
                "relevance": 0.9,
                "doc_kind": "record",
                "reason": "fixture",
            }
        },
    )
    reviewer.review([record], [])
    assert record["review_selected"] and not record["review_cached"]


class TestReviewSelection:
    def test_primary_selected_in_place(self, tmp_path: Path):
        ordinance = _record(tmp_path, "ordinance.pdf", "County A")
        deck = _record(tmp_path, "slides.pdf", "County A")
        reviewer = DocumentReviewer(document_description="the enacted ordinance", keep_top=1)
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
        _downloads, notes = reviewer.review([ordinance, deck], [])

        assert ordinance["review_selected"] is True
        # Files are NOT moved — they stay in place; curated/ is built by engine.
        assert ordinance["path"] == (tmp_path / "ordinance.pdf").as_posix()
        assert (tmp_path / "ordinance.pdf").exists()
        assert "reviewed" not in ordinance["path"]
        # non-primary stays put and is not selected
        assert deck.get("review_selected") is False
        assert (tmp_path / "slides.pdf").exists()
        assert any("Document review" in n for n in notes)

    def test_per_target_promotion_is_independent(self, tmp_path: Path):
        a = _record(tmp_path, "a_ord.pdf", "County A")
        b = _record(tmp_path, "b_ord.pdf", "County B")
        reviewer = DocumentReviewer(document_description="the ordinance", keep_top=1)
        _stub_grades(
            reviewer,
            {
                "a_ord.pdf": {
                    "is_primary": True,
                    "relevance": 0.9,
                    "doc_kind": "ordinance",
                    "reason": "x",
                },
                "b_ord.pdf": {
                    "is_primary": True,
                    "relevance": 0.8,
                    "doc_kind": "ordinance",
                    "reason": "y",
                },
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
    def test_review_never_moves_original_documents(self, tmp_path: Path):
        rec = _record(tmp_path, "ord.pdf", "County A")
        reviewer = DocumentReviewer(document_description="the ordinance", keep_top=1)
        _stub_grades(
            reviewer,
            {
                "ord.pdf": {
                    "is_primary": True,
                    "relevance": 0.9,
                    "doc_kind": "ordinance",
                    "reason": "x",
                }
            },
        )
        reviewer.review([rec], [])
        assert rec["review_selected"] is True
        assert (tmp_path / "ord.pdf").exists()  # not moved
        assert "reviewed" not in rec["path"]


class TestReviewSkips:
    def test_no_downloaded_files_is_noop(self):
        reviewer = DocumentReviewer(document_description="x")
        downloads = [{"status": "failed", "path": None}]
        out, _notes = reviewer.review(downloads, [])
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

        cache = tmp_path / ".review_cache"
        r1 = DocumentReviewer(document_description="the enacted ordinance", cache_dir=cache)
        _stub_grades(r1, grades)
        _, notes1 = r1.review([dict(rec)], [])
        assert any("0 reused from cache" in n for n in notes1)
        assert len(list(cache.glob("*.json"))) == 1

        # Second run: file unchanged → grade reused, LLM must NOT be called.
        r2 = DocumentReviewer(document_description="the enacted ordinance", cache_dir=cache)

        def _boom(_p):  # pragma: no cover - asserts no LLM call on cache hit
            raise AssertionError("LLM grade should not be called on cache hit")

        r2._document_text = lambda _path: "dummy " * 100
        r2._ensure_client = object  # type: ignore[method-assign]
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
                "is_primary": True,
                "relevance": 0.9,
                "doc_kind": "ordinance",
                "reason": "x",
            }
        }
        cache = tmp_path / ".review_cache"
        r1 = DocumentReviewer(document_description="desc one", cache_dir=cache)
        _stub_grades(r1, grades)
        r1.review([dict(rec)], [])

        # Different description → different cache key → must re-grade.
        r2 = DocumentReviewer(document_description="a different description", cache_dir=cache)
        _stub_grades(r2, grades)
        rec2 = dict(rec)
        _, notes2 = r2.review([rec2], [])
        assert rec2.get("review_cached") is not True
        assert any("graded 1 file" in n for n in notes2)

    def test_changed_source_url_invalidates_cache(self, tmp_path: Path):
        rec = _record(tmp_path, "comment.pdf", "Project A")
        rec["url"] = "https://mirror.example.org/comment.pdf"
        grades = {
            "comment.pdf": {
                "evidence": _evidence("dummy"),
                "is_primary": True,
                "relevance": 0.9,
                "doc_kind": "comment",
                "reason": "x",
            }
        }
        cache = tmp_path / ".review_cache"
        r1 = DocumentReviewer(document_description="public comments", cache_dir=cache)
        _stub_grades(r1, grades)
        r1.review([dict(rec)], [])

        # Same file bytes/mtime, but different download provenance → re-grade.
        r2 = DocumentReviewer(document_description="public comments", cache_dir=cache)
        seen: list[str] = []

        def _grade(p, **kw):
            seen.append(kw.get("source_url", ""))
            return grades[Path(p).name]

        r2._document_text = lambda _path: "dummy " * 100
        r2._ensure_client = object  # type: ignore[method-assign]
        r2._grade = _grade  # type: ignore[method-assign]
        rec2 = dict(rec)
        rec2["final_url"] = "https://puc.sd.gov/comments/comment.pdf"
        _, notes2 = r2.review([rec2], [])
        assert rec2.get("review_cached") is not True
        assert seen == ["https://puc.sd.gov/comments/comment.pdf"]
        assert any("graded 1 file" in n for n in notes2)

        # Unchanged provenance on a third run → cache hit.
        r3 = DocumentReviewer(document_description="public comments", cache_dir=cache)
        _stub_grades(r3, {})
        rec3 = dict(rec2)
        r3.review([rec3], [])
        assert rec3.get("review_cached") is True

    def test_url_less_record_cache_key_unchanged(self, tmp_path: Path):
        rec = _record(tmp_path, "ordinance.pdf", "County A")
        reviewer = DocumentReviewer(document_description="desc")
        assert reviewer._cache_key(rec["path"]) == reviewer._cache_key(rec["path"], source_url="")
        assert reviewer._cache_key(rec["path"]) != reviewer._cache_key(
            rec["path"], source_url="https://dps.ny.gov/a.pdf"
        )


class _CapturingClient(MeteredReviewClient):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def respond(self, *, text, schema, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        return {
            "data": {
                "doc_kind": "comment",
                "reason": "ok",
                "grade": 4,
                "evidence": _evidence(" ".join(text.split()[:3])),
            }
        }


class TestReviewSourceUrlContext:
    """The grader must see the record's actual download URL (provenance)."""

    def _review_one(self, tmp_path: Path, monkeypatch, **urls) -> str:
        monkeypatch.setattr(
            document_utils,
            "extract_text_from_document",
            lambda _p: "public comment " * 50,
        )
        rec = _record(tmp_path, "Bury.pdf", "Crowned Ridge Wind")
        rec.update(urls)
        client = _CapturingClient()
        reviewer = DocumentReviewer(document_description="public comments")
        reviewer._client = client
        reviewer.review([rec], [])
        assert len(client.prompts) == 1
        return client.prompts[0]

    def test_final_url_preferred_over_original(self, tmp_path: Path, monkeypatch):
        prompt = self._review_one(
            tmp_path,
            monkeypatch,
            url="https://short.example.com/x",
            final_url="https://puc.sd.gov/commission/dockets/Bury.pdf",
        )
        final = "https://puc.sd.gov/commission/dockets/Bury.pdf"
        assert f"SOURCE_URL: {final}" in prompt
        assert "short.example.com" not in prompt

    def test_original_url_used_when_final_missing(self, tmp_path: Path, monkeypatch):
        prompt = self._review_one(tmp_path, monkeypatch, url="https://dps.ny.gov/ViewDoc-4.pdf")
        assert "SOURCE_URL: https://dps.ny.gov/ViewDoc-4.pdf" in prompt

    def test_missing_url_is_not_fabricated(self, tmp_path: Path, monkeypatch):
        prompt = self._review_one(tmp_path, monkeypatch)
        assert "SOURCE_URL" not in prompt


class TestReviewSampleWindowing:
    """Lock the large-document sampler: deterministic + keyword-biased.

    Phase 3B — on very large PDFs the review sample must (a) be byte-identical
    across runs so the same file always scores the same, and (b) prefer windows
    containing the domain's operative keywords (sourced from config) so relevant
    content deep in a long document reaches the grader. Keywords are supplied by
    the caller (``review_keywords``); no domain vocabulary lives in this module.
    """

    # Marker sits at position 30000 of a 60000-char body. With max_chars=2000
    # the distributed body windows land at offsets ~391/20065/39739/59413, so
    # 30000 falls in a sampling gap — the no-keyword sampler cannot reach it,
    # which is exactly what the keyword bias is there to fix.
    _MAX_CHARS = 2000
    _N = 60000
    _POS = 30000
    _MARKER = "Compressor SENTINEL_TOKEN "

    def _large_text(self) -> str:
        prefix = "x" * self._POS
        suffix = "x" * (self._N - self._POS - len(self._MARKER))
        return prefix + self._MARKER + suffix

    def test_short_document_returns_full_text_unchanged(self):
        reviewer = DocumentReviewer(
            document_description="x", max_chars=100, review_keywords=["compressor"]
        )
        text = "a short ordinance about a compressor station"
        assert reviewer._build_review_sample(text) == text

    def test_keyword_window_is_included_when_deep_in_body(self):
        reviewer = DocumentReviewer(
            document_description="x",
            max_chars=self._MAX_CHARS,
            review_keywords=["compressor"],  # matched case-insensitively
        )
        sample = reviewer._build_review_sample(self._large_text())
        # The keyword-adjacent content deep in the body is pulled in...
        assert "SENTINEL_TOKEN" in sample
        # ...and the bounded sample never exceeds the configured budget.
        assert len(sample) <= self._MAX_CHARS

    def test_no_keyword_sampler_misses_the_deep_marker(self):
        # Same input, no keywords configured -> the deep-body marker falls in a
        # gap between the evenly-spaced body windows and is NOT sampled. This is
        # the recall failure Phase 3B's keyword bias exists to prevent.
        reviewer = DocumentReviewer(
            document_description="x", max_chars=self._MAX_CHARS, review_keywords=[]
        )
        sample = reviewer._build_review_sample(self._large_text())
        assert "SENTINEL_TOKEN" not in sample

    def test_sampling_is_deterministic_across_calls(self):
        text = self._large_text()
        with_kw = DocumentReviewer(
            document_description="x",
            max_chars=self._MAX_CHARS,
            review_keywords=["compressor", "setback"],
        )
        without_kw = DocumentReviewer(
            document_description="x", max_chars=self._MAX_CHARS, review_keywords=[]
        )
        # Repeated calls on the same input are byte-identical (no RNG, no clock).
        assert with_kw._build_review_sample(text) == with_kw._build_review_sample(text)
        assert without_kw._build_review_sample(text) == without_kw._build_review_sample(text)
        # And the two configurations genuinely diverge — the bias changes output.
        assert with_kw._build_review_sample(text) != without_kw._build_review_sample(text)


class TestByteIdenticalDedup:
    """The byte-identical dedup reuses the download-time content_hash."""

    def test_reuses_stored_content_hash_without_reading_files(self, tmp_path: Path):
        # Two files with DIFFERENT bytes but the SAME stored content_hash are
        # treated as byte-identical, which is only possible if the stored hash
        # is reused instead of the files being re-hashed from disk.
        a = tmp_path / "a.pdf"
        a.write_text("AAAA", encoding="utf-8")
        b = tmp_path / "b.pdf"
        b.write_text("BBBB", encoding="utf-8")
        records = [
            {"path": a.as_posix(), "content_hash": "deadbeef", "review_relevance": 0.9},
            {"path": b.as_posix(), "content_hash": "deadbeef", "review_relevance": 0.1},
        ]
        redundant = DocumentReviewer._find_byte_identical_duplicates(records)
        assert redundant == {1}  # higher-ranked index 0 is kept

    def test_falls_back_to_hashing_when_hash_absent(self, tmp_path: Path):
        # No stored hash -> hash the files from disk; identical bytes dedup.
        a = tmp_path / "a.pdf"
        a.write_text("SAME-CONTENT", encoding="utf-8")
        b = tmp_path / "b.pdf"
        b.write_text("SAME-CONTENT", encoding="utf-8")
        records = [
            {"path": a.as_posix(), "review_relevance": 0.5},
            {"path": b.as_posix(), "review_relevance": 0.2},
        ]
        redundant = DocumentReviewer._find_byte_identical_duplicates(records)
        assert redundant == {1}

    def test_rehashes_browser_escalated_stale_hash(self, tmp_path: Path):
        # Browser escalation rewrites files in place, so their stored hash is
        # stale. Two escalated files sharing a stale hash but with different
        # on-disk bytes must NOT be deduped -> the file is re-hashed.
        a = tmp_path / "a.html"
        a.write_text("rendered-A", encoding="utf-8")
        b = tmp_path / "b.html"
        b.write_text("rendered-B", encoding="utf-8")
        records = [
            {"path": a.as_posix(), "content_hash": "stale", "browser_escalated": True},
            {"path": b.as_posix(), "content_hash": "stale", "browser_escalated": True},
        ]
        redundant = DocumentReviewer._find_byte_identical_duplicates(records)
        assert redundant == set()


class _LevelClient(MeteredReviewClient):
    """Return one anchored level per call and record every prompt."""

    def __init__(self, level: int) -> None:
        super().__init__()
        self.level = level
        self.prompts: list[str] = []

    def respond(self, *, text, schema, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        reply = {
            "doc_kind": "record",
            "reason": "ok",
            "grade": self.level,
            "evidence": _evidence(" ".join(text.split()[:3])),
        }
        return {"data": reply}


def _equal_grade(relevance: float) -> dict:
    return {
        "is_primary": True,
        "relevance": relevance,
        "doc_kind": "k",
        "reason": "r",
    }


class TestAnchoredGrades:
    """The grader returns one rubric level; selection derives from it."""

    @pytest.fixture(autouse=True)
    def _text(self, monkeypatch):
        monkeypatch.setattr(
            document_utils,
            "extract_text_from_document",
            lambda _p: "record text " * 20,
        )

    @pytest.mark.parametrize(
        ("level", "primary", "relevance"),
        [(0, False, 0.0), (2, False, 0.5), (3, True, 0.75), (4, True, 1.0)],
    )
    def test_level_maps_to_primary_and_relevance(self, tmp_path: Path, level, primary, relevance):
        rec = _record(tmp_path, "doc.pdf", "County A")
        reviewer = DocumentReviewer(document_description="records")
        reviewer._client = _LevelClient(level)
        reviewer.review([rec], [])
        assert rec["review_is_primary"] is primary
        assert rec["review_relevance"] == relevance
        assert rec["review_selected"] is primary

    def test_prompt_carries_description_target_and_rubric(self, tmp_path: Path):
        rec = _record(tmp_path, "doc.pdf", "County A")
        rec["url"] = "https://example.gov/doc.pdf"
        client = _LevelClient(4)
        reviewer = DocumentReviewer(document_description="rates in {label}")
        reviewer._client = client
        reviewer.review([rec], [])
        (prompt,) = client.prompts
        assert prompt.startswith("TARGET DOCUMENT: rates in County A\n")
        assert '"identity": {"label": "County A"}' in prompt
        assert "untrusted hints, not facts" in prompt
        assert "SOURCE_URL: https://example.gov/doc.pdf" in prompt
        assert all(f"\n{level} = " in prompt for level in range(5))
        assert "grade (integer 0-4)" in prompt
        assert prompt.endswith("EXCERPT:\n" + "record text " * 20)

    def test_rubric_is_domain_agnostic(self):
        rubric = (document_reviewer._REVIEW_SYSTEM + document_reviewer._REVIEW_PROMPT).lower()
        for term in ("solar", "permit", "ordinance", "county", "tariff"):
            assert term not in rubric

    def test_rubric_change_invalidates_cache_and_rerun_is_free(self, tmp_path: Path, monkeypatch):
        cache = tmp_path / ".review_cache"
        rec = _record(tmp_path, "doc.pdf", "County A")

        def run() -> tuple[dict, _LevelClient]:
            reviewer = DocumentReviewer(document_description="records", cache_dir=cache)
            reviewer._client = _LevelClient(4)
            copy = dict(rec)
            reviewer.review([copy], [])
            return copy, reviewer._client

        first, client = run()
        assert not first["review_cached"] and len(client.prompts) == 1
        rerun, client = run()
        assert rerun["review_cached"] and client.prompts == []

        rubric = document_reviewer._REVIEW_PROMPT
        monkeypatch.setattr(
            document_reviewer,
            "_REVIEW_PROMPT",
            rubric.replace("0 = unrelated", "0 = off-topic"),
        )
        changed, client = run()
        assert not changed["review_cached"] and len(client.prompts) == 1

    @pytest.mark.parametrize("reverse", [False, True])
    def test_equal_grades_rank_by_provenance_not_input_order(self, tmp_path: Path, reverse):
        names = ("a", "b", "c")
        records = [_record(tmp_path, f"{n}.pdf", "County A") for n in names]
        for rec, host in zip(records, ("c", "a", "b"), strict=True):
            rec["url"] = f"https://{host}.example.gov/doc.pdf"
        reviewer = DocumentReviewer(
            document_description="records",
            keep_top=1,
            deduplicate_redundant=False,
        )
        _stub_grades(reviewer, {f"{n}.pdf": _equal_grade(1.0) for n in names})
        reviewer.review(records[::-1] if reverse else records, [])
        assert [r["review_selected"] for r in records] == [False, True, False]

    @pytest.mark.parametrize("reverse", [False, True])
    def test_equal_grade_mirror_keeps_the_same_copy(self, tmp_path: Path, reverse):
        records = [_record(tmp_path, f"{n}.pdf", "County A") for n in "ab"]
        for rec, query in zip(records, ("", "?cache=1800"), strict=True):
            rec["url"] = f"https://example.gov/doc.pdf{query}"
            rec["content_hash"] = "same"
        reviewer = DocumentReviewer(document_description="records", keep_top=2)
        _stub_grades(reviewer, {f"{n}.pdf": _equal_grade(0.75) for n in "ab"})
        reviewer.review(records[::-1] if reverse else records, [])
        assert [r["review_selected"] for r in records] == [False, True]
        assert records[0]["review_redundant"] is True


class TestRecordings:
    """Recordings are graded on a cached transcript, never transcribed."""

    TRANSCRIPT = "[00:00:00] I support the wind project because " * 3

    @staticmethod
    def _recording(tmp_path: Path, name: str, transcript: str | None) -> dict:
        record = _record(tmp_path, name, "County A")
        if transcript is not None:
            document_utils.write_text_cache(
                Path(record["path"]),
                transcript,
                method="transcript:fixture:v1",
            )
        return record

    def test_recording_with_a_cached_transcript_is_graded(self, tmp_path: Path):
        recording = self._recording(tmp_path, "meeting.mp3", self.TRANSCRIPT)
        client = _LevelClient(4)
        reviewer = DocumentReviewer(document_description="public comments")
        reviewer._client = client
        _, notes = reviewer.review([recording], [])
        (prompt,) = client.prompts
        assert prompt.endswith(self.TRANSCRIPT)
        assert recording["review_relevance"] == 1.0
        assert recording["review_selected"] is True
        assert not any("recording(s) kept" in note for note in notes)

    def test_untranscribed_recordings_stay_selected_outside_keep_top(self, tmp_path: Path):
        recordings = [self._recording(tmp_path, f"meeting-{n}.mp3", None) for n in (1, 2)]
        minutes = _record(tmp_path, "minutes.txt", "County A")
        Path(minutes["path"]).write_text("minutes of the hearing " * 5, encoding="utf-8")
        client = _LevelClient(4)
        reviewer = DocumentReviewer(document_description="public comments", keep_top=1)
        reviewer._client = client
        _, notes = reviewer.review([*recordings, minutes], [])
        assert len(client.prompts) == 1
        assert minutes["review_selected"] is True
        for recording in recordings:
            assert recording["review_selected"] is True
            assert "review_relevance" not in recording
            assert not recording.get("review_failed")
            assert recording["review_reason"] == document_reviewer.RECORDING_KEPT
            assert record_is_curated(recording, review_ran=True)
        assert "2 recording(s) kept without review (transcribed at extraction)." in notes
        assert not any("browser_escalation" in note for note in notes)

    def test_browser_escalation_note_counts_only_non_audio_unreadables(self, tmp_path: Path):
        silent = self._recording(tmp_path, "silent.mp3", "[inaudible]")
        shell = _record(tmp_path, "shell.html", "County A")
        reviewer = DocumentReviewer(document_description="public comments")
        reviewer._client = _LevelClient(4)
        _, notes = reviewer.review([silent, shell], [])
        assert silent["status"] == shell["status"] == "skipped_unreadable"
        assert not record_is_curated(silent, review_ran=True)
        assert (
            "1 document(s) had too little text to review; JS-rendered pages "
            "need discovery.crawl.browser_escalation." in notes
        )

    def test_rediscovered_recording_is_graded_on_the_extraction_transcript(self, tmp_path: Path):
        """A transcript cached on the curated copy grades the run's same bytes."""
        from psweep.discovery.engine import DiscoveryEngine

        curated = tmp_path / "curated"
        published = curated / "county-a" / "meeting.mp3"
        published.parent.mkdir(parents=True)
        published.write_bytes(b"ID3 recording bytes")
        document_utils.write_text_cache(published, self.TRANSCRIPT, method="transcript:fixture:v1")
        run_copy = tmp_path / "run" / "county-a" / "meeting.mp3"
        run_copy.parent.mkdir(parents=True)
        run_copy.write_bytes(b"ID3 recording bytes")
        changed = tmp_path / "run" / "county-a" / "other.mp3"
        changed.write_bytes(b"ID3 different bytes")
        records = [
            {
                **_record(tmp_path, "unused.txt", "County A"),
                "path": path.as_posix(),
                "relative_path": f"county-a/{path.name}",
            }
            for path in (run_copy, changed)
        ]

        DiscoveryEngine._seed_recording_transcripts(curated, records)

        assert document_utils.read_text_cache(run_copy) == self.TRANSCRIPT
        assert document_utils.read_text_cache(changed) is None
        client = _LevelClient(4)
        reviewer = DocumentReviewer(document_description="public comments")
        reviewer._client = client
        reviewer.review(records[:1], [])
        assert client.prompts[0].endswith(self.TRANSCRIPT)
        assert records[0]["review_relevance"] == 1.0


@pytest.mark.parametrize("domain", ["infrastructure", "equipment", "regulatory"])
def test_grounded_contract_retains_qualified_source_context(tmp_path, monkeypatch, domain):
    quote = f"{domain} Asset A is reported to meet the condition for phase B."
    text = quote + " Publication issued 2024-02. " * 10
    monkeypatch.setattr(document_utils, "extract_text_from_document", lambda _p: text)
    evidence = _evidence(quote)
    evidence.update(
        claim_kind={"value": "attributed, conditional", "quote": quote},
        scope={"value": "phase B only", "quote": quote},
        source_date={"value": "2024-02", "precision": "month", "quote": "issued 2024-02"},
        coverage={"role": {"quote": quote, "scope": "phase B only"}},
    )

    class Client(MeteredReviewClient):
        def respond(self, **_kwargs):
            return {
                "data": {"grade": 4, "doc_kind": domain, "reason": "quoted", "evidence": evidence}
            }

    record = _record(tmp_path, "source.txt", "Asset A")
    reviewer = DocumentReviewer(
        document_description="mandatory information", coverage={"role": "claim"}
    )
    reviewer._client = Client()
    reviewer.review([record], [])
    assert record["review_selected"]
    assert record["review_evidence"] == evidence
    assert record["review_coverage"]["sampled_chars"] == len(text)
    assert record["review_initial_grade"]["evidence"] == evidence


@pytest.mark.parametrize("quote", ["fabricated evidence", "source ... source", "", None])
def test_invalid_primary_evidence_is_pending_without_success_cache(tmp_path, monkeypatch, quote):
    monkeypatch.setattr(
        document_utils, "extract_text_from_document", lambda _p: "source evidence " * 100
    )
    evidence = _evidence("source evidence")
    evidence["content_quote"] = quote

    class Client(MeteredReviewClient):
        def respond(self, **_kwargs):
            return {
                "data": {
                    "grade": 4,
                    "doc_kind": "source",
                    "reason": "accepted",
                    "evidence": evidence,
                }
            }

    record = _record(tmp_path, "source.txt", "A")
    reviewer = DocumentReviewer(document_description="records", cache_dir=tmp_path / "cache")
    reviewer._client = Client()
    reviewer.review([record], [])
    assert record["review_failed"] and not record["review_selected"]
    assert not _cached_grades(tmp_path / "cache")


_TOC = "1. Territory to Which the Rate Schedule Applies ........ 12\nNONRESIDENTIAL ........ 100"


@pytest.mark.parametrize(
    "quote",
    [
        "Territory to Which the Rate Schedule Applies ........ 12",
        "territory to which the rate schedule applies … 12",
        "NONRESIDENTIAL ........ 100",
    ],
)
def test_literal_dot_leader_quote_is_grounded(quote):
    reviewer = DocumentReviewer(document_description="records")
    reviewer._verify_evidence({**_equal_grade(1.0), "evidence": _evidence(quote)}, [_TOC])


@pytest.mark.parametrize(
    "quote",
    ["Territory to Which ... 12", "RESIDENTIAL ........ 10", "Applies ........ 12 ... 100"],
)
def test_elided_or_partial_word_quote_is_not_grounded(quote):
    reviewer = DocumentReviewer(document_description="records")
    with pytest.raises(document_reviewer.ReviewError, match="evidence"):
        reviewer._verify_evidence({**_equal_grade(1.0), "evidence": _evidence(quote)}, [_TOC])


def test_punctuation_only_structured_quote_is_not_grounded():
    reviewer = DocumentReviewer(document_description="records")
    evidence = _evidence("NONRESIDENTIAL ........ 100")
    evidence["scope"]["quote"] = "........"
    with pytest.raises(document_reviewer.ReviewError, match="evidence"):
        reviewer._verify_evidence({**_equal_grade(1.0), "evidence": evidence}, [_TOC])


def _dated(name: str, value: str | None, precision: str = "day", relevance: float = 0.75):
    source_date = value and {"value": value, "precision": precision, "quote": "effective"}
    return {
        "path": name,
        "url": f"https://example.com/{name}",
        "review_relevance": relevance,
        "review_evidence": {"coverage": {}, "source_date": source_date},
    }


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        # A provably older edition loses even with a higher grade.
        ([_dated("a.pdf", "2008-01-11", relevance=1.0), _dated("z.pdf", "2026-03-01")], "z.pdf"),
        # A year can mean any day in it, so it overlaps a later day and grade decides.
        ([_dated("a.pdf", "2026", "year", 1.0), _dated("z.pdf", "2026-09-01")], "a.pdf"),
        # The same month overlaps; a later month is provably newer.
        ([_dated("a.pdf", "2026-03", "month", 1.0), _dated("z.pdf", "2026-03-15")], "a.pdf"),
        ([_dated("a.pdf", "2026-02", "month", 1.0), _dated("z.pdf", "2026-03-01")], "z.pdf"),
        # Undated editions are never dropped; grade then provenance decide.
        ([_dated("a.pdf", None, relevance=1.0), _dated("z.pdf", "2026-03-01")], "a.pdf"),
        ([_dated("z.pdf", None), _dated("a.pdf", "2008-01-11")], "a.pdf"),
    ],
)
def test_prefer_newest_drops_only_provably_older_editions(records, expected):
    reviewer = DocumentReviewer(document_description="records", prefer_newest=True)
    assert reviewer._next_complement(records, set())["path"] == expected


def test_default_selection_ignores_dates_so_other_domains_are_unchanged():
    reviewer = DocumentReviewer(document_description="records")
    old, new = _dated("a-2008.pdf", "2008-01-11"), _dated("z-2026.pdf", "2026-03-01")
    assert reviewer._next_complement([new, old], set()) is old


def _edition_review(tmp_path, *, prefer_newest: bool, keep: int):
    """Old grade-4 and new grade-3 editions with distinct bytes and a valid dedup reply."""
    records = [_record(tmp_path, name, "T") for name in ("old.pdf", "new.pdf")]
    Path(records[1]["path"]).write_text("new edition bytes", encoding="utf-8")
    reviewer = DocumentReviewer(
        document_description="records", keep_top=1, prefer_newest=prefer_newest
    )
    grades = {"old.pdf": _equal_grade(1.0), "new.pdf": _equal_grade(0.75)}
    _stub_grades(reviewer, grades)
    for name, value in (("old.pdf", "2008-01-11"), ("new.pdf", "2026-03-01")):
        grades[name]["evidence"]["source_date"] = {
            "value": value,
            "precision": "day",
            "quote": "dummy",
        }

    class Client(MeteredReviewClient):
        def respond(self, **_kwargs):
            refs = [{"index": i, "field": "content_quote", "quote": "dummy"} for i in (0, 1)]
            group = {
                "keep_index": keep,
                "redundant_indices": [1 - keep],
                "reason": "same schedule",
                "evidence_refs": refs,
            }
            return {"data": {"redundancy_groups": [group]}}

    reviewer._client = Client()
    reviewer._ensure_client = lambda: reviewer._client
    _, notes = reviewer.review(records, [])
    return {Path(r["path"]).name: r for r in records}, notes


@pytest.mark.parametrize("keep", [0, 1])
def test_prefer_newest_keeps_newest_edition_through_comparative_dedup(tmp_path, keep):
    records, _ = _edition_review(tmp_path, prefer_newest=True, keep=keep)
    assert records["new.pdf"]["review_selected"]
    assert records["old.pdf"]["review_redundant"] and not records["old.pdf"]["review_selected"]


def test_default_dedup_keeper_is_unchanged(tmp_path):
    records, _ = _edition_review(tmp_path, prefer_newest=False, keep=0)
    assert records["old.pdf"]["review_selected"] and records["new.pdf"]["review_redundant"]


def test_prefer_newest_notes_undated_candidates_only_when_contested(tmp_path):
    reviewer = DocumentReviewer(
        document_description="records", keep_top=1, deduplicate_redundant=False, prefer_newest=True
    )
    records = [_record(tmp_path, name, "T") for name in ("undated.pdf", "dated.pdf")]
    grades = {name: _equal_grade(0.75) for name in ("undated.pdf", "dated.pdf")}
    _stub_grades(reviewer, grades)
    grades["dated.pdf"]["evidence"]["source_date"] = {
        "value": "2026-01-01",
        "precision": "day",
        "quote": "dummy",
    }
    _, notes = reviewer.review(records, [])
    assert sum(record["review_selected"] for record in records) == 1
    assert any("could not date 1" in note and "undated.pdf" in note for note in notes)
    reviewer._keep_top = 2
    _, notes = reviewer.review(records, [])
    assert not any("could not date" in note for note in notes)


def test_edition_date_wording_is_opt_in_for_prompt_and_cache(tmp_path):
    path = _record(tmp_path, "one.pdf", "T")["path"]
    default = DocumentReviewer(document_description="records")
    newest = DocumentReviewer(document_description="records", prefer_newest=True)
    assert default._review_prompt is document_reviewer._REVIEW_PROMPT
    assert "quote: publication-date support" in default._review_prompt
    assert "publication or effective date" in newest._review_prompt
    assert "publication or effective date" not in default._review_prompt
    assert default._cache_key(path) != newest._cache_key(path)
    assert default._cache_key(path) == DocumentReviewer(document_description="records")._cache_key(
        path
    )


def test_prefer_newest_rejects_coverage_and_non_booleans():
    with pytest.raises(ValueError, match="prefer_newest"):
        DocumentReviewer(document_description="records", prefer_newest="yes")
    with pytest.raises(ValueError, match="prefer_newest"):
        DocumentReviewer(document_description="records", prefer_newest=True, coverage={"a": "b"})


def test_sample_join_cannot_ground_a_source_quote():
    reviewer = DocumentReviewer(document_description="records")
    grade = {**_equal_grade(1.0), "evidence": _evidence("last first")}
    with pytest.raises(document_reviewer.ReviewError, match="evidence"):
        reviewer._verify_evidence(grade, ["last", "first"])


@pytest.mark.parametrize(
    "source_date",
    [
        {"value": "2023-02-29", "precision": "day", "quote": "issued"},
        {"value": "2024-13", "precision": "month", "quote": "issued"},
        {"value": "0000", "precision": "year", "quote": "issued"},
        {"value": "2024-01-01", "precision": "year", "quote": "issued"},
        {"value": "2024", "precision": "unknown", "quote": "issued"},
    ],
)
def test_date_format_calendar_and_precision_are_strict(source_date):
    reviewer = DocumentReviewer(document_description="records")
    evidence = {**_evidence("issued"), "source_date": source_date}
    assert not reviewer._valid_evidence(evidence, primary=True)


def test_unknown_coverage_and_old_cache_contract_are_rejected():
    reviewer = DocumentReviewer(document_description="records", coverage={"known": "specific need"})
    evidence = {**_evidence("source"), "coverage": {"invented": {"quote": "source"}}}
    assert not reviewer._valid_evidence(evidence, primary=True)
    assert not reviewer._valid_grade(_equal_grade(1.0))


@pytest.mark.parametrize("different_scope", [False, True])
def test_greedy_coverage_retains_complementary_sources_before_provenance(tmp_path, different_scope):
    records = [_record(tmp_path, f"{name}.txt", "A") for name in ("a", "b", "z")]
    reviewer = DocumentReviewer(
        document_description="records",
        keep_top=2,
        deduplicate_redundant=False,
        coverage={"history": "history", "status": "status"},
    )
    grades = {f"{name}.txt": _equal_grade(1.0) for name in ("a", "b", "z")}
    _stub_grades(reviewer, grades)
    for name, label in (("a", "history"), ("b", "history"), ("z", "status")):
        grades[f"{name}.txt"]["evidence"]["coverage"] = {label: {"quote": "dummy"}}
        if different_scope and name == "z":
            grades[f"{name}.txt"]["evidence"]["scope"]["value"] = "other phase"
    reviewer.review(records, [])
    assert [r["review_selected"] for r in records] == [True, False, True]


def test_coverage_descriptions_render_per_target_without_shared_mutation(tmp_path):
    record = _record(tmp_path, "a.txt", "A")
    reviewer = DocumentReviewer(
        document_description="records", coverage={"role": "Records for {label}"}
    )
    _stub_grades(reviewer, {"a.txt": _equal_grade(1.0)})
    reviewer.review([record], [])
    import json

    assert json.loads(record["review_target_context"])["coverage"] == {"role": "Records for A"}
    assert reviewer._coverage == {"role": "Records for {label}"}


@pytest.mark.parametrize("change", ["reference", "cycle", "scope", "claim", "content", "labels"])
def test_dedup_rejects_unsupported_or_complementary_decisions(change):
    from copy import deepcopy

    reviewer = DocumentReviewer(document_description="records", coverage={"role": "need"})
    records = [{"review_evidence": deepcopy(_evidence("same source"))} for _ in range(2)]
    group = {
        "keep_index": 0,
        "redundant_indices": [1],
        "reason": "same",
        "evidence_refs": [
            {"index": i, "field": "content_quote", "quote": "same source"} for i in (0, 1)
        ],
    }
    groups = [group]
    if change == "reference":
        group["evidence_refs"][1]["quote"] = "invented"
    elif change == "cycle":
        groups.append({**group, "keep_index": 1, "redundant_indices": [0]})
    elif change == "scope":
        records[1]["review_evidence"]["scope"]["value"] = "other phase"
    elif change == "claim":
        records[1]["review_evidence"]["claim_kind"]["value"] = "reported"
    elif change == "content":
        records[1]["review_evidence"]["content_quote"] = "conflicting source"
        group["evidence_refs"][1]["quote"] = "conflicting source"
    else:
        records[1]["review_evidence"]["coverage"] = {"role": {"quote": "same source"}}
    with pytest.raises(document_reviewer.ReviewError, match="dedup"):
        reviewer._redundant_indices({"data": {"redundancy_groups": groups}}, records)


def test_structural_keys_do_not_drop_differing_content(tmp_path):
    records = [_record(tmp_path, f"{name}.txt", "A") for name in ("a", "b")]
    reviewer = DocumentReviewer(
        document_description="records", keep_top=2, dedup_key_fields=["review_doc_kind"]
    )
    grades = {f"{name}.txt": _equal_grade(1.0) for name in ("a", "b")}
    _stub_grades(reviewer, grades)
    Path(records[1]["path"]).write_text("different bytes", encoding="utf-8")

    class Client(MeteredReviewClient):
        def respond(self, **_kwargs):
            return {"data": {"redundancy_groups": []}}

    reviewer._client = Client()
    reviewer._ensure_client = lambda: reviewer._client
    reviewer.review(records, [])
    assert all(record["review_selected"] for record in records)


def test_missing_mandatory_content_is_a_valid_grade_one(tmp_path, monkeypatch):
    monkeypatch.setattr(
        document_utils, "extract_text_from_document", lambda _p: "identity only " * 30
    )
    evidence = _evidence("identity only")
    evidence["content_quote"] = None

    class Client(MeteredReviewClient):
        def respond(self, **_kwargs):
            return {
                "data": {
                    "grade": 1,
                    "doc_kind": "record",
                    "reason": "missing required content",
                    "evidence": evidence,
                }
            }

    record = _record(tmp_path, "a.txt", "A")
    reviewer = DocumentReviewer(document_description="mandatory requirements")
    reviewer._client = Client()
    reviewer.review([record], [])
    assert record["review_relevance"] == 0.25
    assert not record["review_selected"] and not record.get("review_failed")


def test_fabricated_top_grade_escalates_without_caching_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        document_utils, "extract_text_from_document", lambda _p: "source evidence " * 30
    )

    class Client(MeteredReviewClient):
        def __init__(self, quote):
            super().__init__()
            self.quote = quote
            self.calls = 0

        def respond(self, **_kwargs):
            self.calls += 1
            return {
                "data": {
                    "grade": 4,
                    "doc_kind": "record",
                    "reason": "source",
                    "evidence": _evidence(self.quote),
                }
            }

    small, large = Client("invented quote"), Client("source evidence")
    record = _record(tmp_path, "a.txt", "A")
    reviewer = DocumentReviewer(
        document_description="records", escalate_model="large", cache_dir=tmp_path / "cache"
    )
    reviewer._client, reviewer._escalation_client = small, large
    reviewer.review([record], [])
    assert record["review_selected"] and small.calls == large.calls == 1
    assert (
        "initial_error" in record["review_grade"].get("audit", {}) or record["review_initial_error"]
    )
    assert not list((tmp_path / "cache").glob("*-initial.json"))


@pytest.mark.parametrize("compatible", [True, False])
def test_source_date_is_only_a_same_scope_grade_tiebreaker(tmp_path, compatible):
    records = [_record(tmp_path, f"{name}.txt", "A") for name in ("a", "z")]
    reviewer = DocumentReviewer(
        document_description="records",
        keep_top=1,
        deduplicate_redundant=False,
        coverage={"role": "need"},
    )
    grades = {f"{name}.txt": _equal_grade(1.0) for name in ("a", "z")}
    _stub_grades(reviewer, grades)
    for name, year in (("a", "2020"), ("z", "2024")):
        evidence = grades[f"{name}.txt"]["evidence"]
        evidence["coverage"] = {"role": {"quote": "dummy"}}
        evidence["source_date"] = {"value": year, "precision": "year", "quote": "dummy"}
    if not compatible:
        grades["z.txt"]["evidence"]["scope"]["value"] = "different phase"
    reviewer.review(records, [])
    assert [r["review_selected"] for r in records] == (
        [False, True] if compatible else [True, False]
    )


def test_forged_cached_quote_is_not_a_successful_reuse(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(
        document_utils, "extract_text_from_document", lambda _p: "source evidence " * 30
    )
    record = _record(tmp_path, "a.txt", "A")
    cache = tmp_path / "cache"
    reviewer = DocumentReviewer(document_description="records", cache_dir=cache)
    reviewer._client = _LevelClient(4)
    reviewer.review([record], [])
    (entry,) = cache.glob("*.json")
    payload = json.loads(entry.read_text())
    payload["llm"]["evidence"]["content_quote"] = "fabricated"
    entry.write_text(json.dumps(payload), encoding="utf-8")
    reviewer.review([record], [])
    assert record["review_selected"] and not record["review_cached"]
    assert len(reviewer._client.prompts) == 2
    assert not reviewer._grade_memo and not reviewer._dedup_memo


def _shared_records(tmp_path, names):
    """Source-only records: no roster, no target metadata, one shared group."""
    records = [_record(tmp_path, name, "unused") for name in names]
    for record in records:
        record.pop("target_metadata")
        record.pop("target_label")
    return records


def _primary(relevance=0.9):
    return {"is_primary": True, "relevance": relevance, "doc_kind": "permit", "reason": "fixture"}


def _guidance():
    return {"is_primary": False, "relevance": 0.1, "doc_kind": "guidance", "reason": "fixture"}


def test_source_only_group_qualifies_independent_permits_and_rejects_guidance(tmp_path):
    permits = [f"permit-{n}.pdf" for n in range(1, 4)]
    guidance = ["guidelines.pdf", "policy.pdf"]
    records = _shared_records(tmp_path, [*guidance[:1], *permits, *guidance[1:]])
    reviewer = DocumentReviewer(
        document_description="issued permits", keep_top=5, deduplicate_redundant=False
    )
    _stub_grades(
        reviewer, {**{n: _primary() for n in permits}, **{n: _guidance() for n in guidance}}
    )
    reviewer.review(records, [])
    selected = {Path(r["path"]).name for r in records if r["review_selected"]}
    assert selected == set(permits)
    assert not any(r.get("review_redundant") for r in records)


def test_source_only_group_cap_is_one_shared_keep_top(tmp_path):
    """keep_top bounds the whole shared group, not each facility."""
    names = [f"permit-{n}.pdf" for n in range(1, 7)]
    records = _shared_records(tmp_path, names)
    reviewer = DocumentReviewer(
        document_description="issued permits", keep_top=5, deduplicate_redundant=False
    )
    _stub_grades(reviewer, {n: _primary(0.9 - i / 100) for i, n in enumerate(names)})
    reviewer.review(records, [])
    selected = [Path(r["path"]).name for r in records if r["review_selected"]]
    assert len(selected) == 5 and "permit-6.pdf" not in selected


def test_source_only_default_keep_top_keeps_a_single_permit(tmp_path):
    """The default cap is why a shared collection must configure keep_top deliberately."""
    names = ["permit-1.pdf", "permit-2.pdf"]
    records = _shared_records(tmp_path, names)
    reviewer = DocumentReviewer(document_description="issued permits", deduplicate_redundant=False)
    _stub_grades(reviewer, {n: _primary() for n in names})
    reviewer.review(records, [])
    assert sum(r["review_selected"] for r in records) == 1


# --- Step 12b R1: diagnosable grounding failures -------------------------------------------


def _grounding_failure(segments, **evidence_overrides):
    reviewer = DocumentReviewer(document_description="records", coverage={"role": "need"})
    evidence = _evidence("anchor")
    evidence.update(evidence_overrides)
    with pytest.raises(document_reviewer.ReviewError) as caught:
        reviewer._verify_evidence({**_equal_grade(1.0), "evidence": evidence}, segments)
    return caught.value


def test_ungrounded_quote_names_kind_and_path_but_keeps_text_out_of_the_message():
    error = _grounding_failure(["anchor text"], content_quote="fabricated words")
    assert str(error) == "invalid document grade evidence: ungrounded evidence.content_quote"
    assert error.failure == {
        "kind": "ungrounded",
        "paths": ["evidence.content_quote"],
        "quotes": {"evidence.content_quote": "fabricated words"},
    }


def test_every_failing_path_is_named_including_structured_and_coverage_quotes():
    error = _grounding_failure(
        ["anchor text"],
        scope={"value": "s", "quote": "invented scope"},
        coverage={"role": {"quote": "invented coverage", "scope": "s"}},
    )
    assert error.failure["paths"] == ["evidence.scope", "evidence.coverage.role"]
    assert str(error).endswith("ungrounded evidence.scope, evidence.coverage.role")


@pytest.mark.parametrize(
    ("segments", "quote"),
    [
        (["anchor alpha The Regu", "la tion beta"], "The Regula"),  # straddles a sample boundary
        (["anchor alpha The Regu", "la tion beta"], "The Regulation"),  # word completed past a cut
    ],
)
def test_quote_across_a_sample_boundary_is_ungrounded(segments, quote):
    error = _grounding_failure(segments, content_quote=quote)
    assert error.failure["kind"] == "ungrounded"
    assert error.failure["paths"] == ["evidence.content_quote"]


def test_contract_mismatch_is_distinct_from_an_ungrounded_quote():
    reviewer = DocumentReviewer(document_description="records")
    grade = {**_equal_grade(0.25), "evidence": _evidence("anchor")}  # is_primary contradicts level
    with pytest.raises(document_reviewer.ReviewError) as caught:
        reviewer._verify_evidence(grade, ["anchor text"])
    assert str(caught.value) == "invalid document grade evidence: contract"
    assert caught.value.failure == {"kind": "contract", "paths": [], "quotes": {}}


def test_rejected_quote_text_is_truncated_to_300_characters():
    error = _grounding_failure(["anchor text"], content_quote="x" * 400)
    assert error.failure["quotes"]["evidence.content_quote"] == "x" * 300


def _grounding_client(quote: str):
    class Client(MeteredReviewClient):
        def respond(self, **_kwargs):
            evidence = _evidence("source evidence")
            evidence["content_quote"] = quote
            return {"data": {"grade": 4, "doc_kind": "source", "reason": "r", "evidence": evidence}}

    return Client()


def test_review_audit_keeps_failure_detail_and_notes_count_grounding_failures(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        document_utils, "extract_text_from_document", lambda _p: "source evidence " * 100
    )
    record = _record(tmp_path, "source.txt", "A")
    reviewer = DocumentReviewer(document_description="records", cache_dir=tmp_path / "cache")
    reviewer._client = _grounding_client("document dated")
    notes: list[str] = []
    reviewer.review([record], notes)
    assert record["review_failed"] and record["review_retryable"] is False
    assert record["review_initial_error"] == (
        "invalid document grade evidence: ungrounded evidence.content_quote"
    )
    assert record["review_error"] == "review unverifiable: " + record["review_initial_error"]
    assert record["review_initial_failure"] == {
        "kind": "ungrounded",
        "paths": ["evidence.content_quote"],
        "quotes": {"evidence.content_quote": "document dated"},
    }
    assert any("0 repaired, 1 unverifiable" in note for note in notes)
    assert not _cached_grades(tmp_path / "cache")


def test_a_grounding_failure_costs_one_call_plus_one_repair_and_a_malformed_one_two(
    tmp_path, monkeypatch
):
    """Pin the metering contract: a grounding failure gets one repair; malformed replies a retry."""
    monkeypatch.setattr(
        document_utils, "extract_text_from_document", lambda _p: "source evidence " * 100
    )
    record = _record(tmp_path, "source.txt", "A")
    reviewer = DocumentReviewer(document_description="records")
    reviewer._client = _grounding_client("invented")
    reviewer.review([record], [])
    assert reviewer.get_costs()["llm_calls"] == 2 and record["review_failed"]

    malformed = _record(tmp_path, "malformed.txt", "B")
    replies = iter([{"data": {"grade": 9}}, None])

    class Flaky(MeteredReviewClient):
        def respond(self, **_kwargs):
            return next(replies) or {
                "data": {
                    "grade": 4,
                    "doc_kind": "k",
                    "reason": "r",
                    "evidence": _evidence("source evidence"),
                }
            }

    other = DocumentReviewer(document_description="records")
    other._client = Flaky()
    other.review([malformed], [])
    assert malformed["review_selected"] and other.get_costs()["llm_calls"] == 2


# --- Step 12b R2: one bounded grounding repair ------------------------------------------------

_RUN_TOGETHER = "The permit documentdated May 2008 authorizes operation. "


class _RepairClient(MeteredReviewClient):
    """First answer is paraphrased (``document dated``); a repair may answer verbatim."""

    def __init__(self, *quotes: str) -> None:
        super().__init__()
        self.quotes = list(quotes)
        self.follow_ups: list = []

    def respond(self, *, text, schema, system_prompt, user_prompt, follow_up=None):
        self.follow_ups.append(follow_up)
        quote = self.quotes.pop(0)
        evidence = _evidence("documentdated")
        evidence["content_quote"] = quote
        return {"data": {"grade": 4, "doc_kind": "permit", "reason": "r", "evidence": evidence}}


def _repair_review(tmp_path, monkeypatch, client, **kwargs):
    monkeypatch.setattr(document_utils, "extract_text_from_document", lambda _p: _RUN_TOGETHER * 30)
    record = _record(tmp_path, "permit.txt", "A")
    reviewer = DocumentReviewer(
        document_description="permits", cache_dir=tmp_path / "cache", **kwargs
    )
    reviewer._client = client
    notes: list[str] = []
    reviewer.review([record], notes)
    return reviewer, record, notes


def test_one_repair_turns_a_paraphrased_quote_into_a_verbatim_grade(tmp_path, monkeypatch):
    client = _RepairClient("document dated", "documentdated")
    reviewer, record, notes = _repair_review(tmp_path, monkeypatch, client)
    assert record["review_selected"] and not record.get("review_failed")
    assert len(client.follow_ups) == 2 and client.follow_ups[0] is None
    assert reviewer.get_costs()["llm_calls"] == 2
    assert record["review_initial_repair"]["kind"] == "ungrounded"
    assert record["review_initial_repair"]["quotes"] == {"evidence.content_quote": "document dated"}
    assert any("1 repaired" in note for note in notes)


def test_repair_names_the_rejected_text_but_never_supplies_source_text(tmp_path, monkeypatch):
    client = _RepairClient("document dated", "documentdated")
    _repair_review(tmp_path, monkeypatch, client)
    assistant, user = client.follow_ups[1]
    assert assistant["role"] == "assistant" and "document dated" in assistant["content"]
    assert user["role"] == "user"
    for expected in ("evidence.content_quote", "document dated", "character for character"):
        assert expected in user["content"]
    assert "documentdated" not in user["content"]
    assert "[...]" in user["content"]


def test_repaired_grade_is_cached_under_the_unchanged_key_and_reused_free(tmp_path, monkeypatch):
    _repair_review(tmp_path, monkeypatch, _RepairClient("document dated", "documentdated"))
    repaired = {p.name for p in (tmp_path / "cache").glob("*.json")}
    clean_root = tmp_path / "clean"
    clean_root.mkdir()
    _repair_review(clean_root, monkeypatch, _RepairClient("documentdated"))
    assert repaired == {p.name for p in (clean_root / "cache").glob("*.json")}
    assert len(repaired) == 1
    again = _RepairClient()  # a second run must not call the model
    _, record, _ = _repair_review(tmp_path, monkeypatch, again)
    assert record["review_cached"] and record["review_selected"] and not again.follow_ups


def test_a_second_ungrounded_answer_fails_closed_after_exactly_two_calls(tmp_path, monkeypatch):
    client = _RepairClient("document dated", "documents dated")
    reviewer, record, notes = _repair_review(tmp_path, monkeypatch, client)
    assert record["review_failed"] and not record["review_selected"]
    assert record["review_error"].endswith("ungrounded evidence.content_quote")
    assert reviewer.get_costs()["llm_calls"] == 2
    assert not _cached_grades(tmp_path / "cache")
    assert any("0 repaired, 1 unverifiable" in note for note in notes)


def test_a_malformed_repair_answer_keeps_the_original_grounding_failure(tmp_path, monkeypatch):
    class Client(_RepairClient):
        def respond(self, **kwargs):
            if kwargs.get("follow_up"):
                self.follow_ups.append(kwargs["follow_up"])
                return {"data": {"grade": 9}}
            return super().respond(**kwargs)

    client = Client("document dated")
    _, record, _ = _repair_review(tmp_path, monkeypatch, client)
    assert record["review_failed"]
    assert record["review_error"].endswith("ungrounded evidence.content_quote")


def test_repair_draws_on_the_attempt_budget(tmp_path, monkeypatch):
    from psweep.discovery.attempt_budget import AttemptBudget

    client = _RepairClient("document dated", "documentdated")
    reviewer, record, _ = _repair_review(
        tmp_path, monkeypatch, client, attempt_budget=AttemptBudget(max_review_calls=1)
    )
    assert record["review_failed"] and record["review_budget_exhausted"]
    assert reviewer.get_costs()["llm_calls"] == 1


def test_provider_errors_are_not_repaired(tmp_path, monkeypatch):
    class Client(_RepairClient):
        def respond(self, **kwargs):
            self.follow_ups.append(kwargs.get("follow_up"))
            raise RuntimeError("provider down")

    client = Client()
    _, record, _ = _repair_review(tmp_path, monkeypatch, client)
    assert record["review_failed"] and "LLM grade call failed" in record["review_error"]
    assert len(client.follow_ups) == 1


def test_a_valid_first_answer_is_never_repaired(tmp_path, monkeypatch):
    client = _RepairClient("documentdated")
    reviewer, record, _ = _repair_review(tmp_path, monkeypatch, client)
    assert record["review_selected"] and "review_initial_repair" not in record
    assert reviewer.get_costs()["llm_calls"] == 1


# --- Step 12b R3: convergent terminal outcome ---------------------------------------------


def _cached_run(tmp_path, monkeypatch, client, *, description="permits", **kwargs):
    """Review one unchanged document against a shared cache directory."""
    monkeypatch.setattr(document_utils, "extract_text_from_document", lambda _p: _RUN_TOGETHER * 30)
    record = _record(tmp_path, "permit.txt", "A")
    reviewer = DocumentReviewer(
        document_description=description, cache_dir=tmp_path / "cache", **kwargs
    )
    reviewer._client = client
    notes: list[str] = []
    reviewer.review([record], notes)
    return reviewer, record, notes


def test_a_failed_repair_is_a_terminal_visible_unverifiable_outcome(tmp_path, monkeypatch):
    from psweep.utils.retry import is_retryable

    client = _RepairClient("document dated", "documents dated")
    reviewer, record, notes = _cached_run(tmp_path, monkeypatch, client)
    assert record["review_failed"] and record["review_unverifiable"]
    assert record["review_retryable"] is False and not is_retryable(record)
    assert not record["review_selected"]
    assert record["review_error"].startswith("review unverifiable: invalid document grade evidence")
    assert "unverifiable" in record["review_reason"]  # visible in review.csv `llm_reason`
    assert record["review_initial_failure"]["kind"] == "ungrounded"
    assert any("0 repaired, 1 unverifiable" in note for note in notes)
    (entry,) = (tmp_path / "cache").glob("*.json")
    assert entry.stem == reviewer._cache_key(
        str(tmp_path / "permit.txt"), "", record["review_target_context"]
    )
    assert "llm" not in json.loads(entry.read_text())


def test_the_same_inputs_are_never_billed_again_and_the_partition_can_complete(
    tmp_path, monkeypatch
):
    from psweep.utils.retry import is_retryable

    _cached_run(tmp_path, monkeypatch, _RepairClient("document dated", "documents dated"))
    again = _RepairClient()
    reviewer, record, _ = _cached_run(tmp_path, monkeypatch, again)
    assert not again.follow_ups and reviewer.get_costs()["llm_calls"] == 0
    assert record["review_unverifiable"] and not is_retryable(record)
    assert record["review_initial_failure"]["quotes"] == {
        "evidence.content_quote": "documents dated"
    }


def test_a_changed_grading_input_retries_automatically(tmp_path, monkeypatch):
    _cached_run(tmp_path, monkeypatch, _RepairClient("document dated", "documents dated"))
    retry = _RepairClient("documentdated")
    _, record, _ = _cached_run(tmp_path, monkeypatch, retry, description="issued air permits")
    assert record["review_selected"] and not record.get("review_unverifiable")
    assert len(retry.follow_ups) == 1


def test_provider_failure_and_budget_exhaustion_stay_retryable_with_no_negative_entry(
    tmp_path, monkeypatch
):
    from psweep.discovery.attempt_budget import AttemptBudget

    class Down(_RepairClient):
        def respond(self, **_kwargs):
            raise RuntimeError("provider down")

    _, record, _ = _cached_run(tmp_path, monkeypatch, Down())
    assert record["review_retryable"] is True and "review_unverifiable" not in record
    other = tmp_path / "budget"
    other.mkdir()
    _, record, _ = _cached_run(
        other,
        monkeypatch,
        _RepairClient("document dated", "documentdated"),
        attempt_budget=AttemptBudget(max_review_calls=1),
    )
    assert record["review_retryable"] is True and "review_unverifiable" not in record
    assert not list((tmp_path / "cache").glob("*.json"))
    assert not list((other / "cache").glob("*.json"))


def test_an_unverifiable_document_with_an_escalation_route_is_also_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(document_utils, "extract_text_from_document", lambda _p: _RUN_TOGETHER * 30)

    def review(small, large):
        record = _record(tmp_path, "permit.txt", "A")
        reviewer = DocumentReviewer(
            document_description="permits", escalate_model="large", cache_dir=tmp_path / "cache"
        )
        reviewer._client, reviewer._escalation_client = small, large
        reviewer.review([record], [])
        return record

    small, large = _RepairClient("document dated"), _RepairClient("documents dated")
    record = review(small, large)
    assert record["review_unverifiable"] and record["review_retryable"] is False
    assert small.follow_ups == [None] and large.follow_ups == [None]  # no repair with escalation
    again = _RepairClient()
    second = review(again, again)
    assert second["review_unverifiable"] and not again.follow_ups


@pytest.mark.parametrize(("decision", "curated"), [("keep", True), ("reject", False)])
def test_human_decision_resolves_an_unverifiable_document(tmp_path, monkeypatch, decision, curated):
    _, record, _ = _cached_run(
        tmp_path, monkeypatch, _RepairClient("document dated", "documents dated")
    )
    assert not record_is_curated(record, review_ran=True)
    record["human_decision"] = decision
    assert record_is_curated(record, review_ran=True) is curated
