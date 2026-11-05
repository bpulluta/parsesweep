"""Cell diff, consensus, metrics, review priority and the validation outputs."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import sqlite3
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from itertools import chain, combinations, takewhile
from pathlib import Path

from psweep.compilation.roster import NA
from psweep.extraction.roster import normalize
from psweep.utils.file_io import atomic_json, atomic_write
from psweep.utils.spreadsheet import csv_literal
from psweep.validation.align import Cluster, Columns, Source
from psweep.validation.judge import Candidate, Item, Verdict, cell_item, class_key

JudgeFn = Callable[[Iterable[Item]], tuple[dict[str, Verdict], dict]]

PRIORITIES = ("high", "medium", "low", "none")
HIGH, MEDIUM, LOW, NONE = range(len(PRIORITIES))
CONSENSUS = "consensus"
ALL = "*"
Z = 1.96
CATEGORIES = ("both_na", "agree", "changed", "new", "missing")
CELL_COUNTERS = (*CATEGORIES, "split", "unscored", "tp", "fp", "fn", "tp_j", "fp_j", "fn_j")
ACCURACY_COUNTERS = ("cells", "correct", "correct_j", "missed_no_docs", "missed_extraction")
ROW_COUNTERS = ("row_tp", "row_fp", "row_fn", "row_split")
MODEL_COUNTERS = ("rows", "filled", "judged", "supported")
SUMMARY_COLUMNS = (
    "label",
    "model",
    "role",
    "column",
    "base",
    "coverage",
    "accuracy",
    "accuracy_j",
    "row_precision",
    "row_recall",
    "row_f1",
    "precision",
    "recall",
    "f1",
    "precision_j",
    "recall_j",
    "f1_j",
    "support_rate",
    "quote_fidelity",
    "docs_failed",
    "cost_usd",
    "cost_per_document",
)
QUALITY_COLUMNS = ("quote_fidelity", "docs_failed", "cost_usd", "cost_per_document")
_BATCH = 10_000

# (category, state) -> (priority vs a contender, vs a reference); §3f review priority table.
_PAIR = {
    ("both_na", None): (NONE, NONE),
    ("agree", None): (LOW, LOW),
    ("changed", None): (HIGH, HIGH),
    ("changed", "same"): (LOW, LOW),
    ("changed", "primary"): (LOW, MEDIUM),
    ("changed", "other"): (HIGH, HIGH),
    ("new", None): (MEDIUM, MEDIUM),
    ("new", "corroborated"): (LOW, MEDIUM),
    ("new", "supported"): (LOW, LOW),
    ("new", "unsupported"): (HIGH, HIGH),
    ("new", "unclear"): (MEDIUM, MEDIUM),
    ("missing", None): (MEDIUM, LOW),
    ("missing", "supported"): (HIGH, HIGH),
    ("missing", "unsupported"): (LOW, LOW),
    ("missing", "unclear"): (MEDIUM, MEDIUM),
}


def pair_priority(
    category: str,
    *,
    reference: bool,
    judged: bool,
    same: bool = False,
    p: str | None = None,
    s: str | None = None,
    corroborated: bool = False,
) -> int:
    """Priority of the primary's cell against one source S; ``p``/``s`` are their supports."""
    if category in {"agree", "both_na"}:
        state = None
    elif not judged:
        state = "corroborated" if category == "new" and corroborated else None
    elif category == "changed":
        state = "same" if same else "primary" if p == "supported" and s != "supported" else "other"
    else:
        state = p if category == "new" else s
    return _PAIR[category, state][reference]


def category(x: str | None, base: str | None) -> str:
    """Pair category of class ``x`` against base class ``base`` (None = NA)."""
    if x is None:
        return "both_na" if base is None else "missing"
    if base is None:
        return "new"
    return "agree" if x == base else "changed"


def status(classes: dict[str, str | None], contenders: Iterable[str]) -> str:
    """Cell status over the holders' classes."""
    valued = {c for c in classes.values() if c}
    if not valued:
        return "all_na"
    if not any(classes.get(label) for label in contenders):
        return "reference_only"
    if len(valued) > 1:
        return "disputed"
    return "unanimous" if all(classes.values()) else "partial"


def majority(votes: Sequence) -> tuple[bool, object]:
    """Strict-majority vote among at least 2 voters; ``(False, None)`` is a split."""
    if len(votes) < 2:
        return False, None
    value, n = Counter(votes).most_common(1)[0]
    return (True, value) if 2 * n > len(votes) else (False, None)


