"""LLM judge: batched, cached, evidence-only verdicts on contested cells."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from psweep.compilation.roster import value_key
from psweep.exceptions import ExtractionError
from psweep.extraction.llm_factory import resolve_llm_kwargs

PROMPT_VERSION = "judge-v2"
MAX_ITEMS = 40
MAX_PROMPT_CHARS = 48_000
QUOTE_CHARS = 500
REASON_WORDS = 20
SUPPORT = ("supported", "unsupported", "unclear")
_USAGE = ("cost", "input_tokens", "output_tokens")
_SYSTEM = (
    "You are a strict QA judge. Each item has a field, its description and candidate "
    "values with optional quotes and URLs. Use ONLY the quotes; no outside knowledge; "
    "any quote may support any candidate. For each candidate give support (supported = "
    "a quote states it; unsupported = no quote states it or a quote contradicts it; "
    "unclear = ambiguous) and group (same integer = same fact, differing only in wording, "
    "format, unit or precision). Answer unclear rather than guess. Return JSON "
    '{"verdicts": [{"id", "candidates": [{"id", "support", "group"}], "reason"}]} with '
    "exactly one entry per item id, one per candidate id, and a reason of at most "
    f"{REASON_WORDS} words."
)
_STR = {"type": "string"}
_CANDIDATE_SCHEMA = {
    "type": "object",
    "required": ["id", "support", "group"],
    "properties": {
        "id": _STR,
        "support": {**_STR, "enum": list(SUPPORT)},
        "group": {"type": "integer", "minimum": 0},
    },
}
_SCHEMA = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "candidates", "reason"],
                "properties": {
                    "id": _STR,
                    "reason": _STR,
                    "candidates": {"type": "array", "items": _CANDIDATE_SCHEMA},
                },
            },
        }
    },
}


@dataclass(frozen=True)
class Candidate:
    value: str
    quote: str | None
    url: str | None


@dataclass(frozen=True)
class Item:
    key: str
    field: str
    description: str
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class Verdict:
    support: tuple[str, ...]
    groups: tuple[int, ...]
    reason: str
    cost_usd: float
    input_tokens: int
    output_tokens: int


def class_key(value: str) -> str:
    """Canonical string of ``value_key``: ``1,500`` = ``1500.0``, text casefolded."""
    kind, key = value_key(value)
    if kind == "number":
        key = "0" if key == 0 else format(key.normalize(), "f")
    return f"{kind}:{key}"


def judge_identity(stage: str | None, **kwargs: Any) -> dict[str, Any]:
    """Judge model identity for cache keys: resolved client kwargs minus api_key."""
    resolved = resolve_llm_kwargs(stage, **kwargs)
    resolved.pop("api_key", None)
    return resolved


def _evidence(holders: list[Candidate]) -> tuple[str | None, str | None]:
    quoted = [
        (" ".join(h.quote.split()), h.url or "") for h in holders if h.quote and h.quote.strip()
    ]
    if quoted:
        quote, url = min(quoted)
        return quote, url or None
    return None, min((h.url for h in holders if h.url), default=None)


def cell_item(
    field: str, description: str, holders: Iterable[Candidate], model: dict[str, Any]
) -> Item:
    """One item per cell: a representative per value class, independent of holder order."""
    classes: dict[str, list[Candidate]] = {}
    for holder in holders:
        classes.setdefault(class_key(holder.value), []).append(holder)
    ordered = sorted(classes)
    candidates = tuple(
        Candidate(min(h.value for h in classes[c]), *_evidence(classes[c])) for c in ordered
    )
    identity = {
        "prompt": PROMPT_VERSION,
        "model": model,
        "field": field,
        "description": description,
        "candidates": [[c, x.quote, x.url] for c, x in zip(ordered, candidates)],
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    return Item(key, field, description, candidates)


def _render(index: int, item: Item) -> str:
    candidates = [
        {
            "id": str(i),
            "value": c.value,
            "quote": (c.quote or "")[:QUOTE_CHARS] or None,
            "url": c.url,
        }
        for i, c in enumerate(item.candidates)
    ]
    return json.dumps(
        {
            "id": str(index),
            "field": item.field,
            "description": item.description,
            "candidates": candidates,
        },
        ensure_ascii=False,
    )


def _batches(items: Iterator[Item]) -> Iterator[list[Item]]:
    batch: list[Item] = []
    chars = 0
    for item in items:
        size = len(_render(MAX_ITEMS, item)) + 1
        if batch and (len(batch) == MAX_ITEMS or chars + size > MAX_PROMPT_CHARS):
            yield batch
            batch, chars = [], 0
        batch.append(item)
        chars += size
    if batch:
        yield batch


def _parse(item: Item, row: dict) -> tuple[tuple, tuple, str] | None:
    """Validate one item's verdict and apply the post-rules; None if malformed."""
    rows = row.get("candidates")
    by_id: dict[str, list[dict]] = {}
    for entry in rows if isinstance(rows, list) else ():
        if isinstance(entry, dict):
            by_id.setdefault(str(entry.get("id")), []).append(entry)
    entries = [by_id.get(str(i), []) for i in range(len(item.candidates))]
    if len(by_id) != len(entries) or any(len(e) != 1 for e in entries):
        return None
    support = [e[0].get("support") for e in entries]
    groups = [e[0].get("group") for e in entries]
    if any(s not in SUPPORT for s in support) or any(type(g) is not int or g < 0 for g in groups):
        return None
    if not any(c.quote for c in item.candidates):
        support = ["unclear"] * len(support)
    mixed = {g for g in groups if len({s for s, h in zip(support, groups) if h == g}) > 1}
    support = ["unclear" if g in mixed else s for s, g in zip(support, groups)]
    renumber = {g: n for n, g in enumerate(dict.fromkeys(groups))}
    reason = " ".join(str(row.get("reason") or "").split()[:REASON_WORDS])
    return tuple(support), tuple(renumber[g] for g in groups), reason


