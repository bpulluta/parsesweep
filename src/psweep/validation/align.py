"""Align contender and reference tables: roster identity or exact keys, then the quote anchor."""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import groupby, zip_longest
from operator import attrgetter, itemgetter
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from psweep.compilation.data_compiler import apply_value_normalizations
from psweep.compilation.roster import NA
from psweep.extraction.roster import Roster, normalize
from psweep.utils.schema_metadata import SchemaMetadata
from psweep.utils.spreadsheet import csv_literal
from psweep.validation.judge import class_key

logger = logging.getLogger(__name__)

MAX_SCOPE_ROWS = 5000
NGRAM = 4
MIN_CHARS = 12
ANCHOR = 0.65
CONTENDERS = ("primary", "challenger")
ROLES = (*CONTENDERS, "reference")
_EVIDENCE = "evidence"
_CHUNK = 20_000


@dataclass(frozen=True)
class Cell:
    value: str | None
    quote: str | None
    url: str | None
    conflict: bool


@dataclass(frozen=True)
class Source:
    label: str
    role: str
    path: Path
    model: str | None


@dataclass(frozen=True)
class Columns:
    scored: tuple[str, ...]
    focus: tuple[str, ...]
    titles: dict[str, str]
    descriptions: dict[str, str]
    unscored: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Cluster:
    key: tuple
    scope: tuple
    match: str
    rows: dict[str, tuple[dict[str, Cell], ...]]
    in_scope: frozenset[str]


class _Row(NamedTuple):
    rank: int
    ordinal: int
    key: str
    display: tuple
    scope: tuple
    cells: dict[str, Cell]
    grams: frozenset[str]


def item_properties(metadata: SchemaMetadata, row_array: str | None) -> dict[str, dict]:
    """Item properties in schema order; a row array is replaced by its child properties."""
    item = metadata.schema["properties"][metadata.get_main_data_array()]["items"]
    properties = {f: p for f, p in item.get("properties", {}).items() if f != row_array}
    if row_array:
        properties.update(item["properties"][row_array]["items"].get("properties", {}))
    return properties


def context_fields(metadata: SchemaMetadata) -> set[str]:
    """Context-object fields; an item field of the same name wins, as in compile."""
    schema = metadata.schema["properties"]
    fields = {
        f
        for context in metadata.get_context_objects()
        for f in schema.get(context, {}).get("properties", {})
    }
    return fields - set(item_properties(metadata, metadata.get_row_array()))


def _header(source: Source) -> set[str]:
    with Path(source.path).open(encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle), None)
    if not header:
        raise ValueError(f"{source.label}: {source.path} has no header row")
    repeated = sorted(title for title, n in Counter(header).items() if n > 1)
    if repeated:
        raise ValueError(
            f"{source.label}: repeated header(s) {', '.join(repeated)} in {source.path}"
        )
    return set(header)


def _clean(raw: str) -> str:
    """Undo compile's formula escaping (``csv_literal``); NA becomes ''."""
    text = raw[1:] if raw[:1] == "'" and csv_literal(raw[1:]) == raw else raw
    return "" if text.strip() in ("", NA) else text


def _frames(
    source: Source,
    fields: Sequence[str],
    titles: dict[str, str],
    metadata: SchemaMetadata | None = None,
) -> Iterator[pd.DataFrame]:
    """Stream cleaned field-name frames; ``metadata`` applies rows-mode value normalization."""
    names = {titles.get(f, f): f for f in fields}
    with pd.read_csv(
        source.path,
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
        usecols=list(names),
        chunksize=_CHUNK,
    ) as reader:
        for chunk in reader:
            chunk = chunk.rename(columns=names).map(_clean)
            yield apply_value_normalizations(chunk, metadata) if metadata else chunk


def _part(value: str) -> str:
    """Key part: canonical number (``500`` = ``500.0``) or ``normalize``d text."""
    number = class_key(value)
    return number if number.startswith("number:") else normalize(value)


def _grams(quote: str | None) -> frozenset[str]:
    """Lowercased, whitespace-collapsed char n-grams; empty below MIN_CHARS."""
    if not quote or len(quote.strip()) < MIN_CHARS:
        return frozenset()
    text = " ".join(quote.lower().split())
    return frozenset(text[i : i + NGRAM] for i in range(len(text) - NGRAM + 1))


