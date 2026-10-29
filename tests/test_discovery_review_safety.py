"""Regression coverage for bounded, isolated, auditable document review."""

from __future__ import annotations

import hashlib
import json
import os
import signal
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import pytest

from psweep.discovery import document_reviewer
from psweep.discovery.document_reviewer import DocumentReviewer
from psweep.extraction import document_utils, llm_factory


_GRADE = {
    "is_primary": True,
    "relevance": 0.9,
    "doc_kind": "record",
    "reason": "fixture",
}


@pytest.fixture(autouse=True)
def _fake_external_inputs(monkeypatch):
    """Use only local, deterministic text and model resolution."""
    monkeypatch.setattr(
        llm_factory,
        "resolve_llm_kwargs",
        lambda *_args, **_kwargs: {"model": "fixture", "provider": "fixture"},
    )
    monkeypatch.setattr(
        document_utils,
        "extract_text_from_document",
        lambda _path: "fixture document " * 100,
    )


def _record(tmp_path, name="report.pdf"):
    path = tmp_path / name
    path.write_text(f"document {name}", encoding="utf-8")
    return {"status": "downloaded", "path": str(path)}


def _stub_reviewer(**kwargs):
    reviewer = DocumentReviewer(
        document_description="records",
        deduplicate_redundant=False,
        **kwargs,
    )
    reviewer._client = object()
    reviewer._grade = lambda *_args, **_kwargs: dict(_GRADE)
    return reviewer


@contextmanager
def _deadline():
    """Interrupt a zero-step sampler loop without leaving a hung thread."""
    if not hasattr(signal, "setitimer"):
        pytest.skip("bounded sampler timeout requires POSIX timers")

    def _timeout(_signum, _frame):
        raise TimeoutError("sampler did not terminate")

    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, 1)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.parametrize("max_chars", [1, 5, 41, 42, 43, 50, 100])
def test_tiny_keyword_sampler_terminates_and_is_bounded(max_chars):
    """Tiny budgets and many keywords cannot create zero-step searches."""
    reviewer = DocumentReviewer(
        document_description="records",
        max_chars=max_chars,
        review_keywords=["needle"] * 200,
    )
    text = "needle " * 1000
    with _deadline():
        sample = reviewer._build_review_sample(text)
    assert 0 < len(sample) <= max_chars
    assert sample == reviewer._build_review_sample(text)


@pytest.mark.parametrize("max_chars", [0, -1, True, 1.5, "100"])
def test_direct_constructor_rejects_invalid_sample_budget(max_chars):
    """Direct callers receive the same positive-integer budget guarantee."""
    with pytest.raises(ValueError, match="max_chars"):
        DocumentReviewer(document_description="records", max_chars=max_chars)


def test_same_stem_formats_preserve_separate_human_decisions(tmp_path):
    """PDF and HTML sidecars never borrow each other's human decisions."""
    records = [
        _record(tmp_path, name) for name in ("report.pdf", "report.html")
    ]
    review_dir = tmp_path / ".review"
    review_dir.mkdir()
    decisions = {"report.pdf": "keep", "report.html": "reject"}
    for filename, decision in decisions.items():
        (review_dir / f"{filename}.json").write_text(
            json.dumps({"human": {"decision": decision}}),
            encoding="utf-8",
        )
    (review_dir / "report.json").write_text(
        json.dumps({"human": {"decision": "unrelated legacy decision"}}),
        encoding="utf-8",
    )
    reviewer = _stub_reviewer(keep_top=2)
    reviewer.review(records, [])
    for filename, decision in decisions.items():
        payload = json.loads((review_dir / f"{filename}.json").read_text())
        assert payload["file"] == filename
        assert payload["human"] == {"decision": decision}
        assert payload["llm"] == _GRADE


