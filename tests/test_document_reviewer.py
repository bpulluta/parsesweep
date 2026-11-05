"""Tests for the LLM document reviewer (grading orchestration + selection).

The live LLM call is stubbed; these verify per-target ranking, in-place
selection (files are NOT moved — the engine materializes curated/ separately),
the content-addressed grade cache, and graceful skips.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import psweep.extraction.document_utils as document_utils
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


def test_seed_only_review_preserves_shared_cap(tmp_path):
    """Seed-only files share the configured selection cap."""
    records = [_record(tmp_path, name, "unused") for name in ("one.pdf", "two.pdf")]
    for record in records:
        record.pop("target_metadata")
        record.pop("target_label")
    reviewer = DocumentReviewer(document_description="records", keep_top=1, deduplicate_redundant=False)
    _stub_grades(reviewer, {name: {"is_primary": True, "relevance": 0.9, "doc_kind": "record", "reason": "fixture"} for name in ("one.pdf", "two.pdf")})
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
    _stub_grades(reviewer, {"one.pdf": {"is_primary": True, "relevance": 0.9, "doc_kind": "record", "reason": "fixture"}})
    reviewer.review([record], [])
    assert record["review_selected"] and not record["review_cached"]


class TestReviewSelection:
    def test_primary_selected_in_place(self, tmp_path: Path):
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
    def test_review_never_moves_original_documents(self, tmp_path: Path):
        rec = _record(tmp_path, "ord.pdf", "County A")
        reviewer = DocumentReviewer(
            document_description="the ordinance", keep_top=1
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

        cache = tmp_path / ".review_cache"
        r1 = DocumentReviewer(
            document_description="the enacted ordinance", cache_dir=cache
        )
        _stub_grades(r1, grades)
        _, notes1 = r1.review([dict(rec)], [])
        assert any("0 reused from cache" in n for n in notes1)
        assert len(list(cache.glob("*.json"))) == 1

        # Second run: file unchanged → grade reused, LLM must NOT be called.
        r2 = DocumentReviewer(
            document_description="the enacted ordinance", cache_dir=cache
        )

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
        cache = tmp_path / ".review_cache"
        r1 = DocumentReviewer(document_description="desc one", cache_dir=cache)
        _stub_grades(r1, grades)
        r1.review([dict(rec)], [])

        # Different description → different cache key → must re-grade.
        r2 = DocumentReviewer(
            document_description="a different description", cache_dir=cache
        )
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
                "is_primary": True, "relevance": 0.9,
                "doc_kind": "comment", "reason": "x",
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

        r2._ensure_client = lambda: object()  # type: ignore[method-assign]
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
        assert reviewer._cache_key(rec["path"]) == reviewer._cache_key(
            rec["path"], source_url=""
        )
        assert reviewer._cache_key(rec["path"]) != reviewer._cache_key(
            rec["path"], source_url="https://dps.ny.gov/a.pdf"
        )


class _CapturingClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def extract(self, *, text, schema, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        return {"data": {"doc_kind": "comment", "reason": "ok", "grade": 4}}


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

    def test_final_url_preferred_over_original(
        self, tmp_path: Path, monkeypatch
    ):
        prompt = self._review_one(
            tmp_path,
            monkeypatch,
            url="https://short.example.com/x",
            final_url="https://puc.sd.gov/commission/dockets/Bury.pdf",
        )
        final = "https://puc.sd.gov/commission/dockets/Bury.pdf"
        assert f"SOURCE_URL: {final}" in prompt
        assert "short.example.com" not in prompt

    def test_original_url_used_when_final_missing(
        self, tmp_path: Path, monkeypatch
    ):
        prompt = self._review_one(
            tmp_path, monkeypatch, url="https://dps.ny.gov/ViewDoc-4.pdf"
        )
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

    def test_reuses_stored_content_hash_without_reading_files(
        self, tmp_path: Path
    ):
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


class _LevelClient:
    """Return one anchored level per call and record every prompt."""

    def __init__(self, level: int) -> None:
        self.level = level
        self.prompts: list[str] = []

    def extract(self, *, text, schema, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        reply = {"doc_kind": "record", "reason": "ok", "grade": self.level}
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
    def test_level_maps_to_primary_and_relevance(
        self, tmp_path: Path, level, primary, relevance
    ):
        rec = _record(tmp_path, "doc.pdf", "County A")
        reviewer = DocumentReviewer(document_description="records")
        reviewer._client = _LevelClient(level)
        reviewer.review([rec], [])
        assert rec["review_is_primary"] is primary
        assert rec["review_relevance"] == relevance
        assert rec["review_selected"] is primary

    def test_prompt_carries_description_target_and_rubric(
        self, tmp_path: Path
    ):
        rec = _record(tmp_path, "doc.pdf", "County A")
        rec["url"] = "https://example.gov/doc.pdf"
        client = _LevelClient(4)
        reviewer = DocumentReviewer(document_description="rates in {city}")
        reviewer._client = client
        reviewer.review([rec], [])
        (prompt,) = client.prompts
        assert prompt.startswith("TARGET DOCUMENT: rates in {city}\n")
        assert "SEARCH TARGET: label=County A\n" in prompt
        assert "SOURCE_URL: https://example.gov/doc.pdf" in prompt
        assert all(f"\n{level} = " in prompt for level in range(5))
        assert "grade (integer 0-4)" in prompt
        assert prompt.endswith("EXCERPT:\n" + "record text " * 20)

    def test_rubric_is_domain_agnostic(self):
        rubric = (
            document_reviewer._REVIEW_SYSTEM + document_reviewer._REVIEW_PROMPT
        ).lower()
        for term in ("solar", "permit", "ordinance", "county", "tariff"):
            assert term not in rubric

    def test_rubric_change_invalidates_cache_and_rerun_is_free(
        self, tmp_path: Path, monkeypatch
    ):
        cache = tmp_path / ".review_cache"
        rec = _record(tmp_path, "doc.pdf", "County A")

        def run() -> tuple[dict, _LevelClient]:
            reviewer = DocumentReviewer(
                document_description="records", cache_dir=cache
            )
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
    def test_equal_grades_rank_by_provenance_not_input_order(
        self, tmp_path: Path, reverse
    ):
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
    def test_equal_grade_mirror_keeps_the_same_copy(
        self, tmp_path: Path, reverse
    ):
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

    def test_untranscribed_recordings_stay_selected_outside_keep_top(
        self, tmp_path: Path
    ):
        recordings = [
            self._recording(tmp_path, f"meeting-{n}.mp3", None) for n in (1, 2)
        ]
        minutes = _record(tmp_path, "minutes.txt", "County A")
        Path(minutes["path"]).write_text("minutes of the hearing " * 5)
        client = _LevelClient(4)
        reviewer = DocumentReviewer(
            document_description="public comments", keep_top=1
        )
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
        assert (
            "2 recording(s) kept without review (transcribed at extraction)."
            in notes
        )
        assert not any("browser_escalation" in note for note in notes)

    def test_browser_escalation_note_counts_only_non_audio_unreadables(
        self, tmp_path: Path
    ):
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
