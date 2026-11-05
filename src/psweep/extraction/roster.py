"""Roster-anchored candidate matching (the single target matcher) and quote verification."""

from __future__ import annotations

import functools
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from psweep.config import RuntimeConfigError
from psweep.discovery.targets import identify_target, iter_targets, target_identity
from psweep.extraction.section_locator import split_text_sections
from psweep.utils.normalizers import US_STATES

WINDOW_CHARS = 2_000
WINDOW_CONTEXT_CHARS = 400
_PUNCTUATION = re.compile(r"[^\w\s]|_")
_LINE_HYPHEN = re.compile(r"(\w)[-\u2010\u2011]\s*\n\s*(?=\w)")
_ADDRESS_STATE = re.compile(r",\s*([A-Z]{2})\b|\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")
# A row cell whose label names a state ("State: CA", "state_abbr: CA").
_LABELLED_STATE = re.compile(
    r"(?:^|\|)[^|:\n]*?(?<![A-Za-z])(?i:state)(?![A-Za-z])[^|:\n]*:[ \t]*([A-Z]{2})[ \t]*(?=\||$)",
    re.M,
)
_STATE_CODES = frozenset(US_STATES.values())
_ELLIPSIS = re.compile(r"\u2026|\.{3}")


def normalize(text: Any) -> str:
    """NFKD, strip marks and soft hyphens, join line-break hyphenation,
    casefold, punctuation to space, collapse whitespace."""
    text = _LINE_HYPHEN.sub(r"\1", str(text).replace("\u00ad", ""))
    text = unicodedata.normalize("NFKD", text)
    if not text.isascii():
        text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(_PUNCTUATION.sub(" ", text.casefold()).split())


def _ngram_index(names: Iterable[tuple[str, Any]]) -> dict[tuple[str, ...], list]:
    index: dict[tuple[str, ...], list] = {}
    for name, value in names:
        tokens = tuple(normalize(name).split())
        if tokens:
            index.setdefault(tokens, []).append(value)
    return index


def _lookup(tokens: Sequence[str], index: dict, longest: int, heads: set[str]):
    for start, token in enumerate(tokens):
        if token not in heads:
            continue
        for size in range(1, min(longest, len(tokens) - start) + 1):
            yield from index.get(tuple(tokens[start : start + size]), ())


_STATE_NAMES = _ngram_index(
    (name, code) for name, code in US_STATES.items() if len(name) > 2
)
_STATE_LONGEST = max(map(len, _STATE_NAMES))
_STATE_HEADS = {tokens[0] for tokens in _STATE_NAMES}


def _state_code(value: Any) -> str | None:
    return US_STATES.get(str(value or "").strip().lower())


@dataclass(frozen=True)
class Roster:
    fields: tuple[str, ...]
    targets: dict[str, dict[str, Any]]
    index: dict[tuple[str, ...], list[str]]
    longest: int
    heads: frozenset[str]
    ambiguous: dict[str, frozenset[str]]

    @classmethod
    def build(cls, rows: Iterable[dict[str, Any]], fields: Sequence[str]) -> Roster:
        targets = {}
        for row in rows:
            identity = target_identity(row, fields)
            targets[identify_target(row, fields)] = identity
        index = _ngram_index(
            (identity[fields[-1]], target_id) for target_id, identity in targets.items()
        )
        ambiguous = {
            target_id: frozenset(
                _state_code(targets[target_id][field]) for field in fields[:-1]
            )
            - {None}
            for target_ids in index.values()
            if len(target_ids) > 1
            for target_id in target_ids
        }
        return cls(
            fields=tuple(fields),
            targets=targets,
            index=index,
            longest=max(map(len, index), default=0),
            heads=frozenset(tokens[0] for tokens in index),
            ambiguous=ambiguous,
        )

    def key(self, item: dict[str, Any]) -> tuple:
        return tuple(normalize(item.get(field) or "") for field in self.fields)

    def _name_at(self, tokens: Sequence[str], start: int) -> int:
        if tokens[start] not in self.heads:
            return 0
        for size in range(min(self.longest, len(tokens) - start), 0, -1):
            if tuple(tokens[start : start + size]) in self.index:
                return size
        return 0

    def states(self, text: str) -> set[str]:
        """State evidence: full names outside roster-name spans, codes in
        addresses, in state-labelled cells, or alone on the line after a line
        that is exactly a roster name (card layouts)."""
        found = {
            code
            for match in _ADDRESS_STATE.finditer(text)
            if (code := match.group(1) or match.group(2)) in _STATE_CODES
        }
        found.update(
            code for match in _LABELLED_STATE.finditer(text) if (code := match.group(1)) in _STATE_CODES
        )
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        found.update(
            line
            for previous, line in pairwise(lines)
            if line in _STATE_CODES and tuple(normalize(previous).split()) in self.index
        )
        tokens, segment, start = normalize(text).split(), [], 0
        while start < len(tokens):
            size = self._name_at(tokens, start)
            if size:
                found.update(_lookup(segment, _STATE_NAMES, _STATE_LONGEST, _STATE_HEADS))
                segment, start = [], start + size
            else:
                segment.append(tokens[start])
                start += 1
        found.update(_lookup(segment, _STATE_NAMES, _STATE_LONGEST, _STATE_HEADS))
        return found