def test_atomic_sidecar_failure_keeps_prior_bytes(tmp_path, monkeypatch):
    """A failed replacement leaves the prior human-bearing file intact."""
    record = _record(tmp_path)
    sidecar = tmp_path / ".review" / "report.pdf.json"
    sidecar.parent.mkdir()
    original = '{"human": {"decision": "keep", "notes": "important"}}'
    sidecar.write_text(original, encoding="utf-8")

    def _fail_replace(_self, _target):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(Path, "replace", _fail_replace)
    reviewer = _stub_reviewer()
    reviewer.review([record], [])
    assert sidecar.read_text(encoding="utf-8") == original
    assert list(sidecar.parent.iterdir()) == [sidecar]
    assert record["review_failed"] is True
    assert "sidecar" in record["review_error"]


def test_changed_content_with_identical_stat_invalidates_grade(tmp_path):
    """Size and timestamp equality cannot disguise changed document bytes."""
    record = _record(tmp_path)
    reviewer = _stub_reviewer()
    reviewer.review([record], [])
    path = Path(record["path"])
    previous = path.stat()
    content = path.read_bytes()
    path.write_bytes(b"X" * len(content))
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    seen = []

    def _grade(*args, **kwargs):
        seen.append(args[0])
        return dict(_GRADE)

    reviewer._grade = _grade
    reviewer.review([record], [])
    assert seen == [str(path)]
    assert record["review_cached"] is False


def test_selection_changes_reuse_raw_grade(tmp_path):
    """Selection controls are not inputs to the raw LLM grade cache."""
    record = _record(tmp_path)
    _stub_reviewer(keep_top=1).review([record], [])
    reviewer = _stub_reviewer(keep_top=2)

    def _unexpected(*args, **kwargs):
        raise AssertionError("selection-only change must reuse grade")

    reviewer._grade = _unexpected
    reviewer.review([record], [])
    assert record["review_cached"] is True


@pytest.mark.parametrize(
    "config",
    [{"max_chars": 100}, {"review_keywords": ["needle"]}],
)
def test_sampling_config_invalidates_raw_grade(tmp_path, config):
    """Effective sampling configuration participates in cache identity."""
    record = _record(tmp_path)
    _stub_reviewer().review([record], [])
    reviewer = _stub_reviewer(**config)
    reviewer.review([record], [])
    assert record["review_cached"] is False


def test_resolved_model_identity_is_not_stale(tmp_path, monkeypatch):
    """A resolver change is reflected even when reusing a reviewer object."""
    record = _record(tmp_path)
    reviewer = _stub_reviewer()
    first = reviewer._cache_key(record["path"])
    monkeypatch.setattr(
        llm_factory,
        "resolve_llm_kwargs",
        lambda *_args, **_kwargs: {
            "model": "different",
            "provider": "fixture",
        },
    )
    assert reviewer._cache_key(record["path"]) != first


@pytest.mark.parametrize(
    "invalid",
    [
        {"is_primary": "false"},
        {"is_primary": 1},
        {"relevance": "0.9"},
        {"relevance": True},
        {"relevance": float("nan")},
        {"relevance": float("inf")},
        {"relevance": 1.1},
        {"doc_kind": []},
        {"reason": None},
    ],
)
def test_invalid_grade_fails_explicitly_and_retains_usage(tmp_path, invalid):
    """Bad grade fields never become truthy selections or erase usage."""
    record = _record(tmp_path)

    class Client:
        def extract(self, **kwargs):
            return {
                "data": {**_GRADE, **invalid},
                "cost": 0.25,
                "input_tokens": 10,
                "output_tokens": 3,
            }

    reviewer = DocumentReviewer(document_description="records")
    reviewer._client = Client()
    output, _notes = reviewer.review([record], [])
    assert output is not None
    assert record["review_failed"] is True
    assert record["review_selected"] is False
    assert "grade" in record["review_error"]
    costs = reviewer.get_costs()
    assert costs["total_cost_usd"] == 0.25
    assert costs["total_input_tokens"] == 10
    assert costs["total_output_tokens"] == 3
    assert costs["llm_calls"] == 1