def _containment(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / min(len(a), len(b))


def columns(
    metadata: SchemaMetadata,
    roster: Roster | None,
    *,
    key_fields: Sequence[str],
    quote_field: str | None,
    fields: Sequence[str] | None,
    references: Sequence[Source],
) -> Columns:
    """Scored and focus fields, compiled headers, and the reference header checks."""
    properties = item_properties(metadata, None if roster else metadata.get_row_array())
    keys = roster.fields if roster else tuple(key_fields)
    unknown = sorted(set(keys) - set(metadata.get_output_fields()))
    if unknown:
        raise ValueError(f"unknown key field(s): {', '.join(unknown)}")
    known = metadata.get_column_titles()
    excluded = set(metadata.get_output_exclude_fields())
    skip = {*keys, quote_field}
    scored = tuple(f for f in properties if f not in skip and known.get(f, f) not in excluded)
    unknown = sorted(set(fields or ()) - set(scored))
    if unknown:
        raise ValueError(f"field(s) not scorable: {', '.join(unknown)}")
    chosen = set(fields or scored)
    focus = tuple(f for f in scored if f in chosen)
    titles = {f: known.get(f, f) for f in (*keys, *scored, *([quote_field] if quote_field else []))}
    unscored: dict[str, tuple[str, ...]] = {}
    for source in references:
        header = _header(source)
        missing = [titles[f] for f in keys if titles[f] not in header]
        if missing:
            raise ValueError(f"{source.label}: missing key column(s) {', '.join(missing)}")
        absent = tuple(f for f in focus if titles[f] not in header)
        if len(absent) == len(focus):
            raise ValueError(
                f"{source.label}: no focus column ({', '.join(titles[f] for f in focus)})"
            )
        if absent:
            logger.warning(
                "%s: lacks %s; unscored for this reference",
                source.label,
                ", ".join(titles[f] for f in absent),
            )
        unscored[source.label] = absent
        if roster:
            _roster_reference(source, roster, [f for f in focus if f not in absent], titles)
    descriptions = {f: properties[f].get("description", "") for f in scored}
    return Columns(scored, focus, titles, descriptions, unscored)


def align(
    sources: Sequence[Source],
    *,
    metadata: SchemaMetadata,
    roster: Roster | None,
    key_fields: Sequence[str],
    quote_field: str | None,
    columns: Columns,
    staging: sqlite3.Connection,
    stats: dict,
) -> Iterator[Cluster]:
    """Validate headers and stage rows now; return the cluster stream (fills ``stats``)."""
    roles = sorted({s.role for s in sources} - set(ROLES))
    if roles:
        raise ValueError(f"unknown source role(s): {', '.join(roles)}")
    if not any(s.role in CONTENDERS for s in sources):
        raise ValueError("align needs at least one primary or challenger source")
    stats.update(
        match={"key": 0, "anchor": 0},
        ambiguous={s.label: 0 for s in sources},
        out_of_scope={s.label: 0 for s in sources if s.role == "reference"},
        clusters=0,
    )
    keys = roster.fields if roster else tuple(key_fields)
    present = []
    for source in sources:
        header = _header(source)
        contender = source.role in CONTENDERS
        required = [*keys, *(columns.focus if contender else ())]
        if quote_field and contender:
            required.append(quote_field)
        missing = [columns.titles[f] for f in required if columns.titles[f] not in header]
        if missing:
            raise ValueError(
                f"{source.label}: missing column(s) {', '.join(missing)} in {source.path}"
            )
        present.append((header, [f for f in columns.focus if columns.titles[f] in header]))
    if roster:
        references = {
            rank: _roster_reference(s, roster, present[rank][1], columns.titles)
            for rank, s in enumerate(sources)
            if s.role == "reference"
        }
        ranks = [rank for rank, s in enumerate(sources) if s.role in CONTENDERS]
        streams = [_roster_contender(sources[r], roster, columns, present[r][0]) for r in ranks]
        return _roster_stream(sources, roster, ranks, streams, references, stats)
    scope_at = [i for i, f in enumerate(keys) if f in context_fields(metadata)]
    _stage(sources, metadata, keys, quote_field, columns, present, scope_at, staging)
    if quote_field:
        _check_scopes(sources, staging, scope_at)
    return _rows_clusters(sources, quote_field, scope_at, staging, stats)


def _roster_key(roster: Roster, display: tuple) -> tuple:
    return roster.key(dict(zip(roster.fields, display, strict=True)))


def _roster_reference(
    source: Source,
    roster: Roster,
    focus: Sequence[str],
    titles: dict[str, str],
) -> dict[tuple, tuple[tuple, dict[str, Cell]]]:
    """``{roster key: (display, cells)}``; a repeated target is an error."""
    rows: dict[tuple, tuple[tuple, dict[str, Cell]]] = {}
    for frame in _frames(source, [*roster.fields, *focus], titles):
        for row in frame.to_dict("records"):
            display = tuple(row[f] for f in roster.fields)
            key = _roster_key(roster, display)
            if key in rows:
                raise ValueError(
                    f"{source.label}: duplicate roster target {display} in {source.path}"
                )
            rows[key] = display, {f: Cell(row[f] or None, None, None, False) for f in focus}
    return rows


def _roster_contender(
    source: Source,
    roster: Roster,
    columns: Columns,
    header: set[str],
) -> Iterator[tuple[tuple, tuple, dict[str, Cell]]]:
    evidence = [_EVIDENCE] if _EVIDENCE in header else []
    for frame in _frames(source, [*roster.fields, *columns.focus, *evidence], columns.titles):
        for row in frame.to_dict("records"):
            display = tuple(row[f] for f in roster.fields)
            found = json.loads(row[_EVIDENCE]) if evidence and row[_EVIDENCE] else {}
            cells = {}
            for f in columns.focus:
                item = found.get(f) or {}
                cells[f] = Cell(
                    row[f] or None, item.get("quote"), item.get("url"), bool(item.get("conflict"))
                )
            yield _roster_key(roster, display), display, cells


def _roster_stream(
    sources: Sequence[Source],
    roster: Roster,
    ranks: list[int],
    streams: list[Iterator],
    references: dict[int, dict],
    stats: dict,
) -> Iterator[Cluster]:
    """Contenders in lockstep with the roster: one row per target, in roster order."""
    targets = zip_longest(roster.targets.values(), *streams)
    for position, (identity, *entries) in enumerate(targets, start=1):
        for rank, entry in zip(ranks, entries, strict=True):
            if (entry is None) != (identity is None):
                raise ValueError(
                    f"{sources[rank].label}: {'fewer' if entry is None else 'more'} rows than the "
                    f"roster's {len(roster.targets)} targets; recompile it for the current roster"
                )
            if entry and entry[0] != roster.key(identity):
                expected = tuple(identity.get(f) for f in roster.fields)
                raise ValueError(
                    f"{sources[rank].label}: row {position} is {entry[1]}, not {expected}; "
                    "recompile it for the current roster"
                )
        key, display, _ = entries[0]
        held = dict(zip(ranks, entries, strict=True))
        for rank, rows in references.items():
            if key in rows:
                held[rank] = rows.pop(key)
        clustered = {sources[rank].label: (held[rank][-1],) for rank in sorted(held)}
        stats["match"]["key"] += len(clustered) >= 2
        stats["clusters"] += 1
        in_scope = frozenset(sources[r].label for r in references if r in held)
        yield Cluster(display, display, "key", clustered, in_scope)
    for rank, rows in references.items():
        label = sources[rank].label
        for display, cells in rows.values():
            stats["out_of_scope"][label] += 1
            yield Cluster(display, display, "out_of_scope", {label: (cells,)}, frozenset({label}))


def _stage(
    sources: Sequence[Source],
    metadata: SchemaMetadata,
    keys: Sequence[str],
    quote_field: str | None,
    columns: Columns,
    present: list[tuple[set[str], list[str]]],
    scope_at: list[int],
    staging: sqlite3.Connection,
) -> None:
    """Stream every source into ``rows`` (rebuilt each run)."""
    staging.execute("DROP TABLE IF EXISTS rows")
    staging.execute(
        "CREATE TABLE rows (source INTEGER NOT NULL, ordinal INTEGER NOT NULL, "
        "scope TEXT NOT NULL, key TEXT NOT NULL, cells_json TEXT NOT NULL)"
    )
    for rank, (source, (header, focus)) in enumerate(zip(sources, present, strict=True)):
        quote = [quote_field] if quote_field and columns.titles[quote_field] in header else []
        normalized = metadata if source.role == "reference" else None
        ordinal = 0
        for frame in _frames(source, [*keys, *focus, *quote], columns.titles, normalized):
            batch = []
            for row in frame.to_dict("records"):
                display = [row[f] for f in keys]
                parts = [_part(value) for value in display]
                cells = [
                    display,
                    {f: row[f] or None for f in focus},
                    row[quote[0]] or None if quote else None,
                ]
                batch.append(
                    (
                        rank,
                        ordinal,
                        json.dumps([parts[i] for i in scope_at]),
                        json.dumps(parts),
                        json.dumps(cells, ensure_ascii=False),
                    )
                )
                ordinal += 1
            staging.executemany("INSERT INTO rows VALUES (?, ?, ?, ?, ?)", batch)
    staging.execute("CREATE INDEX rows_order ON rows (scope, key, source, ordinal)")
    staging.commit()


def _check_scopes(
    sources: Sequence[Source], staging: sqlite3.Connection, scope_at: list[int]
) -> None:
    """The anchor is O(L²) per scope: refuse a contender scope above MAX_SCOPE_ROWS."""
    contenders = ",".join(str(r) for r, s in enumerate(sources) if s.role in CONTENDERS)
    found = staging.execute(
        "SELECT scope, COUNT(*) FROM rows GROUP BY scope "
        f"HAVING COUNT(*) > ? AND SUM(source IN ({contenders})) > 0 ORDER BY scope LIMIT 1",
        (MAX_SCOPE_ROWS,),
    ).fetchone()
    if found:
        (cells,) = staging.execute(
            "SELECT cells_json FROM rows WHERE scope = ? LIMIT 1", (found[0],)
        ).fetchone()
        display = json.loads(cells)[0]
        raise ValueError(
            f"scope ({', '.join(display[i] for i in scope_at)}) has {found[1]} rows "
            f"(> {MAX_SCOPE_ROWS}): scope too broad; add a context field to validation.key_fields"
        )


def _rows_clusters(
    sources: Sequence[Source],
    quote_field: str | None,
    scope_at: list[int],
    staging: sqlite3.Connection,
    stats: dict,
) -> Iterator[Cluster]:
    """One ordered cursor, a scope at a time; scope membership comes from a parallel cursor."""
    references = {r for r, s in enumerate(sources) if s.role == "reference"}
    labels = [s.label for s in sources]
    scopes = staging.execute(
        "SELECT scope, GROUP_CONCAT(DISTINCT source) FROM rows GROUP BY scope ORDER BY scope"
    )
    cursor = staging.execute(
        "SELECT scope, key, source, ordinal, cells_json FROM rows ORDER BY scope, key, source, ordinal"
    )
    for (_, members), (_, group) in zip(scopes, groupby(cursor, key=itemgetter(0)), strict=True):
        ranks = {int(r) for r in members.split(",")}
        in_scope = frozenset(labels[r] for r in ranks & references)
        rows = (_load(record, references, quote_field, scope_at) for record in group)
        if not ranks - references:
            for row in rows:
                stats["out_of_scope"][labels[row.rank]] += 1
                yield _cluster(labels, "out_of_scope", {row.rank: (row,)}, in_scope)
        elif quote_field:
            yield from _resolve(list(rows), ranks, labels, in_scope, stats, anchor=True)
        else:
            for _, same in groupby(rows, key=attrgetter("key")):
                yield from _resolve(list(same), ranks, labels, in_scope, stats, anchor=False)


def _load(
    record: tuple, references: set[int], quote_field: str | None, scope_at: list[int]
) -> _Row:
    _, key, rank, ordinal, cells_json = record
    display, values, quote = json.loads(cells_json)
    shown = None if rank in references else quote
    cells = {f: Cell(value, shown, None, False) for f, value in values.items()}
    scope = tuple(display[i] for i in scope_at)
    grams = _grams(quote) if quote_field else frozenset()
    return _Row(rank, ordinal, key, tuple(display), scope, cells, grams)


def _lead(members: dict[int, tuple[_Row, ...]]) -> _Row:
    """The first source's first row: it names the cluster and orders it."""
    return min(
        (row for rows in members.values() for row in rows), key=attrgetter("rank", "ordinal")
    )


def _cluster(labels, match, members: dict[int, tuple[_Row, ...]], in_scope) -> Cluster:
    lead = _lead(members)
    rows = {labels[r]: tuple(row.cells for row in members[r]) for r in sorted(members)}
    return Cluster(lead.display, lead.scope, match, rows, in_scope)


def _resolve(
    rows: list[_Row],
    ranks: set[int],
    labels: list[str],
    in_scope: frozenset[str],
    stats: dict,
    *,
    anchor: bool,
) -> Iterator[Cluster]:
    """Exact key, then (``anchor``) quote anchoring; leftover duplicates become ``ambiguous``."""
    complete, units, ambiguous = [], [], []
    for _, same in groupby(rows, key=attrgetter("key")):
        by_rank: dict[int, list[_Row]] = {}
        for row in same:
            by_rank.setdefault(row.rank, []).append(row)
        single = {r: held[0] for r, held in by_rank.items() if len(held) == 1}
        if single:
            (units if anchor and set(single) != ranks else complete).append(single)
        ambiguous += [row for held in by_rank.values() if len(held) > 1 for row in held]
    clusters = [("key", unit) for unit in complete]
    if anchor:
        keyed = len(units)
        units += [{row.rank: row} for row in ambiguous]
        ambiguous = []
        for group in _anchor(units):
            if len(group) > 1:
                clusters.append(("anchor", {r: row for i in group for r, row in units[i].items()}))
            elif group[0] < keyed:
                clusters.append(("key", units[group[0]]))
            else:
                ambiguous += units[group[0]].values()
    found = [(match, {r: (row,) for r, row in unit.items()}) for match, unit in clusters]
    by_key = attrgetter("key")
    for _, same in groupby(sorted(ambiguous, key=by_key), key=by_key):
        members: dict[int, list[_Row]] = {}
        for row in sorted(same, key=attrgetter("rank", "ordinal")):
            members.setdefault(row.rank, []).append(row)
        found.append(("ambiguous", {r: tuple(held) for r, held in members.items()}))
    found.sort(key=lambda item: attrgetter("key", "rank", "ordinal")(_lead(item[1])))
    for match, members in found:
        if match == "ambiguous":
            for r, held in members.items():
                stats["ambiguous"][labels[r]] += len(held)
        elif match == "anchor" or len(members) >= 2:
            stats["match"][match] += 1
        stats["clusters"] += 1
        yield _cluster(labels, match, members, in_scope)


def _anchor(units: list[dict[int, _Row]]) -> list[list[int]]:
    """Single-linkage merge by best quote containment ≥ ANCHOR, one row per source per cluster."""
    grams = [[row.grams for row in unit.values() if row.grams] for unit in units]
    order = [min((row.rank, row.ordinal) for row in unit.values()) for unit in units]
    quoted = [i for i in range(len(units)) if grams[i]]
    pairs = []
    for n, i in enumerate(quoted):
        for j in quoted[n + 1 :]:
            if units[i].keys() & units[j].keys():
                continue
            score = max(_containment(a, b) for a in grams[i] for b in grams[j])
            if score >= ANCHOR:
                first, second = sorted((i, j), key=order.__getitem__)
                pairs.append((-score, order[first], order[second], first, second))
    parent = list(range(len(units)))
    sources = [set(unit) for unit in units]

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for *_, i, j in sorted(pairs):
        a, b = root(i), root(j)
        if a != b and not sources[a] & sources[b]:
            parent[b] = a
            sources[a] |= sources[b]
    groups: dict[int, list[int]] = {}
    for i in range(len(units)):
        groups.setdefault(root(i), []).append(i)
    return list(groups.values())
