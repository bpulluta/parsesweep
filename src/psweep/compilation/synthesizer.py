#!/usr/bin/env python3
"""Per-entity cross-source reconciliation stage for compilation.

Groups per-document extraction records by an entity key, then reconciles them
into ONE row per entity — resolving conflicting values across sources, grading
confidence, citing the sources that supported each value, and (optionally)
validating an ordering constraint over selected fields.

This is the domain-neutral alternative to the tabular deduplicator: where
`Deduplicator` keeps the single most-complete row (lossy), the synthesizer
merges across sources. It is NOT specific to any domain — every field it groups
on, reconciles, orders, or narrates is declared in `compilation.synthesis`
config as a path into the extraction payload. Timelines (announced/construction/
completion dates with a chronology constraint) are just one configuration; the
same stage reconciles, e.g., permit limits across filings, spec values across
datasheets, or prices across sources.

By default every item found in the main data array across the grouped records is
flattened into ONE row per entity. When a domain repeats sub-entities inside a
record (phases, expansions, line items, permit conditions...), declare
`item_group_by` — item-level field paths — to keep them apart: items are then
bucketed by that key within each entity and one row is synthesized per bucket.

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

from ..utils.item_matcher import get_nested_value as _get_path

logger = logging.getLogger(__name__)


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
        # Optional per-field precision (e.g. day/month/quarter/year for dates).
        # Carried through reconciliation and used to make the ordering check
        # precision-aware — a year-precision value is not "after" another
        # year-precision value in the same year merely because of Jan-1 pinning.
        self.precision_map = {
            r["field"]: r.get("precision")
            for r in rf
            if isinstance(r, dict) and r.get("field") and r.get("precision")
        }
        # Optional ordering validation. Fully config-driven:
        # - ordering_constraint: legacy single sequence ["a", "b", "c"]
        # - ordering_constraints: list of sequences [["a","b"],["b","d"]]
        # - ordering_exclusive_pairs: pairs that cannot both be set
        # Temporal handling stays opt-in and generic via ordering_comparison.
        legacy_ordering = self.cfg.get("ordering_constraint") or []
        configured_sequences = self.cfg.get("ordering_constraints")
        if isinstance(configured_sequences, list) and configured_sequences:
            self.ordering_constraints = [
                seq for seq in configured_sequences
                if isinstance(seq, list) and seq
            ]
        elif legacy_ordering:
            self.ordering_constraints = [legacy_ordering]
        else:
            self.ordering_constraints = []
        # Keep a single-sequence alias for prompt text/backward compatibility.
        self.ordering = self.ordering_constraints[0] if self.ordering_constraints else []
        self.ordering_exclusive_pairs = self.cfg.get("ordering_exclusive_pairs") or []
        self.comparison = self.cfg.get("ordering_comparison") or "lexical"
        # Default strict behavior: if precision is unstated, do NOT infer coarse
        # year/month from pinned dates. This prevents false negatives.
        self.infer_precision_from_pins = bool(
            self.cfg.get("ordering_infer_precision_from_pins", False)
        )
        self.has_ordering_checks = bool(
            self.ordering_constraints or self.ordering_exclusive_pairs
        )
        # Extra free-text outputs a domain may want (e.g. current_status).
        self.narrative_fields = self.cfg.get("narrative_fields") or []

        # Optional sub-entity split. Paths are resolved against each ITEM of the
        # main data array (not the record), so repeated sub-entities inside one
        # entity survive synthesis as separate rows instead of being merged.
        # Absent -> one row per entity, exactly as before.
        self.item_group_by = self.cfg.get("item_group_by") or []

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
            except Exception as exc:  # noqa: BLE001 - unreadable extraction file; skip and log
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

    def _item_finding(self, item: dict, citation: Any) -> dict:
        """One finding entry for a single item of the main data array."""
        entry: dict[str, Any] = {"citation_url": citation}
        for fld in self.fields:
            if item.get(fld) is not None:
                entry[fld] = item.get(fld)
            ev = self.evidence_map.get(fld)
            if ev and item.get(ev):
                entry[ev] = item.get(ev)
            pr = self.precision_map.get(fld)
            if pr and item.get(pr):
                entry[pr] = item.get(pr)
        for path in self.item_group_by:
            value = _get_path(item, path)
            if value is not None:
                entry[path.split(".")[-1]] = value
        return entry

    def _build_findings(self, recs: list[dict]) -> list[dict]:
        citation_field = self.cfg.get("citation_field")
        findings = []
        for rec in recs:
            citation = _get_path(rec, citation_field) if citation_field else None
            items = rec.get(self.main_array) if self.main_array else None
            if isinstance(items, list) and items:
                findings.extend(self._item_finding(it, citation) for it in items)
            else:
                findings.append(
                    {"citation_url": citation,
                     "note": "on-topic source with no reconcilable item extracted"}
                )
        return findings

    def _item_group_key(self, item: dict) -> tuple:
        return tuple(str(_get_path(item, p) or "") for p in self.item_group_by)

    def _build_item_groups(self, recs: list[dict]) -> dict[tuple, list[dict]]:
        """Bucket per-item findings by the configured item-level group key.

        Items that carry none of the key values fall into the empty-key bucket
        rather than being dropped. Sources that yielded no item at all become
        shared context in every bucket, so citation context is never lost; when
        no bucket exists at all they still produce the single empty-key row that
        un-grouped synthesis would have produced.
        """
        citation_field = self.cfg.get("citation_field")
        buckets: dict[tuple, list[dict]] = defaultdict(list)
        shared: list[dict] = []
        for rec in recs:
            citation = _get_path(rec, citation_field) if citation_field else None
            items = rec.get(self.main_array) if self.main_array else None
            if isinstance(items, list) and items:
                for it in items:
                    buckets[self._item_group_key(it)].append(
                        self._item_finding(it, citation)
                    )
            else:
                shared.append(
                    {"citation_url": citation,
                     "note": "on-topic source with no reconcilable item extracted"}
                )
        if not buckets:
            buckets[tuple("" for _ in self.item_group_by)] = []
        return {key: findings + shared for key, findings in buckets.items()}

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
            pr = self.precision_map.get(fld)
            if pr:
                if self.comparison == "date":
                    props[pr] = {
                        "type": ["string", "null"],
                        "enum": ["day", "month", "quarter", "year", None],
                        "description": (
                            f"Granularity of the resolved '{fld}' as actually "
                            "known from the winning source (day/month/quarter/"
                            f"year). Null if '{fld}' is null. Do not claim "
                            "precision you lack."
                        ),
                    }
                else:
                    props[pr] = {
                        "type": ["string", "null"],
                        "description": (
                            f"Granularity/qualifier of the resolved '{fld}' as "
                            f"known from the winning source. Null if '{fld}' is "
                            "null. Do not claim precision you lack."
                        ),
                    }
                required.append(pr)
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
        if self.has_ordering_checks:
            # Only ask for a generic per-field precision object when explicit
            # precision fields were NOT configured (those are emitted above).
            if not self.precision_map:
                props["value_precision"] = {
                    "type": "object",
                    "description": "Granularity actually known per ordered field: exact/day/month/quarter/year/none. Do not invent precision you lack.",
                    "additionalProperties": {"type": "string", "enum": ["exact", "day", "month", "quarter", "year", "none"]},
                }
            props["ordering_consistent"] = {
                "type": "boolean",
                "description": (
                    "ADVISORY. True if the resolved ordered fields read as "
                    "chronologically consistent at the precision actually known. "
                    "This is never a reason to drop or distrust a row — coarse "
                    "dates and expansions can legitimately look out of order."
                ),
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
        if self.has_ordering_checks:
            base += (
                "\n\nORDERING (ADVISORY, NOT A CONSTRAINT). The ordered fields "
                f"usually follow configured non-decreasing sequences (e.g. {' <= '.join(self.ordering)}). "
                "Use it only as a sanity check while resolving conflicts — NEVER "
                "invent, shift, or drop a value to force this order, and never "
                "discard a well-sourced value because it appears out of order. "
            )
            if self.comparison == "date":
                base += (
                    "Coarse values (a whole year pinned to Jan 1) and later "
                    "phases / expansions can legitimately appear out of order; "
                    "that is normal, not an error. Record each value at its true "
                    "granularity in its precision field. "
                )
            base += (
                "If the resolved values read as out of order, set "
                "ordering_consistent=false and explain briefly in ordering_notes, "
                "but still return every value faithfully as the sources state it."
            )
        extra = self.cfg.get("instructions")
        if extra:
            base += "\n\n" + str(extra)
        return base

    # -- ordering guard (deterministic, ADVISORY, comparator-pluggable) -------
    #
    # The ordering check is domain-neutral: `ordering_comparison` selects a
    # comparator so the same stage serves temporal, numeric, or lexical domains.
    # It is ALWAYS advisory — the caller keeps the row regardless of the result.

    # Temporal coarseness rank (used only by the "date" comparator): larger is
    # coarser. Two dates are compared at the COARSEST granularity they share, so
    # a year-precision value is never judged "after" another year-precision value
    # in the same year merely because both were pinned to January 1st.
    _PRECISION_RANK = {"day": 0, "exact": 0, "month": 1, "quarter": 2, "year": 3}

    def _date_precision_of(self, field: str, value: str, row: dict) -> str:
        """Resolved date precision: explicit precision field, else inferred from
        the pinned date pattern (a conservative fallback). Temporal comparator only.
        """
        pr_field = self.precision_map.get(field)
        explicit = row.get(pr_field) if pr_field else None
        if explicit in self._PRECISION_RANK:
            return explicit
        if not self.infer_precision_from_pins:
            return "day"
        v = str(value)
        if v.endswith("-01-01"):
            return "year"
        if v.endswith("-01"):
            return "month"
        return "day"

    @staticmethod
    def _truncate_date(value: str, precision: str) -> str:
        """Truncate a YYYY-MM-DD value to the given precision for comparison."""
        v = str(value)
        if precision == "year":
            return v[:4]
        if precision in ("month", "quarter"):
            return v[:7]
        return v[:10]

    def _out_of_order(self, n1: str, v1, n2: str, v2, row: dict) -> bool:
        """True if (n1, v1) sorts AFTER (n2, v2) under the configured comparator."""
        if self.comparison == "numeric":
            try:
                return float(v1) > float(v2)
            except (TypeError, ValueError):  # noqa: BLE001 - non-numeric values can't be judged; don't flag
                return False  # non-numeric values can't be judged; don't flag
        if self.comparison == "date":
            p1 = self._date_precision_of(n1, v1, row)
            p2 = self._date_precision_of(n2, v2, row)
            coarsest = p1 if self._PRECISION_RANK[p1] >= self._PRECISION_RANK[p2] else p2
            return self._truncate_date(v1, coarsest) > self._truncate_date(v2, coarsest)
        return str(v1) > str(v2)  # lexical (default)

    def _check_ordering(self, row: dict) -> tuple[bool, str]:
        """ADVISORY ordering check. Never fatal: the caller keeps the row
        regardless. Compares consecutive present values with the configured
        comparator (lexical / numeric / date).
        """
        if not self.has_ordering_checks:
            return True, ""
        violations: list[str] = []
        for sequence in self.ordering_constraints:
            present = [(f, row.get(f)) for f in sequence if row.get(f)]
            violations.extend(
                f"{n1} ({v1}) is after {n2} ({v2})"
                for (n1, v1), (n2, v2) in zip(present, present[1:])
                if self._out_of_order(n1, v1, n2, v2, row)
            )
        for pair in self.ordering_exclusive_pairs:
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            left, right = pair
            if row.get(left) and row.get(right):
                violations.append(
                    f"exclusive pair conflict: both {left} ({row.get(left)}) and "
                    f"{right} ({row.get(right)}) are set"
                )
        if violations:
            return False, "ordering note: " + "; ".join(violations)
        return True, "ordering consistent"

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
            pr = self.precision_map.get(fld)
            if pr:
                d[pr] = src.get(pr)
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

    def _identity_context(self, recs: list[dict]) -> dict[str, Any]:
        """First non-empty value per configured identity path across records."""
        identity = self.cfg.get("identity_fields") or []
        id_ctx: dict[str, Any] = {}
        for path in identity:
            for rec in recs:
                v = _get_path(rec, path)
                if v:
                    id_ctx[path.split(".")[-1]] = v
                    break
        return id_ctx

    def _synthesize_group(self, key: tuple, recs: list[dict]) -> dict:
        """Reconcile one entity group into a single row (no item split)."""
        return self._synthesize_findings(
            key,
            (),
            self._identity_context(recs),
            self._build_findings(recs),
            len(recs),
        )

    def _synthesize_group_rows(self, key: tuple, recs: list[dict]) -> list[dict]:
        """Rows for one entity group: one per item subgroup when
        ``item_group_by`` is configured, otherwise exactly one.
        """
        if not self.item_group_by:
            return [self._synthesize_group(key, recs)]
        id_ctx = self._identity_context(recs)
        buckets = self._build_item_groups(recs)
        return [
            self._synthesize_findings(key, item_key, id_ctx, findings, len(recs))
            for item_key, findings in sorted(buckets.items())
        ]

    def _synthesize_findings(
        self,
        key: tuple,
        item_key: tuple,
        id_ctx: dict[str, Any],
        findings: list[dict],
        n_sources: int,
    ) -> dict:
        group_by = self.cfg.get("group_by") or []
        valued = self._valued_findings(findings)
        if len(valued) < self.min_sources_for_llm:
            d = self._deterministic_merge(valued)
            self.deterministic_rows += 1
        else:
            d = self._llm_merge(key, group_by, item_key, id_ctx, findings)
            self.llm_calls += 1
        return self._assemble_row(key, group_by, item_key, id_ctx, d, n_sources)

    def _entity_header(self, key, group_by, item_key, id_ctx) -> str:
        lines = [f"{g.split('.')[-1]}: {k}" for g, k in zip(group_by, key)]
        lines += [f"{name}: {val}" for name, val in id_ctx.items()]
        lines += [
            f"{p.split('.')[-1]}: {k}" for p, k in zip(self.item_group_by, item_key)
        ]
        return "\n".join(lines)

    def _llm_merge(self, key, group_by, item_key, id_ctx, findings) -> dict:
        merge_schema = self._build_merge_schema()
        header = self._entity_header(key, group_by, item_key, id_ctx)
        scope = (
            "Reconcile the single best record for THIS sub-entity from these "
            "per-source extractions; the extractions below belong to it only. "
            if self.item_group_by
            else "Reconcile the single best record from these per-source extractions. "
        )
        prompt = (
            f"ENTITY:\n{header}\n\n"
            + scope
            + "Resolve conflicts and explain resolution. citation_urls must list "
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

    def _assemble_row(self, key, group_by, item_key, id_ctx, d, n_sources) -> dict:
        det_ok, det_note = self._check_ordering(d)
        row: dict[str, Any] = {}
        for g, k in zip(group_by, key):
            row[g.split(".")[-1]] = k
        row.update(id_ctx)
        for path, k in zip(self.item_group_by, item_key):
            row[path.split(".")[-1]] = k
        for fld in self.fields:
            row[fld] = d.get(fld) or ""
            pr = self.precision_map.get(fld)
            if pr:
                row[pr] = d.get(pr) or ""
        for nf in self.narrative_fields:
            row[nf] = d.get(nf) or ""
        if self.has_ordering_checks:
            consistent = bool(d.get("ordering_consistent", True)) and det_ok
            notes = d.get("ordering_notes") or ""
            if not det_ok:
                notes = (notes + (" | " + det_note if notes else det_note)).strip()
            row["ordering_consistent"] = consistent
            row["ordering_notes"] = notes
        row["data_confidence"] = d.get("data_confidence") or ""
        row["reasoning"] = d.get("reasoning") or ""
        row["summary"] = d.get("summary") or ""
        row["citation_urls"] = " | ".join(d.get("citation_urls") or [])
        row["sources_checked"] = n_sources
        return row

    def synthesize_from_directory(self, json_dir: str) -> pd.DataFrame:
        """Load extracted JSON records from a directory and synthesize them.

        Reads all extraction output JSON files under ``json_dir``, groups them
        by the configured ``group_by`` identity fields, reconciles each group
        (optionally via LLM), and returns one row per group — or one row per
        ``item_group_by`` subgroup within each group when that option is set.

        Parameters
        ----------
        json_dir : str
            Path to a directory containing per-document extraction JSON files.

        Returns
        -------
        pd.DataFrame
            One synthesized row per unique entity group (per item subgroup when
            ``item_group_by`` is configured).
        """
        records = self._load_records(json_dir)
        groups = self._group(records)
        rows = [
            row
            for k, recs in sorted(groups.items())
            for row in self._synthesize_group_rows(k, recs)
        ]
        return pd.DataFrame(rows)