def test_parallel_grades_accumulate_all_returned_usage(tmp_path):
    """Concurrent valid and invalid grades retain exact response usage."""
    records = [_record(tmp_path, f"report{i}.pdf") for i in range(4)]
    barrier = Barrier(4)

    class Client:
        def extract(self, **kwargs):
            invalid = barrier.wait(timeout=5) == 0
            return {
                "data": {**_GRADE, "is_primary": "false"}
                if invalid
                else _GRADE,
                "cost": 0.125,
                "input_tokens": 11,
                "output_tokens": 7,
            }

    reviewer = DocumentReviewer(
        document_description="records",
        deduplicate_redundant=False,
    )
    reviewer._client = Client()
    reviewer.review(records, [])
    costs = reviewer.get_costs()
    assert costs["total_cost_usd"] == 0.5
    assert costs["llm_calls"] == 4
    assert costs["total_input_tokens"] == 44
    assert costs["total_output_tokens"] == 28
    assert sum(record.get("review_failed", False) for record in records) == 1
    assert all(
        not record["review_selected"]
        for record in records
        if record.get("review_failed")
    )


def test_llm_exception_is_sanitized_and_usage_is_unknown(tmp_path):
    """An exception carries no invented usage or leaked provider message."""
    record = _record(tmp_path)

    class Client:
        def extract(self, **kwargs):
            raise RuntimeError(
                "api_key=never-expose-this https://x/?token=secret"
            )

    reviewer = DocumentReviewer(document_description="records")
    reviewer._client = Client()
    reviewer.review([record], [])
    assert record["review_failed"] is True
    assert record["review_usage_unknown"] is True
    assert "never-expose" not in record["review_error"]
    assert "secret" not in record["review_error"]
    costs = reviewer.get_costs()
    assert costs["total_cost_usd"] == 0
    assert costs["total_input_tokens"] == 0
    assert costs["total_output_tokens"] == 0
    assert costs["usage_unknown_calls"] == 1


def test_pending_grade_futures_are_bounded(tmp_path, monkeypatch):
    """The existing four-worker window does not enqueue the whole target."""
    records = [_record(tmp_path, f"report{i}.pdf") for i in range(40)]
    state = {"pending": 0, "maximum": 0, "submitted": 0}

    class TrackedFuture(Future):
        def result(self, timeout=None):
            state["pending"] -= 1
            return super().result(timeout=timeout)

    class Executor:
        def __init__(self, max_workers):
            assert max_workers == 4

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, /, *args, **kwargs):
            state["pending"] += 1
            state["submitted"] += 1
            state["maximum"] = max(state["maximum"], state["pending"])
            future = TrackedFuture()
            future.set_result(fn(*args, **kwargs))
            return future

    monkeypatch.setattr(document_reviewer, "ThreadPoolExecutor", Executor)
    reviewer = _stub_reviewer()
    reviewer.review(records, [])
    assert state["maximum"] <= 4
    assert state["submitted"] == len(records)
    assert all(not record.get("review_failed") for record in records)


def test_comparative_dedup_exception_marks_survivors_without_losing_grades(
    tmp_path,
):
    """A failed comparative call blocks survivors but retains raw grades."""
    records = [_record(tmp_path, f"report{i}.pdf") for i in range(2)]

    class Client:
        def extract(self, **kwargs):
            raise RuntimeError("api_key=secret-provider-message")

    reviewer = _stub_reviewer(keep_top=2)
    reviewer._deduplicate_redundant = True
    reviewer._client = Client()
    reviewer.review(records, [])
    for record in records:
        assert record["review_failed"] is True
        assert record["review_selected"] is False
        assert record["review_usage_unknown"] is True
        assert record["review_grade"] == _GRADE
        assert "dedup" in record["review_error"]
        assert "secret" not in record["review_error"]
        sidecar = Path(record["path"]).parent / ".review"
        payload = json.loads(
            (sidecar / f"{Path(record['path']).name}.json").read_text(),
        )
        assert payload["llm"] == _GRADE
        assert payload["selection"]["selected"] is False
    costs = reviewer.get_costs()
    assert costs["usage_unknown_calls"] == 1
    assert costs["llm_calls"] == 1
    assert costs["total_cost_usd"] == 0