def _call(client: Any, items: list[Item]) -> tuple[dict[str, tuple], list, str | None]:
    """One judge call: accepted (support, groups, reason) by key, billed usage, call error."""
    prompt = "\n".join(_render(i, item) for i, item in enumerate(items))
    error = None
    try:
        result = client.extract(
            text=prompt, schema=_SCHEMA, system_prompt=_SYSTEM, user_prompt=prompt
        )
        rows = (result.get("data") or {}).get("verdicts")
    except Exception as exc:  # noqa: BLE001 - a failed call leaves its items for retry
        result, rows = getattr(exc, "usage", None) or {}, None
        # ExtractionError text is already sanitized; never copy other provider text.
        error = str(exc) if isinstance(exc, ExtractionError) else type(exc).__name__
    usage = [result.get(k) or 0 for k in _USAGE]
    seen: dict[str, list[dict]] = {}
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict):
            seen.setdefault(str(row.get("id")), []).append(row)
    accepted = {}
    for i, item in enumerate(items):
        found = seen.get(str(i), [])
        parsed = _parse(item, found[0]) if len(found) == 1 else None
        if parsed:
            accepted[item.key] = parsed
    return accepted, usage, error


def _judge_batch(
    client: Any, items: list[Item]
) -> tuple[dict[str, Verdict], int, list, str | None]:
    """Judge a batch, retrying unaccepted items once; split each call's usage evenly."""
    shares = {item.key: [0.0] * len(_USAGE) for item in items}
    spent: list = [0] * len(_USAGE)
    accepted: dict[str, tuple] = {}
    pending, calls, error = items, 0, None
    while pending and calls < 2:
        got, usage, failure = _call(client, pending)
        error = failure or error
        calls += 1
        for item in pending:
            shares[item.key] = [s + u / len(pending) for s, u in zip(shares[item.key], usage)]
        spent = [s + u for s, u in zip(spent, usage)]
        accepted.update(got)
        pending = [item for item in pending if item.key not in accepted]
    verdicts = {
        key: Verdict(*verdict, shares[key][0], round(shares[key][1]), round(shares[key][2]))
        for key, verdict in accepted.items()
    }
    return verdicts, calls, spent, error


def load_cache(path: Path) -> dict[str, Verdict]:
    """Read the append-only JSONL cache; later lines win and torn lines are skipped."""
    cache: dict[str, Verdict] = {}
    if not path.exists():
        return cache
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
                key = row.pop("key")
                support, groups = tuple(row.pop("support")), tuple(row.pop("groups"))
                cache[key] = Verdict(support, groups, **row)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
    return cache


def _lines(verdicts: Iterable[tuple[str, Verdict]]) -> str:
    return "".join(
        json.dumps({"key": key, **asdict(v)}, sort_keys=True) + "\n" for key, v in verdicts
    )


def judge(
    items: Iterable[Item], client: Any, cache_path: Path, *, workers: int
) -> tuple[dict[str, Verdict], dict]:
    """Verdicts by item key plus this run's usage; each batch is cached on completion."""
    cache = load_cache(cache_path)
    verdicts: dict[str, Verdict] = {}
    run = {
        "calls": 0,
        "cache_hits": 0,
        "failed": 0,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "error": None,
    }
    seen: set[str] = set()

    def fresh() -> Iterator[Item]:
        for item in items:
            if item.key in seen:
                continue
            seen.add(item.key)
            if item.key in cache:
                verdicts[item.key] = cache[item.key]
                run["cache_hits"] += 1
            else:
                yield item

    def finish(future: Future) -> None:
        size = futures.pop(future)
        got, calls, spent, error = future.result()
        verdicts.update(got)
        run["error"] = error or run["error"]
        run["calls"] += calls
        run["failed"] += size - len(got)
        for name, amount in zip(("cost_usd", "input_tokens", "output_tokens"), spent):
            run[name] += amount
        if got:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with cache_path.open("a", encoding="utf-8") as handle:
                handle.write(_lines(got.items()))

    def drain() -> None:
        done = wait(futures, return_when=FIRST_COMPLETED).done
        for future in [f for f in futures if f in done]:
            finish(future)

    futures: dict[Future, int] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for batch in _batches(fresh()):
            if len(futures) >= 2 * max(1, workers):
                drain()
            futures[pool.submit(_judge_batch, client, batch)] = len(batch)
        while futures:
            drain()
    return verdicts, run