def roster_fields(
    inputs: dict[str, Any], key_fields: Sequence[str]
) -> tuple[str, ...] | None:
    """Roster mode is on iff targets exist and key_fields equal partition_by."""
    partition_by = inputs.get("partition_by") or []
    if not inputs.get("targets") or not partition_by:
        return None
    if set(key_fields) == set(partition_by):
        return tuple(partition_by)
    if set(key_fields) & set(partition_by):
        message = (
            "identity.deduplication.key_fields partially overlaps "
            f"discovery.partition_by ({sorted(key_fields)} vs {sorted(partition_by)}); "
            "make them equal for roster extraction or disjoint otherwise"
        )
        raise RuntimeConfigError(message)
    return None


def load_roster(inputs: dict[str, Any], metadata) -> Roster | None:
    """Load the discovery target roster when roster mode is derived."""
    fields = roster_fields(inputs, metadata.get_deduplication_key_fields())
    if fields is None:
        return None
    array = metadata.get_main_data_array()
    items = metadata.schema.get("properties", {}).get(array, {}).get("items", {})
    missing = [field for field in fields if field not in items.get("properties", {})]
    if missing:
        message = f"Roster identity fields must be top-level '{array}' item fields: {missing}"
        raise RuntimeConfigError(message)
    return Roster.build(iter_targets(inputs["targets"]), fields)


def _reach(lines: Sequence[str], number: int, step: int) -> int:
    """Farthest line from ``number`` within ``WINDOW_CONTEXT_CHARS`` of non-blank text.

    The nearest non-blank neighbour is always kept, so card layouts (name, then
    label/value lines) and long table rows both keep their context.
    """
    index, used = number, 0
    while 0 <= index + step < len(lines):
        size = len(lines[index + step].strip())
        if used and used + size > WINDOW_CONTEXT_CHARS:
            break
        index += step
        used += size
    return index


def _spans(text: str, roster: Roster):
    """Yield ``(span, [(hits, context), ...])``; each source line is in at most one span."""
    for chunk in split_text_sections(text):
        body = chunk.to_text()
        lines = body.splitlines()
        hits = [
            (number, ids)
            for number, line in enumerate(lines)
            if (
                ids := set(
                    _lookup(normalize(line).split(), roster.index, roster.longest, roster.heads)
                )
            )
        ]
        if not hits:
            continue
        if len(body) <= WINDOW_CHARS:
            yield body, [(ids, body) for _, ids in hits]
            continue
        group: list[tuple[set[str], str]] = []
        start = end = 0
        for number, ids in hits:
            low, high = _reach(lines, number, -1), _reach(lines, number, 1)
            if group and low > end + 1:
                yield "\n".join(lines[start : end + 1]), group
                group = []
            if not group:
                start = low
            end = high
            group.append((ids, "\n".join(lines[low : high + 1])))
        yield "\n".join(lines[start : end + 1]), group


def scan_candidates(text: str, roster: Roster) -> list[tuple[str, set[str]]]:
    """Return ordered unique spans with the state-disambiguated targets they hold."""
    context_states: dict[str, set[str]] = {}
    document_states: set[str] | None = None
    found: dict[str, set[str]] = {}
    for span, group in _spans(text, roster):
        kept: set[str] = set()
        for hits, context in group:
            unique = hits - roster.ambiguous.keys()
            kept |= unique
            if unique == hits:
                continue
            if context not in context_states:
                context_states[context] = roster.states(context)
            states = context_states[context]
            if not states:
                if document_states is None:
                    document_states = roster.states(text)
                states = document_states
            kept |= {
                target_id
                for target_id in hits - unique
                if not states
                or not roster.ambiguous[target_id]
                or roster.ambiguous[target_id] & states
            }
        if kept:
            found.setdefault(span, set()).update(kept)
    return list(found.items())


def _pieces(text: str, budget: int):
    """Split ``text`` at line boundaries into parts no longer than ``budget``."""
    while len(text) > budget:
        cut = text.rfind("\n", 0, budget)
        cut = cut if cut > 0 else budget
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    yield text