def test_client_setup_failure_is_explicit_and_sanitized(tmp_path):
    """Setup errors do not leak credentials into records or notes."""
    records = [_record(tmp_path)]
    reviewer = DocumentReviewer(document_description="records")

    def _unavailable():
        raise ValueError("api_key=private")

    reviewer._ensure_client = _unavailable
    _downloads, notes = reviewer.review(records, [])
    assert records[0]["review_failed"] is True
    assert records[0]["review_selected"] is False
    assert "private" not in records[0]["review_error"]
    assert "private" not in " ".join(notes)
    assert reviewer.get_costs()["llm_calls"] == 0


def test_invalid_cached_grade_is_regraded(tmp_path):
    """A matching cache key cannot make a malformed grade trustworthy."""
    record = _record(tmp_path)
    reviewer = _stub_reviewer()
    reviewer.review([record], [])
    sidecar = tmp_path / ".review" / "report.pdf.json"
    payload = json.loads(sidecar.read_text())
    payload["llm"]["is_primary"] = "false"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    reviewer.review([record], [])
    assert record["review_cached"] is False
    assert record["review_is_primary"] is True
    assert not record.get("review_failed")


def test_corrupt_prior_sidecar_is_not_overwritten(tmp_path):
    """Unreadable human decisions are preserved rather than replaced."""
    record = _record(tmp_path)
    sidecar = tmp_path / ".review" / "report.pdf.json"
    sidecar.parent.mkdir()
    sidecar.write_text("{broken human decision", encoding="utf-8")
    reviewer = _stub_reviewer()
    reviewer.review([record], [])
    assert sidecar.read_text() == "{broken human decision"
    assert record["review_failed"] is True
    assert record["review_selected"] is False
    assert "sidecar" in record["review_error"]


@pytest.mark.parametrize(
    ("keywords", "expected"),
    [
        (
            [],
            "02277c818414d1c1a0e218187e5146835cde8e4e6b0880225d0cf63b3669ce9f",
        ),
        (
            ["compressor", "setback"],
            "173fb9979f8e8608b1a5182ba0ccf6cc3b5a762da57e786f7f753cc3914c6249",
        ),
    ],
)
def test_normal_sampling_remains_byte_identical(keywords, expected):
    """The budget guard does not change normal pre-fix samples."""
    marker = "Compressor SENTINEL_TOKEN "
    text = "x" * 30000 + marker + "x" * (30000 - len(marker))
    reviewer = DocumentReviewer(
        document_description="records",
        max_chars=2000,
        review_keywords=keywords,
    )
    sample = reviewer._build_review_sample(text)
    assert hashlib.sha256(sample.encode()).hexdigest() == expected


@pytest.mark.parametrize(
    "data",
    [
        {"redundancy_groups": "not a list"},
        {"redundancy_groups": [None]},
        {
            "redundancy_groups": [
                {
                    "keep_index": True,
                    "redundant_indices": [1],
                    "reason": "bad",
                },
            ]
        },
        {
            "redundancy_groups": [
                {"keep_index": 0, "redundant_indices": None, "reason": "bad"},
            ]
        },
    ],
)
def test_invalid_comparative_assessment_preserves_grade_and_usage(
    tmp_path, data
):
    """Invalid dedup data fails explicitly with the actual returned usage."""
    records = [_record(tmp_path, f"report{i}.pdf") for i in range(2)]

    class Client:
        def extract(self, **kwargs):
            return {
                "data": data,
                "cost": 0.2,
                "input_tokens": 20,
                "output_tokens": 5,
            }

    reviewer = _stub_reviewer(keep_top=2)
    reviewer._deduplicate_redundant = True
    reviewer._client = Client()
    reviewer.review(records, [])
    assert all(record["review_failed"] for record in records)
    assert all(record["review_grade"] == _GRADE for record in records)
    assert not any(record["review_selected"] for record in records)
    costs = reviewer.get_costs()
    assert costs["total_cost_usd"] == 0.2
    assert costs["total_input_tokens"] == 20
    assert costs["total_output_tokens"] == 5
    assert costs["llm_calls"] == 1
    assert costs["usage_unknown_calls"] == 0
