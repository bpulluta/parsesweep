"""Roster compile: one row per discovery target, resolved from extracted evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import pandas as pd

from psweep.discovery.targets import TARGET_INPUT_FIELDS, identify_target, iter_targets
from psweep.extraction.roster import Roster
from psweep.extraction.schema_utils import evidence_paths

NA = "NA"
METADATA_COLUMNS = ("sources", "confidence", "evidence")
_CONFIDENCE_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
_NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[+-]?\.\d+")


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def value_key(value: Any) -> tuple[str, Any]:
    """Agreement key: numbers by value; text casefolded with whitespace collapsed.

    Punctuation stays significant, so "> 100" and "< 100" differ while
    300, "300.0" and "300" agree.
    """
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, (int, float)):
        return "number", Decimal(str(value))
    if not isinstance(value, str):
        return "json", json.dumps(value, sort_keys=True, ensure_ascii=False)
    text = " ".join(value.split())
    if _NUMBER.fullmatch(text):
        return "number", Decimal(text.replace(",", ""))
    return "text", text.casefold()


def _resolve(candidates: list[dict[str, Any]]) -> tuple[dict, str, list[dict], list[str]]:
    """Pick a cell value: agreement wins; on conflict target scope beats shared scope."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        groups.setdefault(value_key(candidate["value"]), []).append(candidate)
    if len(groups) == 1:
        (chosen,) = groups.values()
        urls = list(dict.fromkeys(c["url"] for c in chosen))
        return {}, "high" if len(urls) >= 2 else "medium", chosen, urls
    preferred = [c for c in candidates if c["scope"] == "target"] or candidates
    ranked = {}
    for candidate in preferred:
        ranked.setdefault(value_key(candidate["value"]), set()).add(candidate["url"])
    best = max(ranked, key=lambda key: len(ranked[key]))
    chosen = groups[best]
    return {"conflict": True}, "low", chosen, list(dict.fromkeys(c["url"] for c in chosen))


def compile_roster(
    records: Iterable[dict[str, Any]],
    metadata,
    roster: Roster,
    targets_spec: Any,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Return the roster-shaped frame (field-name columns) and accounting stats."""
    main = metadata.get_main_data_array()
    item_fields = list(
        metadata.schema["properties"][main]["items"].get("properties", {})
    )
    attributes = [field for field in item_fields if field not in roster.fields]
    evidence = {
        path[2] for path in evidence_paths(metadata.schema)
        if len(path) == 3 and path[:2] == (main, "*")
    }
    by_key = {roster.key(identity): target_id for target_id, identity in roster.targets.items()}

    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unmatched = 0
    for record in records:
        document = record.get("document") or {}
        url = document.get("source_url")
        scope = document.get("source_scope")
        for item in record["payload"].get(main) or []:
            target_id = by_key.get(roster.key(item))
            if target_id is None:
                unmatched += 1
                continue
            for field in attributes:
                raw = item.get(field)
                value, quote = (
                    (raw.get("value"), raw.get("quote"))
                    if field in evidence and isinstance(raw, dict) else (raw, None)
                )
                if not _missing(value):
                    cells.setdefault((target_id, field), []).append(
                        {"value": value, "quote": quote, "url": url, "scope": scope}
                    )

    rows, passthrough = [], {}
    conflicts = with_evidence = 0
    reserved = set(item_fields) | set(METADATA_COLUMNS)
    for target in iter_targets(targets_spec):
        target_id = identify_target(target, roster.fields)
        extra = {
            key: NA if value is None else str(value)
            for key, value in target.items()
            if key not in roster.fields and key not in TARGET_INPUT_FIELDS
        }
        clash = sorted(set(extra) & reserved)
        if clash:
            raise ValueError(
                f"Roster column(s) {clash} collide with schema fields or "
                f"{', '.join(METADATA_COLUMNS)}; rename them in discovery.targets"
            )
        passthrough.update(dict.fromkeys(extra))
        row = {field: target[field] for field in roster.fields}
        sources, cell_evidence, confidence = [], {}, "high"
        for field in attributes:
            candidates = cells.get((target_id, field))
            if not candidates:
                row[field] = NA
                continue
            flags, level, chosen, urls = _resolve(candidates)
            conflicts += bool(flags)
            first = next((c for c in chosen if c["quote"]), chosen[0])
            row[field] = first["value"]
            cell_evidence[field] = {
                "value": first["value"],
                **({"quote": first["quote"]} if field in evidence else {}),
                "url": first["url"],
                **flags,
            }
            sources.extend(urls)
            confidence = min(confidence, level, key=_CONFIDENCE_RANK.__getitem__)
        with_evidence += bool(cell_evidence)
        rows.append({
            **row,
            **extra,
            "sources": "; ".join(dict.fromkeys(url for url in sources if url)) or NA,
            "confidence": confidence if cell_evidence else "none",
            "evidence": json.dumps(cell_evidence, ensure_ascii=False, separators=(",", ":")),
        })

    columns = [*roster.fields, *attributes, *passthrough, *METADATA_COLUMNS]
    frame = pd.DataFrame(rows, columns=columns).fillna(NA)
    stats = {
        "roster_targets": len(rows),
        "targets_with_evidence": with_evidence,
        "unmatched_items": unmatched,
        "conflicts": conflicts,
    }
    return frame, stats
