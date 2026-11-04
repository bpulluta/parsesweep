"""Offline tests for the multi-candidate LLM judge."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from itertools import permutations
from pathlib import Path

import pytest

from psweep.exceptions import ExtractionError
from psweep.validation import judge as judge_module
from psweep.validation.judge import (
    MAX_ITEMS,
    MAX_PROMPT_CHARS,
    QUOTE_CHARS,
    Candidate,
    Item,
    cell_item,
    class_key,
    judge,
    load_cache,
)

MODEL = {"model": "judge-model", "provider": "openai"}


def supported(row: dict) -> list[dict]:
    return [
        {
            "id": row["id"],
            "reason": "r",
            "candidates": [
                {"id": c["id"], "support": "supported", "group": int(c["id"])}
                for c in row["candidates"]
            ],
        }
    ]


class FakeClient:
    """Answers each prompt row with ``answer(row)``; counts calls and records prompts."""

    def __init__(
        self,
        answer: Callable[[dict], list[dict]] = supported,
        cost: float = 0.0,
        tokens: tuple[int, int] = (0, 0),
    ) -> None:
        self.answer, self.cost, self.tokens = answer, cost, tokens
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        return len(self.prompts)

    def extract(self, text: str, schema: dict, system_prompt: str, user_prompt: str) -> dict:
        with self._lock:
            self.prompts.append(user_prompt)
            call = len(self.prompts)
        rows = [json.loads(line) for line in user_prompt.splitlines()]
        verdicts = [v for row in rows for v in self.answer({**row, "call": call})]
        return {
            "data": {"verdicts": verdicts},
            "cost": self.cost,
            "input_tokens": self.tokens[0],
            "output_tokens": self.tokens[1],
        }


def make(*values: str, quote: str | None = "q", field: str = "f", model: dict = MODEL) -> Item:
    return cell_item(field, "desc", [Candidate(v, quote, "u") for v in values], model)


def answer_with(candidates: list[dict], reason: str = "r") -> Callable[[dict], list[dict]]:
    return lambda row: [{"id": row["id"], "reason": reason, "candidates": candidates}]


# class_key


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("300", "300.0"),
        ("1,500", "1500"),
        ("0.0", "0"),
        ("-0", "0"),
        ("1.50", "1.5"),
        (" Residential  Use ", "residential use"),
    ],
)
def test_class_key_equates_variants(left: str, right: str) -> None:
    assert class_key(left) == class_key(right)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("300.0", "number:300"),
        ("1,500", "number:1500"),
        ("0.0", "number:0"),
        ("2.50", "number:2.5"),
        ("1000", "number:1000"),
        ("  Yes ", "text:yes"),
        ("> 100", "text:> 100"),
    ],
)
def test_class_key_canonical_string(value: str, expected: str) -> None:
    assert class_key(value) == expected


def test_class_key_keeps_punctuation() -> None:
    assert class_key("> 100") != class_key("< 100")


# Item identity


def test_key_is_independent_of_holder_order() -> None:
    holders = [
        Candidate("300", "limit 300", "u1"),
        Candidate("400", None, "u2"),
        Candidate("Three hundred", "q3", None),
    ]
    items = {cell_item("f", "d", list(order), MODEL) for order in permutations(holders)}
    assert len(items) == 1
    (item,) = items
    assert [c.value for c in item.candidates] == ["300", "400", "Three hundred"]


def test_raw_variants_share_class_with_min_display_value() -> None:
    item = cell_item("f", "d", [Candidate("300.0", "q", "u"), Candidate("300", None, None)], MODEL)
    assert item.candidates == (Candidate("300", "q", "u"),)
    assert item.key == cell_item("f", "d", [Candidate("300.0", "q", "u")], MODEL).key


def test_evidence_is_min_quote_and_order_independent() -> None:
    a, b = Candidate("300", "b  quote", "u2"), Candidate("300.0", "a quote", "u9")
    assert cell_item("f", "d", [a, b], MODEL) == cell_item("f", "d", [b, a], MODEL)
    assert cell_item("f", "d", [a, b], MODEL).candidates[0] == Candidate("300", "a quote", "u9")
    assert cell_item("f", "d", [a], MODEL).candidates[0].quote == "b quote"


def test_smaller_quote_changes_key_larger_does_not() -> None:
    base = [Candidate("1", "m quote", "u")]
    key = cell_item("f", "d", base, MODEL).key
    assert cell_item("f", "d", [*base, Candidate("1", "z quote", "u")], MODEL).key == key
    assert cell_item("f", "d", [*base, Candidate("1", "a quote", "u")], MODEL).key != key


def test_unquoted_class_uses_min_url() -> None:
    item = cell_item("f", "d", [Candidate("1", None, "u2"), Candidate("1", " ", "u1")], MODEL)
    assert item.candidates == (Candidate("1", None, "u1"),)
    assert cell_item("f", "d", [Candidate("1", None, None)], MODEL).candidates[0].url is None


def test_key_changes_with_model_prompt_and_content(monkeypatch: pytest.MonkeyPatch) -> None:
    key = make("1", "2").key
    assert make("1", "2", model={**MODEL}).key == key
    assert make("1", "3").key != key
    assert make("1", "2", quote="other").key != key
    assert make("1", "2", model={**MODEL, "model": "other"}).key != key
    monkeypatch.setattr(judge_module, "PROMPT_VERSION", "judge-next")
    assert make("1", "2").key != key


def test_identical_items_are_judged_once(tmp_path: Path) -> None:
    client = FakeClient()
    item = make("1", "2")
    verdicts, run = judge([item, make("2", "1"), item], client, tmp_path / "c.jsonl", workers=2)
    assert client.calls == 1 and len(client.prompts[0].splitlines()) == 1
    assert verdicts[item.key].support == ("supported", "supported") and run["failed"] == 0


# Validation, retry and post-rules


@pytest.mark.parametrize(
    "bad",
    [
        answer_with([{"id": "0", "support": "supported", "group": 0}]),
        answer_with(
            [{"id": "0", "support": "supported", "group": 0}] * 2
            + [{"id": "1", "support": "supported", "group": 1}]
        ),
        answer_with([{"id": str(i), "support": "supported", "group": i} for i in range(3)]),
        answer_with(
            [
                {"id": "0", "support": "maybe", "group": 0},
                {"id": "1", "support": "supported", "group": 1},
            ]
        ),
        answer_with(
            [
                {"id": "0", "support": "supported", "group": -1},
                {"id": "1", "support": "supported", "group": 1},
            ]
        ),
        answer_with(
            [
                {"id": "0", "support": "supported", "group": True},
                {"id": "1", "support": "supported", "group": 1},
            ]
        ),
        answer_with(
            [
                {"id": "0", "support": "supported", "group": "0"},
                {"id": "1", "support": "supported", "group": 1},
            ]
        ),
        lambda row: [],
        lambda row: supported(row) * 2,
    ],
    ids=[
        "missing-candidate",
        "duplicate-candidate",
        "extra-candidate",
        "bad-support",
        "negative-group",
        "bool-group",
        "string-group",
        "missing-item",
        "duplicate-item",
    ],
)
def test_malformed_item_retries_once_then_stays_unjudged(tmp_path: Path, bad: Callable) -> None:
    good, broken = make("1", "2"), make("3", "4")

    def answer(row: dict) -> list[dict]:
        return bad(row) if row["candidates"][0]["value"] == "3" else supported(row)

    client = FakeClient(answer)
    cache = tmp_path / "c.jsonl"
    verdicts, run = judge([good, broken], client, cache, workers=1)
    assert client.calls == 2 and len(client.prompts[1].splitlines()) == 1
    assert set(verdicts) == {good.key} and run["calls"] == 2 and run["failed"] == 1
    assert set(load_cache(cache)) == {good.key}


def test_retry_recovers_items(tmp_path: Path) -> None:
    client = FakeClient(lambda row: [] if row["call"] == 1 else supported(row))
    verdicts, run = judge([make("1", "2"), make("3", "4")], client, tmp_path / "c.jsonl", workers=1)
    assert client.calls == 2 and run["failed"] == 0 and len(verdicts) == 2


def test_client_error_counts_as_bad_response(tmp_path: Path) -> None:
    class Failing(FakeClient):
        def extract(self, *args: object, **kwargs: object) -> dict:
            self.prompts.append("")
            raise RuntimeError("boom")

    client = Failing()
    verdicts, run = judge([make("1", "2")], client, tmp_path / "c.jsonl", workers=1)
    assert client.calls == 2 and verdicts == {} and run["failed"] == 1
    assert run["error"] == "RuntimeError"


def test_sanitized_extraction_error_is_reported(tmp_path: Path) -> None:
    class Denied(FakeClient):
        def extract(self, *args: object, **kwargs: object) -> dict:
            self.prompts.append("")
            raise ExtractionError("LLM request failed: provider failure (PermissionDeniedError)")

    _, run = judge([make("1", "2")], Denied(), tmp_path / "c.jsonl", workers=1)
    assert run["failed"] == 1 and "PermissionDeniedError" in run["error"]


def test_unquoted_item_is_equivalence_only(tmp_path: Path) -> None:
    item = make("1", "2", quote=None)
    client = FakeClient(
        answer_with(
            [
                {"id": "0", "support": "supported", "group": 0},
                {"id": "1", "support": "unsupported", "group": 0},
            ]
        )
    )
    verdict = judge([item], client, tmp_path / "c.jsonl", workers=1)[0][item.key]
    assert verdict.support == ("unclear", "unclear") and verdict.groups == (0, 0)


def test_mixed_support_in_group_becomes_unclear(tmp_path: Path) -> None:
    item = make("1", "2", "3")
    client = FakeClient(
        answer_with(
            [
                {"id": "0", "support": "supported", "group": 4},
                {"id": "1", "support": "unsupported", "group": 4},
                {"id": "2", "support": "unsupported", "group": 7},
            ]
        )
    )
    verdict = judge([item], client, tmp_path / "c.jsonl", workers=1)[0][item.key]
    assert verdict.support == ("unclear", "unclear", "unsupported")


def test_groups_renumbered_by_first_appearance(tmp_path: Path) -> None:
    item = make("1", "2", "3")
    client = FakeClient(
        answer_with(
            [
                {"id": "2", "support": "supported", "group": 5},
                {"id": "0", "support": "supported", "group": 9},
                {"id": "1", "support": "unsupported", "group": 2},
            ],
            reason=" ".join(["word"] * 30),
        )
    )
    verdict = judge([item], client, tmp_path / "c.jsonl", workers=1)[0][item.key]
    assert verdict.groups == (0, 1, 2) and verdict.support == (
        "supported",
        "unsupported",
        "supported",
    )
    assert len(verdict.reason.split()) == judge_module.REASON_WORDS


# Batching


def test_batches_respect_item_limit(tmp_path: Path) -> None:
    client = FakeClient()
    verdicts, run = judge(
        (make(str(i), "x") for i in range(2 * MAX_ITEMS + 5)),
        client,
        tmp_path / "c.jsonl",
        workers=3,
    )
    assert sorted(len(p.splitlines()) for p in client.prompts) == [5, MAX_ITEMS, MAX_ITEMS]
    assert len(verdicts) == 2 * MAX_ITEMS + 5 and run["calls"] == 3


def test_batches_respect_char_cap_and_truncate_quotes(tmp_path: Path) -> None:
    quote = "z" * (QUOTE_CHARS * 4)
    items = [make(f"{i} " + "v" * 1_000, "w" * 1_000, quote=quote) for i in range(MAX_ITEMS)]
    client = FakeClient()
    verdicts, _ = judge(items, client, tmp_path / "c.jsonl", workers=1)
    assert client.calls > 1 and len(verdicts) == MAX_ITEMS
    assert all(len(p) <= MAX_PROMPT_CHARS for p in client.prompts)
    assert all("z" * (QUOTE_CHARS + 1) not in p for p in client.prompts)
    assert make("1", quote=quote + "a").key != make("1", quote=quote + "b").key


# Cache


def test_cache_round_trips_tuples_and_rerun_makes_no_calls(tmp_path: Path) -> None:
    items = [make(str(i), "x") for i in range(50)]
    cache = tmp_path / "validated" / ".judge_cache.jsonl"
    first, _ = judge(items, FakeClient(), cache, workers=2)
    loaded = load_cache(cache)
    assert loaded == first
    assert all(
        isinstance(v.support, tuple) and isinstance(v.groups, tuple) for v in loaded.values()
    )
    client = FakeClient()
    second, run = judge(items, client, cache, workers=2)
    assert client.calls == 0 and run["calls"] == 0 and run["cache_hits"] == 50 and second == first


def test_append_only_cache_survives_removing_and_readding(tmp_path: Path) -> None:
    cache = tmp_path / "c.jsonl"
    full = [make("1", "2"), make("1", "2", "3")]
    judge(full, FakeClient(), cache, workers=1)
    judge(full[:1], FakeClient(), cache, workers=1)
    lines = cache.read_text()
    client = FakeClient()
    _, run = judge(full, client, cache, workers=1)
    assert client.calls == 0 and run["cache_hits"] == 2 and cache.read_text() == lines


def test_completed_batches_survive_crash(tmp_path: Path) -> None:
    class Crash(BaseException):
        pass

    def answer(row: dict) -> list[dict]:
        if row["call"] == 3:
            raise Crash
        return supported(row)

    items = [make(str(i), "x") for i in range(3 * MAX_ITEMS)]
    cache = tmp_path / "c.jsonl"
    with pytest.raises(Crash):
        judge(items, FakeClient(answer), cache, workers=1)
    assert len(load_cache(cache)) == 2 * MAX_ITEMS
    client = FakeClient()
    _, run = judge(items, client, cache, workers=1)
    assert client.calls == 1 and run["cache_hits"] == 2 * MAX_ITEMS


def test_torn_line_is_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "c.jsonl"
    judge([make("1", "2")], FakeClient(), cache, workers=1)
    with cache.open("a") as handle:
        handle.write('{"key": "partial", "supp')
    assert set(load_cache(cache)) == {make("1", "2").key}


# Usage


def test_cost_shares_sum_to_batch_cost(tmp_path: Path) -> None:
    client = FakeClient(cost=0.9, tokens=(300, 60))
    items = [make(str(i), "x") for i in range(3)]
    verdicts, run = judge(items, client, tmp_path / "c.jsonl", workers=1)
    assert sum(v.cost_usd for v in verdicts.values()) == pytest.approx(0.9)
    assert [(v.input_tokens, v.output_tokens) for v in verdicts.values()] == [(100, 20)] * 3
    assert run == {
        "calls": 1,
        "cache_hits": 0,
        "failed": 0,
        "cost_usd": pytest.approx(0.9),
        "input_tokens": 300,
        "output_tokens": 60,
        "error": None,
    }


def test_retry_cost_counts_in_run_and_item_share(tmp_path: Path) -> None:
    first, second = make("1", "x"), make("2", "x")
    client = FakeClient(
        lambda row: [] if row["call"] == 1 and row["id"] == "0" else supported(row), cost=1.0
    )
    verdicts, run = judge([first, second], client, tmp_path / "c.jsonl", workers=1)
    assert run["cost_usd"] == pytest.approx(2.0)
    assert verdicts[first.key].cost_usd == pytest.approx(1.5)
    assert verdicts[second.key].cost_usd == pytest.approx(0.5)