def wilson(k: int, n: int) -> list[float] | None:
    """Wilson 95% score interval; None when n = 0."""
    if not n:
        return None
    p, z2 = k / n, Z * Z
    center = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = Z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return [round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)]


def _rate(k: int, n: int) -> float | None:
    return round(k / n, 4) if n else None


def _class(value: str | None) -> str | None:
    return class_key(value) if value and value.strip() and value.strip() != NA else None


def validate_tables(
    clusters: Iterable[Cluster],
    *,
    sources: Sequence[Source],
    columns: Columns,
    judge_model: dict | None,
    judge_fn: JudgeFn | None,
    staging: sqlite3.Connection,
    out_dir: Path,
    quality: dict[str, dict],
    align_stats: dict,
    inputs: dict,
    run: dict,
    covered: frozenset[tuple] | None,
    min_accuracy: float | None,
) -> dict:
    """Diff, judge, score and publish ``cells.csv``, ``summary.csv`` and ``summary.json``."""
    tally = _Tally(sources, columns, covered, (judge_model or {}) if judge_fn else None)
    staging.execute("DROP TABLE IF EXISTS clusters")
    staging.execute("DROP TABLE IF EXISTS cells")
    staging.execute(
        "CREATE TABLE clusters (sort TEXT NOT NULL, match TEXT NOT NULL, key TEXT NOT NULL, "
        "in_scope TEXT NOT NULL, data TEXT NOT NULL, items TEXT NOT NULL)"
    )
    staging.execute(
        "CREATE TABLE cells (priority INTEGER NOT NULL, cluster INTEGER NOT NULL, "
        "col INTEGER NOT NULL, line TEXT NOT NULL)"
    )
    items = tally.stage(clusters, staging)
    if judge_fn is None:
        deque(items, maxlen=0)
        verdicts, judged = {}, None
    else:
        verdicts, usage = judge_fn(items)
        replayed = sum(v.cost_usd for v in verdicts.values()) - usage.get("cost_usd", 0.0)
        judged = {
            "identity": judge_model,
            "items": len(verdicts) + usage.get("failed", 0),
            **usage,
            "cached_cost_usd": round(max(0.0, replayed), 6),
        }
        run = {
            **run,
            **{
                k: {**run.get(k, {}), "judge": usage.get(u, 0)}
                for k, u in (("llm_calls", "calls"), ("cost_usd", "cost_usd"))
            },
        }
    staging.commit()
    tally.verdicts = verdicts
    tally.score(staging)
    summary = tally.summary(quality, align_stats, min_accuracy)
    summary.update(inputs=inputs, run=run, judge=judged)
    atomic_write(out_dir / "cells.csv", _csv(chain([tally.header], tally.lines(staging))))
    atomic_write(out_dir / "summary.csv", _csv(_summary_rows(summary, sources)))
    atomic_json(out_dir / "summary.json", summary)
    return summary


