#!/usr/bin/env python3
"""Per-entity cross-source reconciliation stage for consolidation.

Groups per-document extraction records by an entity key, then reconciles them
into ONE row per entity — resolving conflicting values across sources, grading
confidence, citing the sources that supported each value, and (optionally)
validating an ordering constraint over selected fields.

This is the domain-neutral alternative to the tabular deduplicator: where
`Deduplicator` keeps the single most-complete row (lossy), the synthesizer
merges across sources. It is NOT specific to any domain — every field it groups
on, reconciles, orders, or narrates is declared in `consolidation.synthesis`
config as a path into the extraction payload. Timelines (announced/construction/
completion dates with a chronology constraint) are just one configuration; the
same stage reconciles, e.g., permit limits across filings, spec values across
datasheets, or prices across sources.

Cost control: the LLM is called ONLY when >= `min_sources_for_llm` sources carry
a value to reconcile. With 0 or 1 such source the answer is deterministic (nulls,
or that single source's values) and no API call is made.
"""
from __future__ import annotations

import glob
import json
import logging
import os
from collections import defaultdict
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def _get_path(obj: dict, dotted: str) -> Any:
    """Resolve a dotted path within a nested dict; None if any hop is missing."""
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


class Synthesizer:
    """Reconcile many per-document records into one row per entity."""

    def __init__(
        self, schema_metadata, config: dict, llm_client, verbose: bool = False
    ):
        self.sm = schema_metadata
        self.cfg = config or {}
        self.client = llm_client
        self.verbose = verbose
        self.main_array = self.cfg.get("item_array") or (
            schema_metadata.get_main_data_array() if schema_metadata else None
        )

        # reconcile_fields: [{field, evidence?}]. Fully generic — any payload
        # item fields, not just dates.
        rf = self.cfg.get("reconcile_fields") or []
        self.fields = [r["field"] for r in rf if isinstance(r, dict) and r.get("field")]
        self.evidence_map = {
            r["field"]: r.get("evidence")
            for r in rf
            if isinstance(r, dict) and r.get("field")
        }
        # Optional ordering validation (e.g. announced <= construction <=
        # completion). Only enabled when configured — non-temporal domains omit it.
        self.ordering = self.cfg.get("ordering_constraint") or []
        # Extra free-text outputs a domain may want (e.g. current_status).
        self.narrative_fields = self.cfg.get("narrative_fields") or []

        # Cost control (see module docstring).
        self.min_sources_for_llm = int(self.cfg.get("min_sources_for_llm", 2))
        self.llm_calls = 0
        self.deterministic_rows = 0

    # -- record loading / grouping -------------------------------------------

    def _load_records(self, json_dir: str) -> list[dict]:
        files = sorted(
            glob.glob(os.path.join(str(json_dir), "**", "*.json"), recursive=True)
        )
        records = []
        for f in files:
            if "run_manifests" in f:
                continue
            try:
                raw = json.load(open(f, encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("skip unreadable %s: %s", f, exc)
                continue
            payload = raw.get("payload") if isinstance(raw, dict) else None
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def _group(self, records: list[dict]) -> dict[tuple, list[dict]]:
        group_by = self.cfg.get("group_by") or []
        relevance = self.cfg.get("relevance_flag")
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for rec in records:
            if relevance and not _get_path(rec, relevance):
                continue
            key = tuple(str(_get_path(rec, g) or "") for g in group_by)
            if not any(key):
                continue
            groups[key].append(rec)
        return groups

    # -- findings extraction --------------------------------------------------

    def _build_findings(self, recs: list[dict]) -> list[dict]:
        citation_field = self.cfg.get("citation_field")
        findings = []
        for rec in recs:
            citation = _get_path(rec, citation_field) if citation_field else None
            items = rec.get(self.main_array) if self.main_array else None
            if isinstance(items, list) and items:
                for it in items:
                    entry = {"citation_url": citation}
                    for fld in self.fields:
                        if it.get(fld) is not None:
                            entry[fld] = it.get(fld)
                        ev = self.evidence_map.get(fld)
                        if ev and it.get(ev):
                            entry[ev] = it.get(ev)
                    findings.append(entry)
            else:
                findings.append(
                    {"citation_url": citation,
                     "note": "on-topic source with no reconcilable item extracted"}
                )
        return findings

    def _valued_findings(self, findings: list[dict]) -> list[dict]:
        """Findings carrying at least one reconcilable value."""
        return [f for f in findings if any(f.get(x) is not None for x in self.fields)]

    # -- merge schema / prompt (generic) -------------------------------------

    def _build_merge_schema(self) -> dict:
        props: dict[str, Any] = {}
        required: list[str] = []
        for fld in self.fields:
            props[fld] = {"type": ["string", "null"], "description": f"Resolved value for '{fld}', or null."}
            required.append(fld)
        for nf in self.narrative_fields:
            props[nf] = {"type": "string", "description": f"Free-text '{nf}' for this entity."}
            required.append(nf)
        props["data_confidence"] = {"type": "string", "enum": ["high", "medium", "low"]}
        props["reasoning"] = {
            "type": "string",
            "description": "Per-field reasoning: which source/quote each resolved value came from and how conflicts were resolved.",
        }
        props["summary"] = {"type": "string", "description": "2-4 sentence narrative synthesis for this entity."}
        props["citation_urls"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": "Source URLs that actually supported a resolved value (subset of inputs).",
        }
        required += ["data_confidence", "reasoning", "summary", "citation_urls"]
        if self.ordering:
            props["value_precision"] = {
                "type": "object",
                "description": "Granularity actually known per ordered field: exact/day/month/quarter/year/none. Do not invent precision you lack.",
                "additionalProperties": {"type": "string", "enum": ["exact", "day", "month", "quarter", "year", "none"]},
            }
            props["ordering_consistent"] = {
                "type": "boolean",
                "description": "True only if the resolved ordered fields obey the required non-decreasing order.",
            }
            props["ordering_notes"] = {"type": "string"}
            required += ["ordering_consistent", "ordering_notes"]
        return {"type": "object", "additionalProperties": False, "required": required, "properties": props}

    def _system_prompt(self) -> str:
        base = (
            "You reconcile ONE authoritative record for an entity from multiple "
            "per-document extractions, resolving conflicts across sources. Every "
            "resolved value must be a fact about the ENTITY itself, never an "
            "artifact of the source document (e.g. a document's publication date "
            "is not an entity value). Prefer official/primary and the most "
            "specific, most recent sources; explain the choice. If a value is not "
            "supported by any source, return null. Do not invent values."
        )
        if self.ordering:
            base += (
                "\n\nORDERING CONSTRAINT (mandatory). The resolved values MUST "
                f"obey this non-decreasing order: {' <= '.join(self.ordering)}. "
                "Handle IMPRECISE values carefully: a coarse value (e.g. a whole "
                "year) must NOT be pinned to a boundary that places it before a "
                "more precise earlier value. Record true granularity in "
                "value_precision. If sources cannot be reconciled into a valid "
                "order, set ordering_consistent=false and explain in "
                "ordering_notes rather than forcing a fake order."
            )
        extra = self.cfg.get("instructions")
        if extra:
            base += "\n\n" + str(extra)
        return base

    # -- ordering guard (deterministic) --------------------------------------

    def _check_ordering(self, row: dict) -> tuple[bool, str]:
        if not self.ordering:
            return True, ""
        present = [(f, row.get(f)) for f in self.ordering if row.get(f)]
        violations = [
            f"{n1} ({v1}) is after {n2} ({v2})"
            for (n1, v1), (n2, v2) in zip(present, present[1:])
            if v1 > v2
        ]
        if violations:
            return False, "ILLOGICAL ORDERING: " + "; ".join(violations)
        return True, "ordering valid"

    # -- deterministic (no-API) merge for trivial cases ----------------------

    def _deterministic_merge(self, valued: list[dict]) -> dict:
        d: dict[str, Any] = dict.fromkeys(self.fields)
        for nf in self.narrative_fields:
            d[nf] = ""
        if not valued:
            d.update(
                data_confidence="low",
                reasoning="No source provided a reconcilable value.",
                summary="No values found in the checked sources.",
                citation_urls=[],
            )
            return d
        src = valued[0]
        for fld in self.fields:
            d[fld] = src.get(fld)
        url = src.get("citation_url")
        ev_bits = [
            f"{fld}: {src.get(self.evidence_map[fld])}"
            for fld in self.fields
            if src.get(fld) and self.evidence_map.get(fld) and src.get(self.evidence_map[fld])
        ]
        d.update(
            data_confidence="low",  # single, uncorroborated source
            reasoning=(
                "Single source with values; no cross-source reconciliation "
                f"needed. Values taken directly from {url}."
            ),
            summary="; ".join(ev_bits) or f"Values from {url}.",
            citation_urls=[url] if url else [],
        )
        return d

    # -- per-entity synthesis -------------------------------------------------

    def _synthesize_group(self, key: tuple, recs: list[dict]) -> dict:
        group_by = self.cfg.get("group_by") or []
        identity = self.cfg.get("identity_fields") or []
        findings = self._build_findings(recs)

        id_ctx: dict[str, Any] = {}
        for path in identity:
            for rec in recs:
                v = _get_path(rec, path)
                if v:
                    id_ctx[path.split(".")[-1]] = v
                    break

        valued = self._valued_findings(findings)
        if len(valued) < self.min_sources_for_llm:
            d = self._deterministic_merge(valued)
            self.deterministic_rows += 1
        else:
            d = self._llm_merge(key, group_by, id_ctx, findings)
            self.llm_calls += 1

        return self._assemble_row(key, group_by, id_ctx, d, len(recs))

    def _llm_merge(self, key, group_by, id_ctx, findings) -> dict:
        merge_schema = self._build_merge_schema()
        header = "\n".join(
            f"{g.split('.')[-1]}: {k}" for g, k in zip(group_by, key)
        )
        header += "".join(f"\n{name}: {val}" for name, val in id_ctx.items())
        prompt = (
            f"ENTITY:\n{header}\n\n"
            "Reconcile the single best record from these per-source extractions. "
            "Resolve conflicts and explain resolution. citation_urls must list "
            "only sources that supported a resolved value.\n\n"
            "SOURCE EXTRACTIONS (JSON):\n"
            + json.dumps(findings, indent=2, default=str)
            + "\n\nReturn ONLY a JSON object matching EXACTLY this schema (all "
            "keys required; null for unknown values):\n"
            + json.dumps(merge_schema, indent=2)
        )
        result = self.client.extract(
            text=prompt, schema=merge_schema,
            system_prompt=self._system_prompt(), user_prompt=prompt,
        )
        return result.get("data", {}) if isinstance(result, dict) else {}

    def _assemble_row(self, key, group_by, id_ctx, d, n_sources) -> dict:
        det_ok, det_note = self._check_ordering(d)
        row: dict[str, Any] = {}
        for g, k in zip(group_by, key):
            row[g.split(".")[-1]] = k
        row.update(id_ctx)
        for fld in self.fields:
            row[fld] = d.get(fld) or ""
        for nf in self.narrative_fields:
            row[nf] = d.get(nf) or ""
        if self.ordering:
            consistent = bool(d.get("ordering_consistent", True)) and det_ok
            notes = d.get("ordering_notes") or ""
            if not det_ok:
                notes = (det_note + (" | " + notes if notes else "")).strip()
            row["ordering_consistent"] = consistent
            row["ordering_notes"] = notes
        row["data_confidence"] = d.get("data_confidence") or ""
        row["reasoning"] = d.get("reasoning") or ""
        row["summary"] = d.get("summary") or ""
        row["citation_urls"] = " | ".join(d.get("citation_urls") or [])
        row["sources_checked"] = n_sources
        return row

    def synthesize_from_directory(self, json_dir: str) -> pd.DataFrame:
        records = self._load_records(json_dir)
        groups = self._group(records)
        rows = [
            self._synthesize_group(k, recs)
            for k, recs in sorted(groups.items())
        ]
        return pd.DataFrame(rows)