def batch(candidates: list[tuple[str, set[str]]], budget: int) -> list[tuple[str, list[str]]]:
    """Group spans in document order so each call's text fits ``budget``."""
    batches: list[tuple[list[str], set[str]]] = []
    size = budget
    for span, target_ids in candidates:
        for piece in _pieces(span, budget):
            if size + len(piece) > budget:
                batches.append(([], set()))
                size = 0
            batches[-1][0].append(piece)
            batches[-1][1].update(target_ids)
            size += len(piece) + 2
    return [("\n\n".join(texts), sorted(ids)) for texts, ids in batches]


def candidate_hash(roster: Roster, target_ids: Iterable[str]) -> str:
    identities = sorted(
        json.dumps(roster.targets[target_id], sort_keys=True, default=str)
        for target_id in set(target_ids)
    )
    return hashlib.sha256(json.dumps(identities).encode()).hexdigest()


def candidates_block(roster: Roster, target_ids: Iterable[str]) -> str:
    lines = [
        "=== ROSTER CANDIDATES (added by ParseSweep; not part of the source document) ===",
        "Emit items only for these identities, copying "
        f"{', '.join(roster.fields)} verbatim:",
        *(
            json.dumps(roster.targets[target_id], ensure_ascii=True, default=str)
            for target_id in target_ids
        ),
        "=== END ROSTER CANDIDATES ===",
        "",
    ]
    return "\n".join(lines) + "\n"


def evidence_nodes(node: Any, path: Sequence[str], location: str = ""):
    if not path:
        yield location, node
    elif path[0] == "*":
        for index, child in enumerate(node if isinstance(node, list) else ()):
            yield from evidence_nodes(child, path[1:], f"{location}[{index}]")
    elif isinstance(node, dict) and path[0] in node:
        key = f"{location}.{path[0]}" if location else path[0]
        yield from evidence_nodes(node[path[0]], path[1:], key)


@functools.lru_cache(maxsize=8)
def _haystacks(source_text: str) -> frozenset[str]:
    return frozenset(
        f" {normalize(text)} "
        for text in (source_text, _LINE_HYPHEN.sub(r"\1 ", source_text))
    )


def _in_order(parts: list[str], haystack: str) -> bool:
    start = 0
    for part in parts:
        start = haystack.find(f" {part} ", start)
        if start < 0:
            return False
        start += len(part) + 1
    return True


def quote_present(quote: Any, source_text: str) -> bool:
    """True when the normalized quote occurs, on word boundaries, in the normalized source.

    An elided quote (``head … tail``) matches when its parts occur in order.
    """
    if quote is None:
        return False
    parts = [part for part in map(normalize, _ELLIPSIS.split(str(quote))) if part]
    return bool(parts) and any(_in_order(parts, haystack) for haystack in _haystacks(source_text))


@dataclass(frozen=True)
class QuoteRows:
    """Rows mode: every compiled row carries its verbatim quote in ``field``.

    A row is an ``array`` item, or the item merged with each
    ``row_array`` child.
    """

    array: str
    row_array: str | None
    field: str


def _rejected(location: str, quote: Any) -> str:
    shown = json.dumps(quote[:160], ensure_ascii=False) if isinstance(quote, str) else "null"
    return f"unverified quote at {location}: {shown}"


def _drop_unquoted_rows(data: dict, rows: QuoteRows, source_text: str) -> list[str]:
    """Drop rows whose quote is absent, and items left without rows; one warning per row."""
    warnings, items = [], []
    for index, item in enumerate(data.get(rows.array) or []):
        location = f"{rows.array}[{index}]"
        children = item.get(rows.row_array) if rows.row_array else None
        if not children:
            quote = item.get(rows.field)
            if quote_present(quote, source_text):
                items.append(item)
            else:
                warnings.append(_rejected(f"{location}.{rows.field}", quote))
            continue
        kept = []
        for number, child in enumerate(children):
            quote = {**item, **child}.get(rows.field)
            if quote_present(quote, source_text):
                kept.append(child)
            else:
                where = f"{location}.{rows.row_array}[{number}].{rows.field}"
                warnings.append(_rejected(where, quote))
        if kept:
            items.append({**item, rows.row_array: kept})
    if rows.array in data:
        data[rows.array] = items
    return warnings


def verify_quotes(
    data: dict,
    source_text: str,
    *,
    evidence: Sequence[Sequence[str]] = (),
    rows: QuoteRows | None = None,
) -> list[str]:
    """Drop ``rows`` and null ``evidence`` whose quote is not in the source.

    The one quote check for roster and rows mode. Returns one warning
    per rejection.
    """
    warnings = _drop_unquoted_rows(data, rows, source_text) if rows else []
    for path in evidence:
        for location, node in evidence_nodes(data, path):
            if not isinstance(node, dict):
                continue
            quote = node.get("quote")
            if quote is None and node.get("value") is None:
                continue
            if quote_present(quote, source_text):
                continue
            node["value"] = node["quote"] = None
            warnings.append(_rejected(location, quote))
    return warnings