def _csv(rows: Iterable[Sequence]) -> Iterator[str]:
    """Stream escaped CSV text one row at a time."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if v is None else csv_literal(str(v)) for v in row])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate()


class _Tally:
    """Pass 1 stages clusters and yields judge items; pass 2 scores and queues lines."""

    def __init__(
        self,
        sources: Sequence[Source],
        columns: Columns,
        covered: frozenset | None,
        judge_model: dict | None,
    ):
        self.labels = [s.label for s in sources]
        self.contenders = [s.label for s in sources if s.role != "reference"]
        self.refs = [s.label for s in sources if s.role == "reference"]
        self.primary = next(s.label for s in sources if s.role == "primary")
        self.columns, self.covered, self.roster = columns, covered, covered is not None
        self.focus = columns.focus
        self.titles = [columns.titles[f] for f in self.focus]
        self.consensus = len(self.contenders) >= 3
        self.judge_model = judge_model
        self.verdicts: dict[str, Verdict] = {}
        scored = set(columns.scored)
        keys = takewhile(lambda f: f not in scored, columns.titles)  # titles: keys, scored, quote
        self.header = [
            *(columns.titles[f] for f in keys),
            "match",
            "column",
            "status",
            *self.labels,
            *(f"{c}_{part}" for c in self.contenders for part in ("quote", "url")),
            f"{self.primary}_conflict",
            "miss",
            *(f"{s}_support" for s in self.labels),
            "judge_groups",
            "judge_reason",
            "review_priority",
        ]
        self.cells: defaultdict[tuple, Counter] = defaultdict(Counter)  # (label, title, base)
        self.rows: defaultdict[tuple, Counter] = defaultdict(Counter)  # (label, base)
        self.model: defaultdict[tuple, Counter] = defaultdict(Counter)  # (label, title)
        self.agree: defaultdict[tuple, Counter] = defaultdict(Counter)  # (pair, title | None)
        self.diag: defaultdict[str, Counter] = defaultdict(Counter)
        self.review, self.high = Counter(), Counter()
        self.rows_missed, self.no_docs, self.no_docs_keys = 0, 0, []
        self.out_scopes: set[tuple] = set()

    # -- pass 1 -------------------------------------------------------------
    def stage(self, clusters: Iterable[Cluster], staging: sqlite3.Connection) -> Iterator[Item]:
        batch = []
        for cluster in clusters:
            if cluster.match == "out_of_scope":
                if not self.roster:
                    self.out_scopes.add(tuple(cluster.scope))
                continue
            data = {
                label: [
                    [
                        None if (c := row.get(f)) is None else [c.value, c.quote, c.url, c.conflict]
                        for f in self.focus
                    ]
                    for row in cluster.rows[label]
                ]
                for label in self.labels
                if label in cluster.rows and (label in self.contenders or label in cluster.in_scope)
            }
            for label in self.contenders:
                for row in data.get(label, ()):
                    for title, entry in zip(self.titles, row, strict=True):
                        self.model[label, title]["rows"] += 1
                        self.model[label, title]["filled"] += _class(entry[0]) is not None
            keys: list[str | None] = [None] * len(self.focus)
            if cluster.match != "ambiguous" and self.judge_model is not None:
                for i, field in enumerate(self.focus):
                    holders = {
                        label: rows[0][i] for label, rows in data.items() if rows[0][i] is not None
                    }
                    item = self._item(field, holders)
                    if item:
                        keys[i] = item.key
                        yield item
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(encoded.encode()).hexdigest()[:16]
            sort = json.dumps([list(cluster.key), cluster.match], ensure_ascii=False) + digest
            batch.append(
                (
                    sort,
                    cluster.match,
                    json.dumps(list(cluster.key), ensure_ascii=False),
                    json.dumps(sorted(cluster.in_scope)),
                    encoded,
                    json.dumps(keys),
                )
            )
            if len(batch) >= _BATCH:
                staging.executemany("INSERT INTO clusters VALUES (?, ?, ?, ?, ?, ?)", batch)
                batch = []
        staging.executemany("INSERT INTO clusters VALUES (?, ?, ?, ?, ?, ?)", batch)

    def _item(self, field: str, holders: dict[str, list]) -> Item | None:
        """One item when the cell is disputed, or partial with a quoted valued holder."""
        classes = {label: _class(entry[0]) for label, entry in holders.items()}
        state = status(classes, self.contenders)
        valued = [Candidate(e[0], e[1], e[2]) for label, e in holders.items() if classes[label]]
        if state == "disputed" or (state == "partial" and any(c.quote for c in valued)):
            return cell_item(
                field, self.columns.descriptions.get(field, ""), valued, self.judge_model
            )
        return None

    # -- pass 2 -------------------------------------------------------------
    def score(self, staging: sqlite3.Connection) -> None:
        staging.execute("CREATE INDEX clusters_sort ON clusters (sort)")
        cursor = staging.execute(
            "SELECT match, key, in_scope, data, items FROM clusters ORDER BY sort"
        )
        batch = []
        for rank, (match, key, in_scope, data, items) in enumerate(cursor):
            lines = self._cluster(
                match,
                tuple(json.loads(key)),
                frozenset(json.loads(in_scope)),
                json.loads(data),
                json.loads(items),
            )
            batch += [
                (priority, rank, col, json.dumps(line, ensure_ascii=False))
                for col, priority, line in lines
            ]
            if len(batch) >= _BATCH:
                staging.executemany("INSERT INTO cells VALUES (?, ?, ?, ?)", batch)
                batch = []
        staging.executemany("INSERT INTO cells VALUES (?, ?, ?, ?)", batch)
        staging.execute("CREATE INDEX cells_order ON cells (priority, cluster, col)")
        staging.commit()

    def lines(self, staging: sqlite3.Connection) -> Iterator[list]:
        for (line,) in staging.execute("SELECT line FROM cells ORDER BY priority, cluster, col"):
            yield json.loads(line)

    def _cluster(self, match, key, in_scope, data, items) -> list[tuple[int, int, list]]:
        p = self.primary
        present = [label for label in self.labels if label in data]
        if match == "ambiguous":
            return [self._record(key, match, "ambiguous", MEDIUM, data, "")]
        miss = (
            "no_docs"
            if self.roster and tuple(map(normalize, key)) not in self.covered
            else "extraction"
        )
        refs_here = [r for r in self.refs if r in data]
        lines = []
        if not self.roster:
            self._rows(present, in_scope)
            record = self._record_line(key, match, data, in_scope, refs_here)
            if record:
                lines.append(record)
        if p not in data and refs_here:
            self.rows_missed += 1
        ref_valued = False
        for i, title in enumerate(self.titles):
            held = {label: data[label][0][i] for label in present if data[label][0][i] is not None}
            cls = {label: _class(entry[0]) for label, entry in held.items()}
            ref_valued |= any(cls.get(r) for r in self.refs)
            verdict = self.verdicts.get(items[i]) if items[i] else None
            v = None
            if verdict:
                v = dict(
                    zip(
                        sorted({c for c in cls.values() if c}),
                        zip(verdict.support, verdict.groups, strict=True),
                        strict=True,
                    )
                )
            self._score_cell(title, data, held, cls, v, miss)
            self._agreement(title, held, cls, v)
            self._diagnose(title, cls, v, miss)
            if p in data:
                priority = self._priority(held, cls, v)
                self.review[PRIORITIES[priority]] += 1
                self.high[title] += priority == HIGH
                lines.append(
                    (
                        i,
                        priority,
                        self._line(key, match, title, held, cls, v, verdict, miss, priority),
                    )
                )
        if self.roster and ref_valued and miss == "no_docs":
            self.no_docs += 1
            if len(self.no_docs_keys) < 10:
                self.no_docs_keys.append(list(key))
        return lines

    def _rows(self, present: list[str], in_scope: frozenset) -> None:
        """Row categories (rows mode) and row-level agreement."""
        here = set(present)
        for x in self.contenders:
            for r in self.refs:
                if r in in_scope:
                    _row(self.rows[x, r], x in here, r in here)
            if self.consensus:
                found, value = majority([o in here for o in self.contenders if o != x])
                if found:
                    _row(self.rows[x, CONSENSUS], x in here, value)
                else:
                    self.rows[x, CONSENSUS]["row_split"] += 1
        for a, b in combinations(self.labels, 2):
            if all(s not in self.refs or s in in_scope for s in (a, b)) and (
                a in here or b in here
            ):
                c = self.agree[f"{a}|{b}", None]
                c[
                    "matched" if a in here and b in here else "only_a" if a in here else "only_b"
                ] += 1

    def _score_cell(self, title, data, held, cls, v, miss) -> None:
        for x in self.contenders:
            if x not in held:
                for r in self.refs:  # a reference row X lacks: every scored cell is wrong
                    if r in held and x not in data:
                        c = self.cells[x, title, r]
                        c["cells"] += 1
                        c["missed_extraction"] += cls[r] is not None
                continue
            if v and cls[x]:
                self.model[x, title]["judged"] += 1
                self.model[x, title]["supported"] += v[cls[x]][0] == "supported"
            for r in self.refs:
                if r in data:
                    c = self.cells[x, title, r]
                    if r not in held:
                        c["unscored"] += 1
                        continue
                    cat = category(cls[x], cls[r])
                    same = _count(c, cat, cls[x], cls[r], v)
                    c["cells"] += 1
                    c["correct"] += cat in {"agree", "both_na"}
                    c["correct_j"] += cat in {"agree", "both_na"} or same
                    c[f"missed_{miss}"] += cat == "missing"
            if self.consensus:
                others = [o for o in self.contenders if o != x]
                if majority([o in data for o in others]) != (True, True):
                    continue
                found, base = majority([cls[o] for o in others if o in held])
                if found:
                    _count(self.cells[x, title, CONSENSUS], category(cls[x], base), cls[x], base, v)
                else:
                    self.cells[x, title, CONSENSUS]["split"] += 1

    def _agreement(self, title, held, cls, v) -> None:
        for a, b in combinations(self.labels, 2):
            if a in held and b in held:
                c = self.agree[f"{a}|{b}", title]
                if cls[a] and cls[b]:
                    c["both_valued"] += 1
                    c["agree"] += cls[a] == cls[b]
                    c["judged_same"] += bool(
                        cls[a] != cls[b] and v and v[cls[a]][1] == v[cls[b]][1]
                    )
                elif cls[a] or cls[b]:
                    c["one_sided"] += 1

    def _diagnose(self, title, cls, v, miss) -> None:
        """Primary versus the first valued reference."""
        valued = [r for r in self.refs if cls.get(r)]
        if not valued:
            return
        mine, theirs = cls.get(self.primary), cls[valued[0]]
        if mine is None:
            self.diag[title][f"missed_{miss}"] += 1
        elif mine != theirs and not (v and v[mine][1] == v[theirs][1]):
            supported = bool(v) and v[mine][0] == "supported"
            self.diag[title]["candidate_updates" if supported else "wrong"] += 1

    def _priority(self, held, cls, v) -> int:
        p, mine = self.primary, cls.get(self.primary)
        best = None
        for s in held:
            if s == p:
                continue
            cat = category(mine, cls[s])
            rank = pair_priority(
                cat,
                reference=s in self.refs,
                judged=v is not None,
                same=bool(v and mine and cls[s] and v[mine][1] == v[cls[s]][1]),
                p=v[mine][0] if v and mine else None,
                s=v[cls[s]][0] if v and cls[s] else None,
                corroborated=any(cls[o] == mine for o in held if o not in {p, s}),
            )
            best = rank if best is None else min(best, rank)
        if best is None:
            best = LOW if mine else NONE
        conflict = p in held and held[p][3]
        return MEDIUM if best == LOW and conflict else best

    def _line(self, key, match, title, held, cls, v, verdict, miss, priority) -> list:
        mine = cls.get(self.primary)
        groups = ""
        if v:
            by_group: dict[int, list[str]] = {}
            for label in self.labels:
                if cls.get(label):
                    by_group.setdefault(v[cls[label]][1], []).append(label)
            groups = " | ".join(",".join(by_group[g]) for g in sorted(by_group))
        ref_valued = any(cls.get(r) for r in self.refs)
        return [
            *key,
            match,
            title,
            status(cls, self.contenders),
            *((NA if held[s][0] is None else held[s][0]) if s in held else "" for s in self.labels),
            *(held[c][k] or "" if c in held else "" for c in self.contenders for k in (1, 2)),
            "true" if held[self.primary][3] else "",
            miss if mine is None and ref_valued else "",
            *(v[cls[s]][0] if v and cls.get(s) else "" for s in self.labels),
            groups,
            verdict.reason if verdict else "",
            PRIORITIES[priority],
        ]

    def _record_line(self, key, match, data, in_scope, refs_here) -> tuple | None:
        """Rows-mode record line for a row the primary lacks or holds alone."""
        others = [c for c in self.contenders if c != self.primary and c in data]
        n_other = len(self.contenders) - 1
        if self.primary not in data:
            if len(others) >= 2 and 2 * len(others) > n_other:
                priority = HIGH
            elif others or refs_here:
                priority = MEDIUM if others else LOW
            else:
                return None
            return self._record(
                key, match, "missing_row", priority, data, "extraction" if refs_here else ""
            )
        if others or refs_here or not (n_other or in_scope):
            return None
        return self._record(key, match, "extra_row", HIGH if n_other >= 2 else MEDIUM, data, "")

    def _record(self, key, match, state, priority, data, miss) -> tuple[int, int, list]:
        self.review[PRIORITIES[priority]] += 1

        def compact(rows: list, part: int) -> str:
            if part == 0:
                shown = [
                    {
                        t: NA if e[0] is None else e[0]
                        for t, e in zip(self.titles, row, strict=True)
                        if e is not None
                    }
                    for row in rows
                ]
            else:
                shown = [
                    next((e[part] for e in row if e is not None and e[part]), None) for row in rows
                ]
                if len(shown) == 1:
                    return shown[0] or ""
            return json.dumps(shown, ensure_ascii=False, separators=(",", ":"))

        line = [
            *key,
            match,
            ALL,
            state,
            *(compact(data[s], 0) if s in data else "" for s in self.labels),
            *(compact(data[c], k) if c in data else "" for c in self.contenders for k in (1, 2)),
            "",
            miss,
            *("" for _ in self.labels),
            "",
            "",
            PRIORITIES[priority],
        ]
        return -1, priority, line

    # -- summary ------------------------------------------------------------
    def summary(self, quality: dict, align_stats: dict, min_accuracy: float | None) -> dict:
        bases = [CONSENSUS] * self.consensus + self.refs or [""]
        models = {}
        for x in self.contenders:
            per = {t: {b: self._metrics(x, (t,), b) for b in bases} for t in self.titles}
            overall = {b: self._metrics(x, self.titles, b) for b in bases}
            models[x] = {"columns": per, "overall": overall, "quality": quality.get(x, {})}
        agreement = {"overall": {}, "columns": {t: {} for t in self.titles}}
        for a, b in combinations(self.labels, 2):
            pair = f"{a}|{b}"
            total = Counter()
            for t in self.titles:
                total.update(self.agree[pair, t])
                agreement["columns"][t][pair] = _agreement(self.agree[pair, t], rows=None)
            agreement["overall"][pair] = _agreement(
                total, rows=None if self.roster else self.agree[pair, None]
            )
        out_of_scope = dict(align_stats.get("out_of_scope", {}))
        no_docs = (
            {"kind": "target", "count": self.no_docs, "keys": self.no_docs_keys}
            if self.roster
            else {
                "kind": "scope",
                "count": len(self.out_scopes),
                "keys": [list(s) for s in sorted(self.out_scopes)[:10]],
            }
        )
        summary = {
            "columns": {
                "scored": list(self.columns.scored),
                "focus": list(self.focus),
                "titles": dict(self.columns.titles),
                "unscored": {r: list(f) for r, f in self.columns.unscored.items()},
            },
            "rows": dict(align_stats),
            "models": models,
            "agreement": agreement,
            "review": {
                **{p: self.review[p] for p in PRIORITIES},
                "high_by_column": {t: self.high[t] for t in self.titles},
            },
            "diagnosis": {
                "columns": {
                    t: {
                        k: self.diag[t][k]
                        for k in (
                            "missed_extraction",
                            "missed_no_docs",
                            "wrong",
                            "candidate_updates",
                        )
                    }
                    for t in self.titles
                },
                "rows_missed": self.rows_missed,
                "no_docs": no_docs,
                "out_of_scope": out_of_scope,
            },
        }
        if min_accuracy is not None:
            refs = {}
            for r in self.refs:
                c = models[self.primary]["overall"][r]
                ok = bool(c["cells"]) and c["correct_j"] / c["cells"] >= min_accuracy
                refs[r] = {
                    "accuracy_j": c["accuracy_j"],
                    "out_of_scope": out_of_scope.get(r, 0),
                    "passed": ok and not out_of_scope.get(r),
                }
            summary["gate"] = {
                "min_accuracy": min_accuracy,
                "references": refs,
                "passed": all(r["passed"] for r in refs.values()),
            }
        return summary

    def _metrics(self, x: str, titles: Sequence[str], base: str) -> dict:
        c = Counter()
        for t in titles:
            c.update(self.model[x, t])
            c.update(self.cells[x, t, base])
        names = list(MODEL_COUNTERS)
        if base:
            names += CELL_COUNTERS
            if not self.roster:
                c.update(self.rows[x, base])
                names += ROW_COUNTERS
        if base in self.refs:
            names += ACCURACY_COUNTERS
        m = {n: c[n] for n in names}
        m["compared"] = sum(c[k] for k in CATEGORIES) if base else 0
        m["coverage"] = _rate(c["filled"], c["rows"])
        m["support_rate"] = _rate(c["supported"], c["judged"])
        if not base:
            return m
        ci = {}
        for s in ("", "_j"):
            tp, fp, fn = c[f"tp{s}"], c[f"fp{s}"], c[f"fn{s}"]
            m[f"precision{s}"], m[f"recall{s}"] = _rate(tp, tp + fp), _rate(tp, tp + fn)
            m[f"f1{s}"] = _rate(2 * tp, 2 * tp + fp + fn)
            ci[f"precision{s}"], ci[f"recall{s}"] = wilson(tp, tp + fp), wilson(tp, tp + fn)
            if base in self.refs:
                key = "correct_j" if s else "correct"
                m[f"accuracy{s}"] = _rate(c[key], c["cells"])
                ci[f"accuracy{s}"] = wilson(c[key], c["cells"])
        if not self.roster:
            tp, fp, fn = c["row_tp"], c["row_fp"], c["row_fn"]
            m.update(
                row_precision=_rate(tp, tp + fp),
                row_recall=_rate(tp, tp + fn),
                row_f1=_rate(2 * tp, 2 * tp + fp + fn),
            )
        m["ci95"] = ci
        return m


def _row(c: Counter, x: bool, base: bool) -> None:
    c["row_tp"] += x and base
    c["row_fp"] += x and not base
    c["row_fn"] += base and not x


def _count(c: Counter, cat: str, x: str | None, base: str | None, v: dict | None) -> bool:
    """Raw and judged P/R/F1 counters; returns whether a changed cell is judged one group."""
    c[cat] += 1
    wrong_x, wrong_base = cat in {"changed", "new"}, cat in {"changed", "missing"}
    c["tp"] += cat == "agree"
    c["fp"] += wrong_x
    c["fn"] += wrong_base
    same = bool(v) and cat == "changed" and v[x][1] == v[base][1]
    if cat == "agree" or same:
        c["tp_j"] += 1
    else:
        c["fp_j"] += wrong_x and not (v and v[x][0] == "supported")
        c["fn_j"] += wrong_base and not (v and v[base][0] == "unsupported")
    return same


def _agreement(c: Counter, rows: Counter | None) -> dict:
    m = {k: c[k] for k in ("both_valued", "agree", "judged_same", "one_sided")}
    m["rate"] = _rate(c["agree"], c["both_valued"])
    m["rate_judged"] = _rate(c["agree"] + c["judged_same"], c["both_valued"])
    if rows is not None:
        m.update({k: rows[k] for k in ("matched", "only_a", "only_b")})
        m["jaccard"] = _rate(rows["matched"], rows["matched"] + rows["only_a"] + rows["only_b"])
    return m


def _summary_rows(summary: dict, sources: Sequence[Source]) -> Iterator[list]:
    yield list(SUMMARY_COLUMNS)
    for source in sources:
        model = summary["models"].get(source.label)
        if model is None:
            continue
        for column, per in [*model["columns"].items(), (ALL, model["overall"])]:
            for base, m in per.items():
                extra = model["quality"] if column == ALL else {}
                row = {
                    "label": source.label,
                    "model": source.model,
                    "role": source.role,
                    "column": column,
                    "base": base,
                    **m,
                    **{k: extra.get(k) for k in QUALITY_COLUMNS},
                }
                yield [row.get(k) for k in SUMMARY_COLUMNS]


def leaderboard(summary: dict, title: str) -> list[str]:
    """The printed leaderboard: contenders, per-field lines, gate, review and next step."""
    models = summary["models"]
    labels = list(models)
    bases = list(models[labels[0]]["overall"])
    refs = [b for b in bases if b not in {CONSENSUS, ""}]
    titles = [summary["columns"]["titles"][f] for f in summary["columns"]["focus"]]
    inputs = summary.get("inputs") or {}
    judged = summary.get("judge") is not None

    def score(per: dict) -> float | None:
        accuracy = [per[r]["accuracy_j"] for r in refs if per[r]["accuracy_j"] is not None]
        if accuracy:
            return sum(accuracy) / len(accuracy)
        if CONSENSUS in per and per[CONSENSUS]["f1_j"] is not None:
            return per[CONSENSUS]["f1_j"]
        return next(iter(per.values()))["support_rate"]

    def ranked(scores: dict[str, float | None]) -> list[str]:
        return sorted(scores, key=lambda label: (scores[label] is None, -(scores[label] or 0)))

    def fmt(value: float | None, digits: int = 2) -> str:
        return "—" if value is None else f"{value:.{digits}f}"

    references = inputs.get("references") or {}
    head = [title, f"{len(labels)} contenders"]
    if refs:
        head.append(
            "refs "
            + ", ".join(
                f"{r}({references[r]['rows']})" if "rows" in references.get(r, {}) else r
                for r in refs
            )
        )
    if judged:
        head.append(f"judge {inputs.get('judge') or 'on'}")
    head.append("focus " + ", ".join(titles))
    columns = [
        "model",
        *(f"{r} acc±" for r in refs),
        *(["consensus F1"] * (CONSENSUS in bases)),
        *(["support"] * judged),
        "quotes",
        "$/doc",
    ]
    table = [columns]
    for label in ranked({label: score(models[label]["overall"]) for label in labels}):
        overall, quality = models[label]["overall"], models[label]["quality"]
        row = [label]
        for r in refs:
            ci = overall[r]["ci95"]["accuracy_j"]
            half = f" ±{(ci[1] - ci[0]) / 2:.2f}" if ci else ""
            row.append(fmt(overall[r]["accuracy_j"]) + half)
        if CONSENSUS in bases:
            row.append(fmt(overall[CONSENSUS]["f1_j"]))
        if judged:
            row.append(fmt(overall[bases[0]]["support_rate"]))
        row += [fmt(quality.get("quote_fidelity")), fmt(quality.get("cost_per_document"), 3)]
        table.append(row)
    widths = [max(len(row[i]) for row in table) for i in range(len(columns))]
    lines = [
        " · ".join(head),
        *(
            "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
            for row in table
        ),
    ]
    per_field = {t: {label: score(models[label]["columns"][t]) for label in labels} for t in titles}
    if not inputs.get("fields"):
        weakest = [t for t in titles if per_field[t][labels[0]] is not None]
        titles = sorted(weakest, key=lambda t: per_field[t][labels[0]])[:3]
    width = max((len(t) for t in titles), default=0)
    for t in titles:
        scores = per_field[t]
        lines.append(
            f"  {t.ljust(width)}  "
            + " · ".join(f"{label} {fmt(scores[label])}" for label in ranked(scores))
        )
    judge = summary.get("judge") or {}
    if judge.get("failed"):
        lines.append(
            f"Judge: {judge['failed']:,} of {judge['items']:,} items unjudged"
            f" ({judge.get('error') or 'malformed responses'}); rerun to retry"
        )
    gate = summary.get("gate")
    if gate:
        parts = []
        for r, g in gate["references"].items():
            note = (
                ""
                if g["passed"]
                else (
                    f" ({g['out_of_scope']} rows out of scope)" if g["out_of_scope"] else " (below)"
                )
            )
            parts.append(f"{r} {fmt(g['accuracy_j'])}{note}")
        lines.append(
            f"Gate: {'PASS' if gate['passed'] else 'FAIL'}  min_accuracy "
            f"{gate['min_accuracy']} · " + " · ".join(parts)
        )
    review = summary["review"]
    lines.extend(
        (
            (
                f"Review: {review['high']:,} high · {review['medium']:,} medium · "
                f"{review['low']:,} low → validated/cells.csv"
            ),
            "Next: " + (" · ".join(_next_steps(summary, refs)) or "nothing flagged"),
        )
    )
    return lines


def _next_steps(summary: dict, refs: list[str]) -> list[str]:
    diagnosis, steps = summary["diagnosis"], []
    if not refs:
        high = summary["review"]["high_by_column"]
        top = [t for t in sorted(high, key=lambda t: -high[t]) if high[t]][:3]
        return [("review — " + ", ".join(f"{t} {high[t]} high" for t in top))] if top else []
    columns = diagnosis["columns"]
    load = {t: d["missed_extraction"] + d["wrong"] for t, d in columns.items()}
    top = [t for t in sorted(load, key=lambda t: -load[t]) if load[t]][:3]
    if top:
        steps.append(
            "extraction — "
            + ", ".join(
                f"{t} {columns[t]['missed_extraction']} missed / {columns[t]['wrong']} wrong"
                for t in top
            )
        )
    discovery = []
    no_docs = diagnosis["no_docs"]
    if no_docs["count"]:
        shown = ", ".join(" ".join(k) for k in no_docs["keys"][:2])
        more = ", …" if no_docs["count"] > 2 else ""
        what = (
            "targets without documents"
            if no_docs["kind"] == "target"
            else "scopes without contender rows"
        )
        discovery.append(f"{no_docs['count']} {what} ({shown}{more})")
    out_of_scope = sum(diagnosis["out_of_scope"].values())
    if out_of_scope and no_docs["kind"] == "target":
        discovery.append(f"{out_of_scope} reference rows outside the roster")
    if discovery:
        steps.append("discovery — " + ", ".join(discovery))
    updates = sum(d["candidate_updates"] for d in columns.values())
    if updates:
        steps.append(f"{updates} candidate updates")
    return steps
