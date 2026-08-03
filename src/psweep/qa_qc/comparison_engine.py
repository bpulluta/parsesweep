"""
Comparison Engine for QA/QC Multi-Model Validation.

Compares outputs from multiple models. Comparison behavior is driven by the
active runtime QA/QC profile in config/<domain>/run.yaml.
Includes potential duplicate detection and completeness metrics.

Usage:
    from psweep.qa_qc.comparison_engine import ComparisonEngine
    from psweep.qa_qc.utils import resolve_qaqc_runtime_config
    from psweep.utils.schema_metadata import SchemaMetadata

    schema_metadata = SchemaMetadata(schema_path)
    qa_qc_config = resolve_qaqc_runtime_config(
        schema_metadata,
        runtime_qaqc=run_config["qaqc"],   # from config/<domain>/run.yaml
    )
    engine = ComparisonEngine(schema_metadata, qa_qc_config=qa_qc_config)

    result = engine.compare_outputs(
        output_files={"gpt-4.1": path1, "claude-haiku-4-5": path2},
        document_name="austin_energy"
    )
"""

import json
import logging
import re
from collections import Counter
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Import shared utilities
from ..utils.item_matcher import (
    create_item_index,
    extract_key_tokens,
    get_nested_value,
    normalize_for_matching,
)
from ..utils.value_normalizer import (
    build_unit_equivalence_index,
    canonicalize_measurement,
    canonicalize_unit,
    is_numeric_value,
    normalize_value,
)
from .utils import resolve_qaqc_runtime_config

logger = logging.getLogger(__name__)


@dataclass
class FieldComparison:
    """Represents comparison of a single field across models."""

    item_id: str
    field_path: str
    model_values: Dict[str, Any]  # {model_name: value}
    agreement_score: str  # e.g., "3/3", "2/3"
    needs_review: bool
    notes: str = ""
    present_models: List[str] = field(default_factory=list)
    missing_models: List[str] = field(default_factory=list)
    paired_by_judge: bool = False
    pairing_confidence: str = ""
    pairing_reason: str = ""
    row_id: str = ""
    match_method: str = ""


@dataclass
class PotentialDuplicate:
    """
    Represents items that might be the same data with different keys.

    Example: gpt-5 has time__reclamation_deadline_days=60
             gpt-4.1 has time__permit_validity_days=60
    Same value extracted with different requirement_type - likely same source data.
    """

    value: Any
    unit: str
    items: List[Tuple[str, dict]]  # [(model_name, item_dict), ...]
    reason: str = "Same value extracted with different requirement_type"


@dataclass
class CompletenessResult:
    """Completeness metrics for a single model's extraction."""

    model: str
    items_extracted: int
    expected_found: int
    expected_total: int
    completeness_score: float  # 0.0 to 1.0
    missing_expected: List[str]  # Expected requirement_types not found


@dataclass
class ComparisonResult:
    """Full comparison result for a document."""

    document_name: str
    models: List[str]
    summary: Dict[str, Any] = field(default_factory=dict)
    context_comparisons: List[FieldComparison] = field(default_factory=list)
    item_comparisons: List[FieldComparison] = field(default_factory=list)
    potential_duplicates: List[PotentialDuplicate] = field(
        default_factory=list
    )
    completeness: Dict[str, CompletenessResult] = field(default_factory=dict)

    @property
    def field_comparisons(self) -> List[FieldComparison]:
        """All field comparisons (context + items combined)."""
        return self.context_comparisons + self.item_comparisons


class ComparisonEngine:
    """
    Compare outputs from multiple models using schema metadata plus runtime QA/QC config.

    Uses resolved QA/QC configuration always sourced from config/<domain>/run.yaml.
    Schema metadata carries only the extraction contract (main_data_array, identifier_fields,
    context_objects, deduplication keys) — no QA/QC runtime settings.

    Example usage::

        qa_qc_config = resolve_qaqc_runtime_config(schema_metadata, pack, pack_qaqc, lane)
        engine = ComparisonEngine(schema_metadata, qa_qc_config=qa_qc_config)
        result = engine.compare_outputs(output_files, document_name)
    """

    def __init__(
        self, schema_metadata, qa_qc_config: Dict[str, Any]
    ):
        """
        Initialize comparison engine.

        Args:
            schema_metadata: SchemaMetadata instance (extraction contract only).
            qa_qc_config: Resolved QA/QC runtime config from
                ``resolve_qaqc_runtime_config``. Always sourced from
                config/<domain>/run.yaml — never from schema metadata.
        """
        self.schema_metadata = schema_metadata
        self.main_data_array = schema_metadata.get_main_data_array()
        self.identifier_fields = schema_metadata.get_identifier_fields()
        self.qa_qc_config = qa_qc_config

        self.match_fields = list(self.qa_qc_config["match_fields"])
        self.compare_fields = set(self.qa_qc_config["compare_fields"])
        # comparison_approach controls which fields are compared:
        #   "mixed"        — compare ALL compare_fields (numeric as numeric, categorical/text
        #                    as case-insensitive string equality). This is the recommended default.
        #   "numeric_only" — legacy: only compare fields where at least one value is numeric.
        #   "text_review"  — all fields, plus full deduplication/fallback-matching for
        #                    free-form narrative rows.
        self.comparison_approach = self.qa_qc_config.get(
            "comparison_approach", "mixed"
        )
        self.config_source = self.qa_qc_config.get("source", "schema_metadata")
        self.lane_name = self.qa_qc_config.get("lane_name")
        self.projection = self.qa_qc_config.get("projection") or None
        self.enable_text_fallback_matching = bool(
            self.qa_qc_config.get("enable_text_fallback_matching", False)
        )
        self.enable_judge_pair_matching = bool(
            self.qa_qc_config.get("enable_judge_pair_matching", False)
        )
        self.fuzzy_match_fields = list(
            self.qa_qc_config.get("fuzzy_match_fields") or []
        )
        self.text_fallback_fields = list(
            self.qa_qc_config.get("text_fallback_fields") or []
        )
        self.unit_equivalence_index = build_unit_equivalence_index(
            self.qa_qc_config.get("unit_equivalence_groups")
        )
        self.semantic_match_threshold = float(
            self.qa_qc_config.get("semantic_match_threshold", 0.30)
        )
        self._judge_config = self.qa_qc_config.get("judge") or {}
        self._judge_runtime = self.qa_qc_config.get("judge_runtime") or {}
        self._judge_row_equivalence_enabled = bool(
            self._judge_config.get("row_equivalence", True)
        )
        self._judge_client = None
        # Configurable scope-variant category/subject pairs for qualitative lanes.
        # Format: list of [category, subject] pairs (normalized, lowercase).
        # Scope-variant items are excluded from gate-failure math because they
        # represent narrow auxiliary rows that differ between models by design.
        # Empty by default — populate via lane config scope_variant_keys.
        self._scope_variant_keys: frozenset[tuple] = frozenset(
            (str(pair[0]).strip().lower(), str(pair[1]).strip().lower())
            for pair in (self.qa_qc_config.get("scope_variant_keys") or [])
            if isinstance(pair, (list, tuple)) and len(pair) >= 2
        )

        logger.debug(
            f"ComparisonEngine: main_data_array={self.main_data_array}"
        )
        logger.debug(f"ComparisonEngine: match_fields={self.match_fields}")
        logger.debug(f"ComparisonEngine: compare_fields={self.compare_fields}")

    def compare_outputs(
        self,
        output_files: Dict[str, Path],
        document_name: str,
    ) -> ComparisonResult:
        """
        Compare outputs from multiple models.

        Args:
            output_files: Dict mapping model name to output file path
            document_name: Name of the document being compared

        Returns:
            ComparisonResult with summary and field comparisons based on the
            resolved QA/QC comparison approach
        """
        # Load outputs
        outputs = self._load_outputs(output_files)
        self._judge_stats = {
            "attempted_calls": 0,
            "successful_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_cost_usd": 0.0,
        }

        if len(outputs) < 2:
            logger.warning(
                f"Only {len(outputs)} valid outputs, need 2+ for comparison"
            )
            return ComparisonResult(
                document_name=document_name,
                models=list(outputs.keys()),
                summary={"error": "Need at least 2 valid outputs"},
            )

        models = list(outputs.keys())

        # Track statistics
        context_skipped = 0
        item_skipped = 0

        # 1. Compare context objects (numeric fields only)
        context_comparisons = []
        for ctx_name in self.schema_metadata.get_context_objects():
            comps, skipped = self._compare_context_object(
                ctx_name, outputs, models
            )
            context_comparisons.extend(comps)
            context_skipped += skipped

        # 2. Compare main data array items
        item_arrays = self._extract_item_arrays(outputs)
        aligned_rows = self._align_items(item_arrays, models)

        item_comparisons = []
        for aligned_row in aligned_rows:
            comps, skipped = self._compare_item_fields(
                aligned_row["item_key"],
                aligned_row["items"],
                models,
                row_id=aligned_row["row_id"],
                match_method=aligned_row["match_method"],
            )
            item_comparisons.extend(comps)
            item_skipped += skipped

        # 3. Detect potential duplicates
        potential_duplicates = self._detect_potential_duplicates(
            {
                model: {
                    row["row_id"]: row["items"][model]
                    for row in aligned_rows
                    if row["items"].get(model) is not None
                }
                for model in models
            },
            models,
        )

        # 4. Calculate completeness
        completeness = self._calculate_completeness(item_arrays, models)

        # 5. Calculate summary
        summary = self._calculate_summary(
            context_comparisons,
            item_comparisons,
            len(models),
            item_arrays,
            context_skipped,
            item_skipped,
            potential_duplicates,
            completeness,
        )

        return ComparisonResult(
            document_name=document_name,
            models=models,
            summary=summary,
            context_comparisons=context_comparisons,
            item_comparisons=item_comparisons,
            potential_duplicates=potential_duplicates,
            completeness=completeness,
        )

    def _compare_context_object(
        self, ctx_name: str, outputs: Dict[str, dict], models: List[str]
    ) -> Tuple[List[FieldComparison], int]:
        """Compare a context object across models.

        Context objects (jurisdiction, document_applicability, etc.) are
        informational metadata — they are counted as skipped so the summary
        can report how many fields were seen but not compared.
        """
        skipped = 0
        for ctx in (outputs[m].get(ctx_name, {}) for m in models):
            if isinstance(ctx, dict):
                skipped += len(self._get_flat_fields(ctx))
        return [], skipped

    def _compare_item_fields(
        self,
        item_key: Tuple,
        items: Dict[str, Optional[dict]],
        models: List[str],
        *,
        row_id: str = "",
        match_method: str = "",
    ) -> Tuple[List[FieldComparison], int]:
        """Compare fields for one item across models (numeric only)."""
        comparisons = []
        skipped = 0

        # Create readable item ID
        item_id = self._format_item_id(item_key, items)

        # Check which models have this item
        missing = [m for m in models if items.get(m) is None]
        present = [m for m in models if items.get(m) is not None]

        # If NO models have this item, skip entirely
        if not present:
            return comparisons, skipped

        # Get all fields from present items
        all_fields = set()
        for m in present:
            all_fields.update(self._get_flat_fields(items[m]))

        # Compare each field using the active QA/QC comparison approach.
        for field_path in sorted(all_fields):
            # Skip internal fields
            if field_path.startswith("_"):
                skipped += 1
                continue

            # Get field name (last part of path)
            field_name = field_path.split(".")[-1].lower()

            # Only compare fields specified in schema qa_qc.comparison.primary_fields
            if field_name not in self.compare_fields:
                skipped += 1
                continue

            # Get values - show actual value for present items, None for missing
            model_values = {}
            for model in models:
                if items.get(model) is not None:
                    model_values[model] = get_nested_value(
                        items[model], field_path
                    )
                else:
                    model_values[model] = None  # Model doesn't have this item

            # For missing items, always include the comparison to show which model
            # extracted the item. Otherwise, filter based on the active comparison approach.
            if missing:
                pass
            elif self.comparison_approach == "text_review":
                # All configured fields compared; deep dedup/fallback matching active.
                pass
            elif self.comparison_approach in ("mixed", "numeric_only"):
                # "mixed"        — compare ALL compare_fields. Numeric fields get
                #                  numeric comparison; categorical/text fields get
                #                  case-insensitive string equality. Fixes the
                #                  silent-drop bug where obligation/value_interpretation
                #                  (schema enum fields) were skipped in numeric_only.
                # "numeric_only" — legacy: only compare fields that contain a numeric
                #                  value; keep unit/source_text for report context.
                if self.comparison_approach == "mixed":
                    pass  # compare everything in compare_fields
                else:
                    # numeric_only: skip non-numeric fields (except context fields)
                    if field_name in ("unit", "source_text"):
                        pass
                    else:
                        has_numeric = any(
                            is_numeric_value(v)
                            for v in model_values.values()
                            if v is not None
                        )
                        if not has_numeric:
                            skipped += 1
                            continue
            else:
                # Unknown approach: default to comparing everything (safe fallback).
                pass

            comparison = self._create_comparison(
                item_id=item_id,
                field_path=field_path,
                model_values=model_values,
                models=models,
                present_models=present,
                missing_models=missing,  # Pass info about which models are missing this item
                pair_metadata=self._extract_pair_metadata(items, present),
                row_id=row_id,
                match_method=match_method,
            )
            comparisons.append(comparison)

        self._resolve_measurement_pair_equivalence(
            comparisons=comparisons,
            models=models,
            missing_models=missing,
        )
        self._apply_row_level_judge_resolution(
            comparisons=comparisons,
            item_id=item_id,
            items=items,
            models=models,
            missing_models=missing,
        )
        return comparisons, skipped

    def _apply_row_level_judge_resolution(
        self,
        *,
        comparisons: List[FieldComparison],
        item_id: str,
        items: Dict[str, Optional[dict]],
        models: List[str],
        missing_models: List[str],
    ) -> None:
        """Use judge at row level to clear minor multi-field semantic variance."""
        if not comparisons:
            return
        if not self._judge_row_equivalence_enabled:
            return
        apply_on = str(self._judge_config.get("apply_on", "none")).strip().lower()
        if apply_on != "all":
            return
        if missing_models:
            return
        if not any(comp.needs_review for comp in comparisons):
            return
        if not self._judge_config.get("enabled") or not self._judge_runtime.get("model"):
            return

        row_payload: Dict[str, str] = {}
        compare_fields = sorted(self.compare_fields)
        for model in models:
            item = items.get(model)
            if item is None:
                continue
            parts: List[str] = []
            for field_name in compare_fields:
                value = get_nested_value(item, field_name)
                if value in (None, ""):
                    continue
                parts.append(f"{field_name}={value}")
            for key_field in self.match_fields:
                value = get_nested_value(item, key_field)
                if value in (None, ""):
                    continue
                parts.append(f"{key_field}={value}")
            row_payload[model] = " | ".join(parts)

        if len(row_payload) < 2:
            return

        try:
            judge_result = self._judge_text_equivalence(
                item_id=item_id,
                field_path="row_equivalence",
                model_values=row_payload,
            )
        except Exception:
            return
        if not isinstance(judge_result, dict):
            return
        confidence = str(judge_result.get("confidence", "low"))
        if not (
            judge_result.get("equivalent") is True
            and self._judge_meets_confidence_threshold(confidence)
        ):
            return

        reason = str(judge_result.get("reason") or "row-level semantic alignment")
        for comp in comparisons:
            if not comp.needs_review:
                continue
            comp.needs_review = False
            comp.agreement_score = f"{len(models)}/{len(models)}"
            comp.notes = f"LLM row judge matched ({confidence}): {reason}"

    def _create_comparison(
        self,
        item_id: str,
        field_path: str,
        model_values: Dict[str, Any],
        models: List[str],
        present_models: List[str] = None,
        missing_models: List[str] = None,
        pair_metadata: Optional[Dict[str, Any]] = None,
        row_id: str = "",
        match_method: str = "",
    ) -> FieldComparison:
        """Create a FieldComparison with agreement calculation."""
        present_models = present_models or []
        missing_models = missing_models or []
        pair_metadata = pair_metadata or {}

        # Normalize values for comparison
        normalized = {m: normalize_value(v) for m, v in model_values.items()}
        field_name = field_path.split(".")[-1].lower()

        # Count agreement
        agreement_count = self._count_agreement_for_field(
            field_name=field_name,
            normalized_values=normalized,
        )
        agreement_score = f"{agreement_count}/{len(models)}"
        needs_review = agreement_count < len(models)

        # Generate notes - prioritize explaining WHY there's disagreement
        notes = ""
        if needs_review:
            if missing_models:
                # Item was missing from some models entirely
                notes = f"Item not extracted by: {', '.join(missing_models)}"
            else:
                # Item exists in all models but values differ
                unique = set(
                    str(v) for v in normalized.values() if v is not None
                )
                if len(unique) > 1:
                    notes = f"{len(unique)} different values"
                elif any(v is None for v in normalized.values()):
                    empty = [m for m, v in normalized.items() if v is None]
                    notes = f"Empty in: {', '.join(empty)}"
                if self._should_attempt_judge(
                    field_path=field_path,
                    model_values=model_values,
                ):
                    try:
                        judge_result = self._judge_text_equivalence(
                            item_id=item_id,
                            field_path=field_path,
                            model_values=model_values,
                        )
                    except Exception as exc:
                        judge_result = None
                        notes = (
                            f"{notes}; judge_error={str(exc)[:80]}"
                            if notes
                            else f"judge_error={str(exc)[:80]}"
                        )
                    if judge_result:
                        judge_conf = str(judge_result.get("confidence", "low"))
                        judge_reason = str(judge_result.get("reason", "equivalent"))
                        if (
                            judge_result.get("equivalent") is True
                            and self._judge_meets_confidence_threshold(judge_conf)
                        ):
                            needs_review = False
                            agreement_score = f"{len(models)}/{len(models)}"
                            notes = (
                                f"LLM judge matched ({judge_conf}): {judge_reason}"
                            )
                        elif judge_result.get("equivalent") is False:
                            notes = (
                                f"{notes}; LLM judge mismatch ({judge_conf}): {judge_reason}"
                                if notes
                                else f"LLM judge mismatch ({judge_conf}): {judge_reason}"
                            )
                        else:
                            notes = (
                                f"{notes}; LLM judge inconclusive ({judge_conf}): {judge_reason}"
                                if notes
                                else f"LLM judge inconclusive ({judge_conf}): {judge_reason}"
                            )

        return FieldComparison(
            item_id=item_id,
            field_path=field_path,
            model_values=model_values,
            agreement_score=agreement_score,
            needs_review=needs_review,
            notes=notes,
            present_models=present_models,
            missing_models=missing_models,
            paired_by_judge=bool(pair_metadata.get("paired_by_judge", False)),
            pairing_confidence=str(pair_metadata.get("confidence") or ""),
            pairing_reason=str(pair_metadata.get("reason") or ""),
            row_id=row_id,
            match_method=match_method,
        )

    def _count_agreement_for_field(
        self, *, field_name: str, normalized_values: Dict[str, Any]
    ) -> int:
        """Count agreement with semantic equivalence for value/unit/time fields."""
        if field_name in {"unit", "units"}:
            canonicalized = {
                model: canonicalize_unit(value, self.unit_equivalence_index)
                for model, value in normalized_values.items()
            }
            return self._count_agreement(canonicalized)

        if field_name == "value":
            values = list(normalized_values.values())
            if len(values) == 2 and self._values_semantically_equivalent(
                values[0], values[1]
            ):
                return 2

        return self._count_agreement(normalized_values)

    def _resolve_measurement_pair_equivalence(
        self,
        *,
        comparisons: List[FieldComparison],
        models: List[str],
        missing_models: List[str],
    ) -> None:
        """Clear false conflicts when models encode the same measurement differently."""
        if missing_models:
            return
        if len(models) < 2:
            return

        value_comp = next(
            (
                comp
                for comp in comparisons
                if comp.field_path.split(".")[-1].lower() == "value"
            ),
            None,
        )
        unit_comp = next(
            (
                comp
                for comp in comparisons
                if comp.field_path.split(".")[-1].lower() in {"unit", "units"}
            ),
            None,
        )
        if value_comp is None:
            return

        canonical_signatures: List[Tuple[str, Optional[str]]] = []
        for model in models:
            value = value_comp.model_values.get(model)
            unit = unit_comp.model_values.get(model) if unit_comp is not None else None
            signature = canonicalize_measurement(
                value, unit, self.unit_equivalence_index
            )
            if signature is None:
                return
            canonical_signatures.append(signature)

        if len(set(canonical_signatures)) != 1:
            return

        for comp in comparisons:
            if comp.field_path.split(".")[-1].lower() not in {"value", "unit", "units"}:
                continue
            if not comp.needs_review:
                continue
            comp.needs_review = False
            comp.agreement_score = f"{len(models)}/{len(models)}"
            comp.notes = "normalized-equivalent measurement"

    def _align_items(
        self, item_arrays: Dict[str, List[dict]], models: List[str]
    ) -> List[Dict[str, Any]]:
        """Align items across models using exact keys, fallback signatures, then judge pairing."""
        if self.comparison_approach == "text_review":
            item_arrays = self._collapse_text_review_duplicates(item_arrays)
            item_arrays = self._merge_text_review_duplicate_keys(item_arrays)

        exact_groups: Dict[str, Dict[Tuple, List[dict]]] = {model: {} for model in models}
        for model in models:
            for item in item_arrays.get(model, []):
                key = self._build_match_key(
                    item, use_fuzzy=bool(self.fuzzy_match_fields)
                )
                exact_groups[model].setdefault(key, []).append(item)

        aligned_rows: List[Dict[str, Any]] = []
        row_counter = 0
        consumed_ids: Dict[str, set[int]] = {model: set() for model in models}

        # Pass 1: exact-key alignment only when key exists in at least 2 models.
        for key in sorted({k for model in models for k in exact_groups[model].keys()}, key=str):
            per_model = {model: exact_groups[model].get(key, []) for model in models}
            contributing_models = [m for m in models if per_model[m]]
            if len(contributing_models) < 2:
                continue
            max_len = max((len(items) for items in per_model.values()), default=0)
            for idx in range(max_len):
                row_counter += 1
                row_items: Dict[str, Optional[dict]] = {}
                present = 0
                for model in models:
                    model_items = per_model[model]
                    item = model_items[idx] if idx < len(model_items) else None
                    row_items[model] = item
                    if item is not None:
                        consumed_ids[model].add(id(item))
                        present += 1
                if present < 2:
                    continue
                aligned_rows.append(
                    {
                        "row_id": f"row-{row_counter}",
                        "item_key": key,
                        "items": row_items,
                        "match_method": "exact",
                    }
                )

        leftovers: Dict[str, List[dict]] = {model: [] for model in models}
        for model in models:
            for item in item_arrays.get(model, []):
                if id(item) not in consumed_ids[model]:
                    leftovers[model].append(item)

        # Pass 2: text fallback signature matching across leftover one-sided rows.
        if self._uses_text_fallback_matching():
            if len(models) == 2 and self.comparison_approach == "mixed":
                model_a, model_b = models[0], models[1]
                remaining_a = [
                    item
                    for item in leftovers[model_a]
                    if id(item) not in consumed_ids[model_a]
                ]
                remaining_b = [
                    item
                    for item in leftovers[model_b]
                    if id(item) not in consumed_ids[model_b]
                ]
                candidate_pairs: List[Tuple[float, dict, dict]] = []
                for item_a in remaining_a:
                    for item_b in remaining_b:
                        score = self._semantic_alignment_score(item_a, item_b)
                        if score >= self.semantic_match_threshold:
                            candidate_pairs.append((score, item_a, item_b))
                candidate_pairs.sort(key=lambda row: row[0], reverse=True)
                matched_a: set[int] = set()
                matched_b: set[int] = set()
                for _, item_a, best_item_b in candidate_pairs:
                    if id(item_a) in matched_a or id(best_item_b) in matched_b:
                        continue
                    row_counter += 1
                    aligned_rows.append(
                        {
                            "row_id": f"row-{row_counter}",
                            "item_key": self._build_match_key(
                                item_a, use_fuzzy=True
                            ),
                            "items": {
                                model_a: item_a,
                                model_b: best_item_b,
                            },
                            "match_method": "semantic",
                        }
                    )
                    consumed_ids[model_a].add(id(item_a))
                    consumed_ids[model_b].add(id(best_item_b))
                    matched_a.add(id(item_a))
                    matched_b.add(id(best_item_b))
            else:
                sig_groups: Dict[str, Dict[str, List[dict]]] = {}
                for model in models:
                    for item in leftovers[model]:
                        signature = self._build_text_review_signature(item)
                        if not signature:
                            continue
                        sig_groups.setdefault(signature, {}).setdefault(model, []).append(item)

                for signature, per_model_items in sorted(sig_groups.items(), key=lambda x: x[0]):
                    participating_models = [m for m, items in per_model_items.items() if items]
                    if len(participating_models) < 2:
                        continue
                    max_len = max(len(per_model_items.get(model, [])) for model in models)
                    for idx in range(max_len):
                        row_counter += 1
                        row_items: Dict[str, Optional[dict]] = {model: None for model in models}
                        present = 0
                        for model in models:
                            model_items = per_model_items.get(model, [])
                            item = model_items[idx] if idx < len(model_items) else None
                            if item is not None and id(item) not in consumed_ids[model]:
                                row_items[model] = item
                                consumed_ids[model].add(id(item))
                                present += 1
                        if present < 2:
                            continue
                        aligned_rows.append(
                            {
                                "row_id": f"row-{row_counter}",
                                "item_key": ("fallback", signature),
                                "items": row_items,
                                "match_method": "fuzzy",
                            }
                        )

        # Pass 3: judge pair matching for two-model mixed lanes.
        if self._uses_judge_pair_matching(models):
            model_a, model_b = models[0], models[1]
            remaining_a = [item for item in leftovers[model_a] if id(item) not in consumed_ids[model_a]]
            remaining_b = [item for item in leftovers[model_b] if id(item) not in consumed_ids[model_b]]
            max_pair_calls = int((self._judge_config or {}).get("max_pairing_calls", 25) or 25)
            min_pair_overlap = float((self._judge_config or {}).get("min_pair_overlap", 0.2) or 0.2)
            pair_calls = 0
            matched_b_ids: set[int] = set()
            for item_a in remaining_a:
                if pair_calls >= max_pair_calls:
                    break
                candidates: List[Tuple[float, dict]] = []
                for item_b in remaining_b:
                    if id(item_b) in matched_b_ids:
                        continue
                    overlap = self._pair_overlap_score(item_a, item_b)
                    if overlap >= min_pair_overlap:
                        candidates.append((overlap, item_b))
                if not candidates:
                    continue
                candidates.sort(key=lambda x: x[0], reverse=True)
                pair_calls += 1
                candidate = candidates[0][1]
                judge_match = self._judge_item_pair_equivalence(
                    model_a=model_a,
                    item_a=item_a,
                    model_b=model_b,
                    item_b=candidate,
                )
                if not judge_match or not judge_match.get("equivalent"):
                    continue
                row_counter += 1
                paired_a = dict(item_a)
                paired_b = dict(candidate)
                pair_info = {
                    "paired_by_judge": True,
                    "confidence": str(judge_match.get("confidence") or ""),
                    "reason": str(judge_match.get("reason") or ""),
                }
                paired_a["_pairing_info"] = pair_info
                paired_b["_pairing_info"] = pair_info
                aligned_rows.append(
                    {
                        "row_id": f"row-{row_counter}",
                        "item_key": ("judge_pair", row_counter),
                        "items": {model_a: paired_a, model_b: paired_b},
                        "match_method": "judge_pair",
                    }
                )
                consumed_ids[model_a].add(id(item_a))
                consumed_ids[model_b].add(id(candidate))
                matched_b_ids.add(id(candidate))

        # Pass 3.5: absorb text-review subsumed one-sided leftovers (coverage by broader rows).
        if self.comparison_approach == "text_review":
            for source_model in models:
                for source_item in item_arrays.get(source_model, []):
                    if id(source_item) in consumed_ids[source_model]:
                        continue
                    source_category = self._normalize_match_label(
                        get_nested_value(source_item, self.match_fields[0])
                        if self.match_fields
                        else None
                    )
                    source_text = self._build_text_review_raw_text(source_item)
                    source_tokens = self._build_text_review_token_set(source_item)
                    if not source_category or not source_text or len(source_tokens) < 3:
                        continue
                    subsumed_everywhere = True
                    for other_model in models:
                        if other_model == source_model:
                            continue
                        candidates = []
                        for candidate in item_arrays.get(other_model, []):
                            candidate_category = self._normalize_match_label(
                                get_nested_value(candidate, self.match_fields[0])
                                if self.match_fields
                                else None
                            )
                            if candidate_category != source_category:
                                continue
                            if not self._text_review_item_subsumes(
                                candidate,
                                source_item,
                                source_text,
                                source_tokens,
                            ):
                                continue
                            candidates.append(candidate)
                        if len(candidates) != 1:
                            subsumed_everywhere = False
                            break
                    if subsumed_everywhere:
                        consumed_ids[source_model].add(id(source_item))

        # Pass 3.6: absorb mixed-lane one-sided leftovers subsumed by an already aligned row.
        if self.comparison_approach == "mixed":
            for source_model in models:
                for source_item in item_arrays.get(source_model, []):
                    if id(source_item) in consumed_ids[source_model]:
                        continue
                    if self._is_subsumed_by_aligned_row(
                        source_model=source_model,
                        source_item=source_item,
                        aligned_rows=aligned_rows,
                        models=models,
                    ):
                        consumed_ids[source_model].add(id(source_item))

        # Pass 4: unmatched leftovers routed as one-sided presence rows.
        for model in models:
            for item in item_arrays.get(model, []):
                if id(item) in consumed_ids[model]:
                    continue
                row_counter += 1
                row_items = {m: None for m in models}
                row_items[model] = item
                match_method = "unmatched"
                if self._is_scope_split_unmatched(
                    source_model=model,
                    source_item=item,
                    aligned_rows=aligned_rows,
                    models=models,
                ):
                    match_method = "scope_split"
                aligned_rows.append(
                    {
                        "row_id": f"row-{row_counter}",
                        "item_key": self._build_match_key(item, use_fuzzy=True),
                        "items": row_items,
                        "match_method": match_method,
                    }
                )

        return aligned_rows

    def _is_subsumed_by_aligned_row(
        self,
        *,
        source_model: str,
        source_item: dict,
        aligned_rows: List[Dict[str, Any]],
        models: List[str],
    ) -> bool:
        """Return True if a one-sided mixed-lane row is covered by a broader aligned row."""
        source_feature = self._normalize_match_label(
            get_nested_value(source_item, self.match_fields[0])
            if self.match_fields
            else None
        )
        source_text = self._build_text_review_raw_text(source_item) or ""
        source_tokens = extract_key_tokens(source_text)
        source_value = normalize_value(get_nested_value(source_item, "value"))
        source_obligation = self._normalize_match_label(
            get_nested_value(source_item, "obligation")
        )
        if not source_feature:
            return False
        for row in aligned_rows:
            row_items = row.get("items") or {}
            peer_source = row_items.get(source_model)
            if peer_source is None:
                continue
            if not any(
                row_items.get(model) is not None
                for model in models
                if model != source_model
            ):
                continue
            peer_feature = self._normalize_match_label(
                get_nested_value(peer_source, self.match_fields[0])
                if self.match_fields
                else None
            )
            if peer_feature != source_feature:
                continue
            peer_value = normalize_value(get_nested_value(peer_source, "value"))
            if (
                source_value is not None
                and peer_value is not None
                and not self._values_semantically_equivalent(
                    source_value, peer_value
                )
            ):
                continue
            peer_obligation = self._normalize_match_label(
                get_nested_value(peer_source, "obligation")
            )
            if (
                source_obligation
                and peer_obligation
                and source_obligation != peer_obligation
            ):
                continue
            peer_text = self._build_text_review_raw_text(peer_source) or ""
            peer_tokens = extract_key_tokens(peer_text)
            if not source_tokens or not peer_tokens:
                continue
            overlap = len(source_tokens.intersection(peer_tokens)) / max(
                1, len(source_tokens.union(peer_tokens))
            )
            if overlap >= 0.45:
                return True
            if (
                self._is_temporal_feature(source_feature)
                and source_value is not None
                and peer_value is not None
                and self._values_semantically_equivalent(source_value, peer_value)
                and overlap >= 0.15
            ):
                return True
        return False

    def _is_scope_split_unmatched(
        self,
        *,
        source_model: str,
        source_item: dict,
        aligned_rows: List[Dict[str, Any]],
        models: List[str],
    ) -> bool:
        """Detect likely scope split/merge cases to avoid labeling as pure missing."""
        source_feature = self._normalize_match_label(
            get_nested_value(source_item, self.match_fields[0])
            if self.match_fields
            else None
        )
        source_value = normalize_value(get_nested_value(source_item, "value"))
        source_obligation = self._normalize_match_label(
            get_nested_value(source_item, "obligation")
        )
        source_tokens = self._build_text_review_token_set(source_item)
        if not source_feature:
            return False
        for row in aligned_rows:
            row_items = row.get("items") or {}
            peer_source = row_items.get(source_model)
            if peer_source is None:
                continue
            if not any(
                row_items.get(model) is not None
                for model in models
                if model != source_model
            ):
                continue
            peer_feature = self._normalize_match_label(
                get_nested_value(peer_source, self.match_fields[0])
                if self.match_fields
                else None
            )
            if peer_feature != source_feature:
                continue
            peer_value = normalize_value(get_nested_value(peer_source, "value"))
            if (
                source_value is not None
                and peer_value is not None
                and not self._values_semantically_equivalent(
                    source_value, peer_value
                )
            ):
                continue
            peer_obligation = self._normalize_match_label(
                get_nested_value(peer_source, "obligation")
            )
            if (
                source_obligation
                and peer_obligation
                and source_obligation != peer_obligation
            ):
                continue
            if source_tokens:
                peer_tokens = self._build_text_review_token_set(peer_source)
                if peer_tokens:
                    overlap = len(source_tokens.intersection(peer_tokens)) / max(
                        1, len(source_tokens.union(peer_tokens))
                    )
                    if overlap >= 0.35:
                        return True
                    if (
                        self._is_temporal_feature(source_feature)
                        and source_value is not None
                        and peer_value is not None
                        and self._values_semantically_equivalent(
                            source_value, peer_value
                        )
                        and overlap >= 0.10
                    ):
                        return True
                    continue
            return True
        return False

    def _semantic_alignment_score(self, item_a: dict, item_b: dict) -> float:
        """Score whether two unmatched rows likely represent the same requirement."""
        feature_a = self._normalize_match_label(
            get_nested_value(item_a, self.match_fields[0]) if self.match_fields else None
        )
        feature_b = self._normalize_match_label(
            get_nested_value(item_b, self.match_fields[0]) if self.match_fields else None
        )
        feature_score = 0.0
        if feature_a and feature_b:
            if feature_a == feature_b:
                feature_score = 1.0
            else:
                tokens_a = extract_key_tokens(feature_a)
                tokens_b = extract_key_tokens(feature_b)
                if tokens_a and tokens_b:
                    feature_score = len(tokens_a.intersection(tokens_b)) / len(
                        tokens_a.union(tokens_b)
                    )

        text_a = self._build_text_review_raw_text(item_a) or ""
        text_b = self._build_text_review_raw_text(item_b) or ""
        text_tokens_a = extract_key_tokens(text_a)
        text_tokens_b = extract_key_tokens(text_b)
        text_score = 0.0
        if text_tokens_a and text_tokens_b:
            text_score = len(text_tokens_a.intersection(text_tokens_b)) / len(
                text_tokens_a.union(text_tokens_b)
            )

        subject_score = 0.0
        if len(self.match_fields) > 1:
            subject_a_parts: List[str] = []
            subject_b_parts: List[str] = []
            for field_path in self.match_fields[1:]:
                val_a = self._normalize_match_label(get_nested_value(item_a, field_path))
                val_b = self._normalize_match_label(get_nested_value(item_b, field_path))
                if val_a:
                    subject_a_parts.append(val_a)
                if val_b:
                    subject_b_parts.append(val_b)
            if subject_a_parts and subject_b_parts:
                tokens_a = extract_key_tokens(" ".join(subject_a_parts))
                tokens_b = extract_key_tokens(" ".join(subject_b_parts))
                if tokens_a and tokens_b:
                    subject_score = len(tokens_a.intersection(tokens_b)) / len(
                        tokens_a.union(tokens_b)
                    )

        value_a = normalize_value(get_nested_value(item_a, "value"))
        value_b = normalize_value(get_nested_value(item_b, "value"))
        values_match = value_a is not None and value_b is not None and self._values_semantically_equivalent(value_a, value_b)
        if (
            value_a is not None
            and value_b is not None
            and not self._values_semantically_equivalent(value_a, value_b)
            and is_numeric_value(value_a)
            and is_numeric_value(value_b)
        ):
            return 0.0

        units_a = canonicalize_unit(
            get_nested_value(item_a, "units") or get_nested_value(item_a, "unit"),
            self.unit_equivalence_index,
        )
        units_b = canonicalize_unit(
            get_nested_value(item_b, "units") or get_nested_value(item_b, "unit"),
            self.unit_equivalence_index,
        )
        units_match = bool(units_a and units_b and units_a == units_b)

        obligation_a = self._normalize_match_label(get_nested_value(item_a, "obligation"))
        obligation_b = self._normalize_match_label(get_nested_value(item_b, "obligation"))
        obligation_match = bool(obligation_a and obligation_b and obligation_a == obligation_b)

        score = (0.30 * feature_score) + (0.30 * text_score) + (0.30 * subject_score)
        if values_match:
            score += 0.30
        if units_match:
            score += 0.10
        if obligation_match:
            score += 0.10
        return min(score, 1.0)

    def _normalize_time_value(self, value: Any) -> Optional[str]:
        raw = self._normalize_match_label(value)
        if not raw:
            return None
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{hour:02d}:{minute:02d}"
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", raw)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or "0")
            marker = m.group(3)
            if not (1 <= hour <= 12 and 0 <= minute <= 59):
                return None
            if marker == "p" and hour != 12:
                hour += 12
            if marker == "a" and hour == 12:
                hour = 0
            return f"{hour:02d}:{minute:02d}"
        return None

    def _values_semantically_equivalent(self, value_a: Any, value_b: Any) -> bool:
        if value_a == value_b:
            return True
        measurement_a = canonicalize_measurement(
            value_a, equivalence_index=self.unit_equivalence_index
        )
        measurement_b = canonicalize_measurement(
            value_b, equivalence_index=self.unit_equivalence_index
        )
        if measurement_a and measurement_b and measurement_a == measurement_b:
            return True
        time_a = self._normalize_time_value(value_a)
        time_b = self._normalize_time_value(value_b)
        if time_a and time_b and time_a == time_b:
            return True
        return False

    def _is_temporal_feature(self, feature_label: str) -> bool:
        label = str(feature_label or "").lower()
        return any(token in label for token in ("time", "hour", "hours", "start", "end"))

    def _build_match_key(self, item: dict, *, use_fuzzy: bool) -> Tuple:
        """Build a normalized match key from configured match fields."""
        values: List[Any] = []
        fuzzy_fields = set(self.fuzzy_match_fields if use_fuzzy else [])
        for field_path in self.match_fields:
            value = get_nested_value(item, field_path)
            if value is None:
                values.append(None)
                continue
            if isinstance(value, str):
                if field_path in fuzzy_fields:
                    values.append(normalize_for_matching(value))
                else:
                    values.append(value.strip().lower())
                continue
            values.append(value)
        return tuple(values)

    def _judge_text_equivalence(
        self,
        item_id: str,
        field_path: str,
        model_values: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Optionally run an LLM-as-judge pass for text-review disagreements."""
        if not self._judge_config.get("enabled"):
            return None
        judge_model = self._judge_runtime.get("model")
        if not judge_model:
            return None

        values = {k: v for k, v in model_values.items() if v is not None}
        if len(values) < 2:
            return None
        unique_values = {str(v).strip().lower() for v in values.values()}
        if len(unique_values) <= 1:
            return {"equivalent": True, "confidence": "high", "reason": "exact_match"}

        from ..extraction.llm_client import LLMClient

        if self._judge_client is None:
            self._judge_client = LLMClient(
                api_key=self._judge_runtime.get("api_key"),
                model=judge_model,
                provider=self._judge_runtime.get("provider"),
                azure_endpoint=self._judge_runtime.get("azure_endpoint"),
                azure_api_version=self._judge_runtime.get("azure_api_version"),
                base_url=self._judge_runtime.get("base_url"),
            )

        judge_schema = {
            "type": "object",
            "properties": {
                "equivalent": {"type": "boolean"},
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
                "reason": {"type": "string"},
            },
            "required": ["equivalent", "confidence", "reason"],
        }
        values_blob = "\n".join(
            f"- {model}: {value}" for model, value in sorted(values.items())
        )
        strictness = (
            self._judge_config.get("strictness")
            or "Conservative: if uncertain, mark not equivalent."
        )
        prompt = (
            "You are a strict QA judge. Decide whether values are semantically equivalent.\n"
            "Never infer unseen facts. If there is any ambiguity, return equivalent=false.\n"
            f"Policy: {strictness}\n"
            f"Item: {item_id}\nField: {field_path}\nValues:\n{values_blob}"
        )
        self._judge_stats["attempted_calls"] += 1
        judged = self._judge_client.extract(
            text=prompt,
            schema=judge_schema,
            system_prompt=(
                "Return only JSON that conforms to schema. "
                "Be conservative and do not guess."
            ),
        )
        self._judge_stats["successful_calls"] += 1
        self._judge_stats["input_tokens"] += int(judged.get("input_tokens") or 0)
        self._judge_stats["output_tokens"] += int(
            judged.get("output_tokens") or 0
        )
        self._judge_stats["total_cost_usd"] += float(judged.get("cost") or 0.0)
        payload = judged.get("data") if isinstance(judged, dict) else None
        if not isinstance(payload, dict):
            return None
        return payload

    def _should_attempt_judge(
        self,
        *,
        field_path: str,
        model_values: Dict[str, Any],
    ) -> bool:
        """Decide if judge should run for this mismatch while controlling cost."""
        if not self._judge_config.get("enabled"):
            return False
        if not self._judge_runtime.get("model"):
            return False
        values = [value for value in model_values.values() if value is not None]
        if len(values) < 2:
            return False
        unique_values = {str(value).strip().lower() for value in values}
        if len(unique_values) <= 1:
            return False

        if self.comparison_approach == "text_review":
            return True
        if self.comparison_approach != "mixed":
            return False

        apply_on = str(self._judge_config.get("apply_on", "none")).strip().lower()
        if apply_on in {"none", "off", "disabled"}:
            return False
        if apply_on == "all":
            return True

        # Cost-optimized default for mixed lanes:
        # skip numeric-like mismatches and judge only non-numeric text/enum values.
        has_numeric = any(is_numeric_value(value) for value in values)
        if apply_on in {"non_numeric", "text", "text_only", "categorical"}:
            return not has_numeric
        return False

    def _judge_meets_confidence_threshold(self, confidence: str) -> bool:
        """Apply configurable minimum judge confidence for auto-match decisions."""
        threshold = str(
            self._judge_config.get("min_confidence", "high")
        ).strip().lower()
        rank = {"low": 1, "medium": 2, "high": 3}
        return rank.get(str(confidence).strip().lower(), 0) >= rank.get(threshold, 3)

    def _count_agreement(self, model_values: Dict[str, Any]) -> int:
        """Count how many models agree (most common value)."""
        values = list(model_values.values())
        if not values:
            return 0

        # Treat None as a valid value for agreement
        sentinel = "__NONE__"
        normalized = [sentinel if v is None else v for v in values]

        most_common = max(set(normalized), key=normalized.count)
        return normalized.count(most_common)

    def _calculate_summary(
        self,
        context_comparisons: List[FieldComparison],
        item_comparisons: List[FieldComparison],
        num_models: int,
        item_arrays: Dict[str, List[dict]],
        context_skipped: int,
        item_skipped: int,
        potential_duplicates: List[PotentialDuplicate] = None,
        completeness: Dict[str, "CompletenessResult"] = None,
    ) -> Dict[str, Any]:
        """Calculate summary statistics."""
        potential_duplicates = potential_duplicates or []
        completeness = completeness or {}

        # Count agreements
        context_agree = sum(
            1
            for c in context_comparisons
            if c.agreement_score == f"{num_models}/{num_models}"
        )
        item_agree = sum(
            1
            for c in item_comparisons
            if c.agreement_score == f"{num_models}/{num_models}"
        )

        total = len(context_comparisons) + len(item_comparisons)
        full_agree = context_agree + item_agree
        needs_review = total - full_agree

        summary = {
            # Comparison approach
            "comparison_approach": self.comparison_approach,
            "qaqc_config_source": self.config_source,
            "qaqc_profile": self.lane_name,
            "skipped_non_numeric": context_skipped + item_skipped,
            # Item counts per model
            "items_per_model": {
                m: len(items) for m, items in item_arrays.items()
            },
            # Overall stats
            "total_comparisons": total,
            "full_agreement_count": full_agree,
            "full_agreement_pct": round(full_agree / total * 100, 1)
            if total
            else 0.0,
            "needs_review_count": needs_review,
            "needs_review_pct": round(needs_review / total * 100, 1)
            if total
            else 0.0,
            # Context breakdown
            "context_comparisons": len(context_comparisons),
            "context_agreement_pct": round(
                context_agree / len(context_comparisons) * 100, 1
            )
            if context_comparisons
            else 0.0,
            # Item breakdown
            "item_comparisons": len(item_comparisons),
            "item_agreement_pct": round(
                item_agree / len(item_comparisons) * 100, 1
            )
            if item_comparisons
            else 0.0,
            "review_category_counts": self._calculate_review_category_counts(
                item_comparisons
            ),
            "potential_duplicates_count": len(potential_duplicates),
            "completeness_per_model": {
                model: {
                    "items_extracted": result.items_extracted,
                    "expected_found": result.expected_found,
                    "expected_total": result.expected_total,
                    "completeness_score": round(
                        result.completeness_score * 100, 1
                    ),
                }
                for model, result in completeness.items()
            }
            if completeness
            else {},
        }
        if self._judge_config.get("enabled"):
            judge_matches = sum(
                1
                for c in item_comparisons
                if c.notes.startswith("LLM judge matched")
                or c.notes.startswith("LLM row judge matched")
            )
            judge_errors = sum(
                1 for c in item_comparisons if "judge_error=" in (c.notes or "")
            )
            summary["judge"] = {
                "enabled": True,
                "model": self._judge_runtime.get("model"),
                "resolved_matches": judge_matches,
                "errors": judge_errors,
                "attempted_calls": int(
                    (self._judge_stats or {}).get("attempted_calls", 0)
                ),
                "successful_calls": int(
                    (self._judge_stats or {}).get("successful_calls", 0)
                ),
                "input_tokens": int(
                    (self._judge_stats or {}).get("input_tokens", 0)
                ),
                "output_tokens": int(
                    (self._judge_stats or {}).get("output_tokens", 0)
                ),
                "total_cost_usd": float(
                    (self._judge_stats or {}).get("total_cost_usd", 0.0)
                ),
            }

        qualitative_breakdown = self._build_qualitative_mismatch_breakdown(
            item_comparisons
        )
        if qualitative_breakdown is not None:
            summary["qualitative_mismatch_breakdown"] = qualitative_breakdown

        qualitative_gate = self._build_qualitative_advisory_gate(
            summary["review_category_counts"]
        )
        if qualitative_gate is not None:
            summary["qualitative_advisory_gate"] = qualitative_gate

        return summary

    def _calculate_review_category_counts(
        self, item_comparisons: List[FieldComparison]
    ) -> Dict[str, int]:
        """Compute stable review categories for report-layer policy labeling."""
        counts: Dict[str, int] = {
            "aligned": 0,
            "missing_item": 0,
            "scope_variant": 0,
            "text_difference": 0,
            "value_difference": 0,
        }

        for comparison in item_comparisons:
            counts[self._classify_review_category(comparison)] += 1

        return {key: value for key, value in counts.items() if value > 0}

    def _classify_review_category(self, comparison: FieldComparison) -> str:
        """Classify a comparison into a stable benchmark/report review category."""
        if not comparison.needs_review:
            return "aligned"

        if comparison.missing_models:
            if (
                self.comparison_approach == "text_review"
                and self._is_text_review_scope_variant(comparison.item_id)
            ):
                return "scope_variant"
            return "missing_item"

        notes_lower = (comparison.notes or "").lower()
        if "item not extracted by" in notes_lower:
            if (
                self.comparison_approach == "text_review"
                and self._is_text_review_scope_variant(comparison.item_id)
            ):
                return "scope_variant"
            return "missing_item"

        if self.comparison_approach == "text_review":
            return "text_difference"
        return "value_difference"

    def _build_qualitative_advisory_gate(
        self,
        review_category_counts: Dict[str, int],
    ) -> Optional[Dict[str, Any]]:
        """Build an advisory qualitative gate summary for text-review lanes only."""
        if self.comparison_approach != "text_review":
            return None

        evaluated_counts = {
            key: value
            for key, value in review_category_counts.items()
            if key != "scope_variant"
        }
        excluded_scope_variants = review_category_counts.get(
            "scope_variant", 0
        )
        total = sum(evaluated_counts.values())
        if total <= 0:
            return {
                "mode": "advisory",
                "status": "not_applicable",
                "aligned_pct": 0.0,
                "missing_item_pct": 0.0,
                "dominant_category": "none",
                "evaluated_comparisons": 0,
                "excluded_scope_variants": excluded_scope_variants,
                "recommended_action": "No qualitative comparisons were produced for gate evaluation.",
            }

        aligned_count = evaluated_counts.get("aligned", 0)
        missing_count = evaluated_counts.get("missing_item", 0)
        aligned_pct = round(aligned_count / total * 100, 1)
        missing_pct = round(missing_count / total * 100, 1)
        dominant_category = max(
            sorted(evaluated_counts.items()),
            key=lambda item: item[1],
        )[0]

        if aligned_pct >= 80.0 and missing_pct <= 10.0:
            status = "pass"
            recommended_action = "Qualitative review is mostly aligned; spot-check flagged differences before benchmark gating."
        elif aligned_pct >= 50.0 and missing_pct <= 25.0:
            status = "warn"
            recommended_action = "Qualitative review shows moderate divergence; inspect text differences before relying on this run."
        else:
            status = "fail"
            recommended_action = "Qualitative review divergence is high; treat this run as not ready for qualitative benchmark gating."

        if excluded_scope_variants > 0:
            recommended_action = f"{recommended_action} {excluded_scope_variants} auxiliary scope-variant row(s) were excluded from gate math."

        return {
            "mode": "advisory",
            "status": status,
            "aligned_pct": aligned_pct,
            "missing_item_pct": missing_pct,
            "dominant_category": dominant_category,
            "evaluated_comparisons": total,
            "excluded_scope_variants": excluded_scope_variants,
            "recommended_action": recommended_action,
        }

    def _build_qualitative_mismatch_breakdown(
        self,
        item_comparisons: List[FieldComparison],
    ) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        """Summarize the dominant qualitative mismatch classes for text-review lanes."""
        if self.comparison_approach != "text_review":
            return None

        missing_item_by_category: Counter[str] = Counter()
        scope_variant_by_category: Counter[str] = Counter()
        text_difference_by_category: Counter[str] = Counter()
        missing_item_requirements: Counter[str] = Counter()
        scope_variant_requirements: Counter[str] = Counter()
        text_difference_requirements: Counter[str] = Counter()

        for comparison in item_comparisons:
            review_category = self._classify_review_category(comparison)
            if review_category == "aligned":
                continue

            category_label, requirement_label = (
                self._extract_requirement_labels(comparison.item_id)
            )
            if review_category == "missing_item":
                missing_item_by_category[category_label] += 1
                missing_item_requirements[requirement_label] += 1
            elif review_category == "scope_variant":
                scope_variant_by_category[category_label] += 1
                scope_variant_requirements[requirement_label] += 1
            else:
                text_difference_by_category[category_label] += 1
                text_difference_requirements[requirement_label] += 1

        return {
            "missing_item_by_category": self._serialize_counter(
                missing_item_by_category
            ),
            "scope_variant_by_category": self._serialize_counter(
                scope_variant_by_category
            ),
            "text_difference_by_category": self._serialize_counter(
                text_difference_by_category
            ),
            "top_missing_requirements": self._serialize_counter(
                missing_item_requirements
            ),
            "top_scope_variant_requirements": self._serialize_counter(
                scope_variant_requirements
            ),
            "top_text_difference_requirements": self._serialize_counter(
                text_difference_requirements
            ),
        }

    def _extract_requirement_labels(self, item_id: str) -> Tuple[str, str]:
        """Split a readable item id into category and full requirement labels."""
        parts = [part.strip() for part in item_id.split("|")]
        category_label = parts[0] if parts else "Unknown"
        requirement_label = " | ".join(parts[:3]) if parts else item_id
        return category_label, requirement_label

    def _is_text_review_scope_variant(self, item_id: str) -> bool:
        """Return True when an item matches a configured scope-variant pattern.

        Scope-variant items represent narrow auxiliary rows that models
        legitimately disagree on by design (e.g. domain-specific auxiliary
        requirements). They are excluded from qualitative advisory gate math.

        Patterns are declared in the lane config under ``scope_variant_keys``
        as a list of [category, subject] pairs. No domain-specific patterns
        are hardcoded here — this is a universal tool.
        """
        if not self._scope_variant_keys:
            return False
        category_label, _, subject_label = self._extract_requirement_triplet(
            item_id
        )
        return (category_label, subject_label) in self._scope_variant_keys

    def _extract_requirement_triplet(
        self, item_id: str
    ) -> Tuple[str, str, str]:
        """Return normalized category/facility/subject labels from a readable requirement id."""
        parts = [
            self._normalize_match_label(part) or ""
            for part in item_id.split("|")
        ]
        while len(parts) < 3:
            parts.append("")
        return parts[0], parts[1], parts[2]

    def _serialize_counter(
        self, counts: Counter[str], limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Return deterministic top-N counter rows for reports and machine-readable output."""
        return [
            {"label": label, "count": count}
            for label, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0])
            )[:limit]
        ]

    # --- Helper Methods ---

    def _load_outputs(self, output_files: Dict[str, Path]) -> Dict[str, dict]:
        """Load output JSON files."""
        outputs = {}
        for model, path in output_files.items():
            if path and Path(path).exists():
                try:
                    with open(path) as f:
                        loaded = json.load(f)
                        if isinstance(loaded, dict) and isinstance(
                            loaded.get("payload"), dict
                        ):
                            outputs[model] = loaded["payload"]
                        else:
                            outputs[model] = loaded
                except Exception as e:
                    logger.error(f"Failed to load {model}: {e}")
        return outputs

    def _extract_item_arrays(
        self, outputs: Dict[str, dict]
    ) -> Dict[str, List[dict]]:
        """Extract main data arrays."""
        arrays = {}
        for model, output in outputs.items():
            if self.projection:
                arrays[model] = self._project_item_array(output)
                continue

            items = output.get(self.main_data_array, [])
            arrays[model] = items if isinstance(items, list) else []
        return arrays

    def _project_item_array(self, output: dict) -> List[dict]:
        """Project nested extraction structures into comparison rows when configured."""
        if not isinstance(self.projection, dict):
            return []

        if self.projection.get("type") != "nested_array_items":
            logger.warning(
                "Unsupported QA/QC projection type: %s",
                self.projection.get("type"),
            )
            return []

        source_array_name = (
            self.projection.get("source_array") or self.main_data_array
        )
        nested_array_name = self.projection.get("nested_array")
        if not nested_array_name:
            return []

        parent_fields = self.projection.get("parent_fields") or []
        nested_items = output.get(source_array_name, [])
        if not isinstance(nested_items, list):
            return []

        projected: List[dict] = []
        for parent_item in nested_items:
            if not isinstance(parent_item, dict):
                continue

            child_items = parent_item.get(nested_array_name, [])
            if not isinstance(child_items, list):
                continue

            parent_projection = {
                field_name: parent_item.get(field_name)
                for field_name in parent_fields
            }

            for child_item in child_items:
                if not isinstance(child_item, dict):
                    continue
                projected.append({**parent_projection, **child_item})

        return projected

    def _build_indexes(
        self, item_arrays: Dict[str, List[dict]]
    ) -> Dict[str, Dict[Tuple, dict]]:
        """Build item indexes for matching."""
        return {
            model: create_item_index(
                items,
                self.match_fields,
                fuzzy_fields=self.fuzzy_match_fields,
            )
            for model, items in item_arrays.items()
        }

    def _collapse_text_review_duplicates(
        self,
        item_arrays: Dict[str, List[dict]],
    ) -> Dict[str, List[dict]]:
        """Collapse repeated qualitative rows within a single model before matching."""
        if not self._uses_text_fallback_matching():
            return item_arrays

        collapsed_arrays: Dict[str, List[dict]] = {}
        for model, items in item_arrays.items():
            deduped_items: List[dict] = []
            seen_signatures: set[str] = set()

            for item in items:
                signature = self._build_text_review_dedupe_signature(item)
                if signature is None or signature not in seen_signatures:
                    deduped_items.append(item)
                    if signature is not None:
                        seen_signatures.add(signature)

            collapsed_arrays[model] = deduped_items

        return collapsed_arrays

    def _merge_text_review_duplicate_keys(
        self,
        item_arrays: Dict[str, List[dict]],
    ) -> Dict[str, List[dict]]:
        """Merge text-review rows that share the exact same match key within one model."""
        if not self._uses_text_fallback_matching():
            return item_arrays

        merged_arrays: Dict[str, List[dict]] = {}
        for model, items in item_arrays.items():
            grouped_items: Dict[Tuple, dict] = {}
            group_order: List[Tuple] = []

            for item in items:
                item_key = tuple(
                    get_nested_value(item, field_path)
                    for field_path in self.match_fields
                )
                if item_key not in grouped_items:
                    grouped_items[item_key] = dict(item)
                    group_order.append(item_key)
                    continue

                grouped_items[item_key] = self._merge_text_review_items(
                    grouped_items[item_key], item
                )

            merged_arrays[model] = [
                grouped_items[item_key] for item_key in group_order
            ]

        return merged_arrays

    def _merge_text_review_items(
        self, base_item: dict, extra_item: dict
    ) -> dict:
        """Merge duplicate-key qualitative rows by concatenating distinct compare-field text."""
        merged_item = dict(base_item)
        for field_path in sorted(self.compare_fields):
            field_name = field_path.split(".")[-1]
            merged_item[field_name] = self._merge_text_review_values(
                get_nested_value(base_item, field_path),
                get_nested_value(extra_item, field_path),
            )
        return merged_item

    def _merge_text_review_values(
        self, base_value: Any, extra_value: Any
    ) -> Any:
        """Concatenate distinct qualitative text fragments while preserving order."""
        fragments: List[str] = []
        seen_normalized: set[str] = set()

        for value in (base_value, extra_value):
            normalized_value = self._normalize_match_label(value)
            if not normalized_value or normalized_value in seen_normalized:
                continue
            fragments.append(str(value).strip())
            seen_normalized.add(normalized_value)

        if not fragments:
            return base_value if base_value is not None else extra_value
        if len(fragments) == 1:
            return fragments[0]
        return " ".join(fragments)

    def _apply_text_review_fallback_matches(
        self,
        indexes: Dict[str, Dict[Tuple, dict]],
        models: List[str],
    ) -> Dict[str, Dict[Tuple, dict]]:
        """Pair unmatched qualitative items by normalized compare text when safe."""
        if not self._uses_text_fallback_matching():
            return indexes

        all_keys = self._collect_all_keys(indexes)
        unmatched_groups: Dict[Tuple, List[Tuple[str, Tuple, dict]]] = (
            defaultdict(list)
        )

        for key in all_keys:
            models_with_key = [
                model for model in models if key in indexes[model]
            ]
            if len(models_with_key) != 1:
                continue

            model = models_with_key[0]
            item = indexes[model][key]
            signature = self._build_text_review_signature(item)
            if signature is None:
                continue
            unmatched_groups[signature].append((model, key, item))

        for signature, group in unmatched_groups.items():
            models_in_group = [model for model, _, _ in group]
            if len(set(models_in_group)) < 2:
                continue

            # Skip ambiguous cases where one model has multiple unmatched items with the same text.
            if len(set(models_in_group)) != len(models_in_group):
                continue

            synthetic_key = ("__text_review__", signature)
            for model, key, item in group:
                del indexes[model][key]
                indexes[model][synthetic_key] = item

        return indexes

    def _apply_judge_pair_matches(
        self,
        indexes: Dict[str, Dict[Tuple, dict]],
        models: List[str],
    ) -> Dict[str, Dict[Tuple, dict]]:
        """Use judge to pair one-sided mixed-lane items before field comparison."""
        if not self._uses_judge_pair_matching(models):
            return indexes

        model_a, model_b = models[0], models[1]
        unmatched_a: List[Tuple[Tuple, dict]] = []
        unmatched_b: List[Tuple[Tuple, dict]] = []
        for key in self._collect_all_keys(indexes):
            present_models = [m for m in models if key in indexes[m]]
            if len(present_models) != 1:
                continue
            owner = present_models[0]
            if owner == model_a:
                unmatched_a.append((key, indexes[model_a][key]))
            elif owner == model_b:
                unmatched_b.append((key, indexes[model_b][key]))

        if not unmatched_a or not unmatched_b:
            return indexes

        max_pair_calls = int(
            (self._judge_config or {}).get("max_pairing_calls", 25) or 25
        )
        min_pair_overlap = float(
            (self._judge_config or {}).get("min_pair_overlap", 0.2) or 0.2
        )
        pair_calls = 0
        matched_b_keys: set[Tuple] = set()
        pair_counter = 0

        for key_a, item_a in unmatched_a:
            if pair_calls >= max_pair_calls:
                break
            candidates: List[Tuple[Tuple, dict, float]] = []
            for key_b, item_b in unmatched_b:
                if key_b in matched_b_keys:
                    continue
                overlap = self._pair_overlap_score(item_a, item_b)
                if overlap < min_pair_overlap:
                    continue
                candidates.append((key_b, item_b, overlap))
            if not candidates:
                continue
            candidates.sort(key=lambda row: row[2], reverse=True)
            key_b, item_b, _ = candidates[0]
            pair_calls += 1
            judge_match = self._judge_item_pair_equivalence(
                model_a=model_a,
                item_a=item_a,
                model_b=model_b,
                item_b=item_b,
            )
            if not judge_match or not judge_match.get("equivalent"):
                continue
            pair_counter += 1
            synthetic_key = ("__judge_pair__", str(pair_counter))
            paired_a = dict(item_a)
            paired_b = dict(item_b)
            paired_a["_paired_by_judge"] = True
            paired_b["_paired_by_judge"] = True
            paired_a["_pairing_info"] = {
                "paired_by_judge": True,
                "confidence": str(judge_match.get("confidence") or ""),
                "reason": str(judge_match.get("reason") or ""),
            }
            paired_b["_pairing_info"] = {
                "paired_by_judge": True,
                "confidence": str(judge_match.get("confidence") or ""),
                "reason": str(judge_match.get("reason") or ""),
            }
            indexes[model_a].pop(key_a, None)
            indexes[model_b].pop(key_b, None)
            indexes[model_a][synthetic_key] = paired_a
            indexes[model_b][synthetic_key] = paired_b
            matched_b_keys.add(key_b)

        return indexes

    def _absorb_text_review_subsumed_items(
        self,
        indexes: Dict[str, Dict[Tuple, dict]],
        models: List[str],
    ) -> Dict[str, Dict[Tuple, dict]]:
        """Drop one-sided qualitative rows when every opposing model has a unique broader row covering them."""
        if not self._uses_text_fallback_matching():
            return indexes

        unmatched_keys = [
            key
            for key in self._collect_all_keys(indexes)
            if sum(1 for model in models if key in indexes[model]) == 1
        ]

        keys_to_remove: List[Tuple[str, Tuple]] = []
        for key in unmatched_keys:
            source_models = [
                model for model in models if key in indexes[model]
            ]
            if len(source_models) != 1:
                continue

            source_model = source_models[0]
            source_item = indexes[source_model][key]
            if not self._has_text_review_subsumption(
                indexes, models, source_model, source_item
            ):
                continue

            keys_to_remove.append((source_model, key))

        for model, key in keys_to_remove:
            indexes[model].pop(key, None)

        return indexes

    def _has_text_review_subsumption(
        self,
        indexes: Dict[str, Dict[Tuple, dict]],
        models: List[str],
        source_model: str,
        source_item: dict,
    ) -> bool:
        """Return true when each opposing model has a unique broader same-category qualitative row."""
        source_category = self._normalize_match_label(
            get_nested_value(source_item, self.match_fields[0])
            if self.match_fields
            else None
        )
        source_text = self._build_text_review_raw_text(source_item)
        source_tokens = self._build_text_review_token_set(source_item)

        if not source_category or not source_text or len(source_tokens) < 3:
            return False

        for model in models:
            if model == source_model:
                continue

            candidates = []
            for candidate in indexes[model].values():
                candidate_category = self._normalize_match_label(
                    get_nested_value(candidate, self.match_fields[0])
                    if self.match_fields
                    else None
                )
                if candidate_category != source_category:
                    continue
                if not self._text_review_item_subsumes(
                    candidate, source_item, source_text, source_tokens
                ):
                    continue
                candidates.append(candidate)

            if len(candidates) != 1:
                return False

        return True

    def _text_review_item_subsumes(
        self,
        candidate: dict,
        source_item: dict,
        source_text: str,
        source_tokens: set[str],
    ) -> bool:
        """Check whether a candidate qualitative row clearly covers a narrower source row."""
        candidate_text = self._build_text_review_raw_text(candidate)
        candidate_tokens = self._build_text_review_token_set(candidate)

        if not candidate_text or not candidate_tokens:
            return False
        if candidate_text == source_text:
            return False
        if self._matches_text_review_hierarchical_rule(
            candidate, source_item, source_text, source_tokens
        ):
            return True
        if len(candidate_tokens) <= len(source_tokens):
            return False
        if source_text in candidate_text:
            return True
        return source_tokens.issubset(candidate_tokens)

    def _matches_text_review_hierarchical_rule(
        self,
        candidate: dict,
        source_item: dict,
        source_text: str,
        source_tokens: set[str],
    ) -> bool:
        """Handle qualitative parent-child coverage patterns.

        Previously contained hardcoded domain-specific patterns. Now returns
        False so that only the generic text/token-subset logic in
        _text_review_item_subsumes drives subsumption decisions.
        Domain-specific patterns can be added back via config if needed.
        """
        return False

    def _build_text_review_signature(
        self, item: Optional[dict]
    ) -> Optional[str]:
        """Build a normalized text signature for qualitative fallback matching."""
        if not isinstance(item, dict):
            return None

        signature_parts: List[str] = []
        for field_path in self._fallback_signature_paths():
            value = get_nested_value(item, field_path)
            if value is None:
                continue

            if isinstance(value, str):
                normalized_value = normalize_for_matching(value)
                if normalized_value:
                    signature_parts.append(normalized_value)
                continue

            signature_parts.append(str(value).strip().lower())

        if not signature_parts:
            return None
        return "||".join(signature_parts)

    def _uses_text_fallback_matching(self) -> bool:
        """Whether text-signature fallback matching should run for this lane."""
        if self.comparison_approach == "text_review":
            return True
        return self.comparison_approach == "mixed" and self.enable_text_fallback_matching

    def _fallback_signature_paths(self) -> List[str]:
        """Return field paths used for text fallback signatures."""
        if self.comparison_approach == "mixed" and self.text_fallback_fields:
            return sorted(set(self.text_fallback_fields))
        return sorted(self.compare_fields)

    def _uses_judge_pair_matching(self, models: List[str]) -> bool:
        """Whether mixed-lane judge-assisted pairing should run."""
        if len(models) != 2:
            return False
        if self.comparison_approach != "mixed":
            return False
        if not self.enable_judge_pair_matching:
            return False
        if not self._judge_config.get("enabled"):
            return False
        if not self._judge_runtime.get("model"):
            return False
        return str(self._judge_config.get("apply_on", "none")).strip().lower() in {
            "all",
            "non_numeric",
            "text",
            "text_only",
            "categorical",
        }

    def _pair_overlap_score(self, item_a: dict, item_b: dict) -> float:
        """Cheap prefilter score before LLM pairing."""
        text_a = self._build_text_review_raw_text(item_a) or ""
        text_b = self._build_text_review_raw_text(item_b) or ""
        tokens_a = extract_key_tokens(text_a)
        tokens_b = extract_key_tokens(text_b)
        if not tokens_a or not tokens_b:
            return 0.0
        inter = len(tokens_a.intersection(tokens_b))
        union = len(tokens_a.union(tokens_b))
        if union <= 0:
            return 0.0
        return inter / union

    def _judge_item_pair_equivalence(
        self,
        *,
        model_a: str,
        item_a: dict,
        model_b: str,
        item_b: dict,
    ) -> Optional[Dict[str, Any]]:
        """Ask judge if two unmatched items represent the same requirement."""
        fields = self._fallback_signature_paths()
        values_a = {field: get_nested_value(item_a, field) for field in fields}
        values_b = {field: get_nested_value(item_b, field) for field in fields}
        payload = {
            model_a: values_a,
            model_b: values_b,
        }
        try:
            result = self._judge_text_equivalence(
                item_id="pair_match",
                field_path="item_identity",
                model_values={model_a: payload[model_a], model_b: payload[model_b]},
            )
        except Exception:
            return None
        if not isinstance(result, dict):
            return None
        confidence = str(result.get("confidence", "low"))
        if not self._judge_meets_confidence_threshold(confidence):
            return None
        if result.get("equivalent") is not True:
            return None
        return {
            "equivalent": True,
            "confidence": confidence,
            "reason": str(result.get("reason") or ""),
        }

    def _extract_pair_metadata(
        self,
        items: Dict[str, Optional[dict]],
        present_models: List[str],
    ) -> Dict[str, Any]:
        """Extract judge-pairing metadata from present model items."""
        for model in present_models:
            item = items.get(model)
            if not isinstance(item, dict):
                continue
            info = item.get("_pairing_info")
            if isinstance(info, dict) and info.get("paired_by_judge"):
                return info
        return {}

    def _build_text_review_raw_text(
        self, item: Optional[dict]
    ) -> Optional[str]:
        """Build a normalized joined text string for qualitative overlap checks."""
        if not isinstance(item, dict):
            return None

        text_parts: List[str] = []
        for field_path in self._fallback_signature_paths():
            value = get_nested_value(item, field_path)
            normalized_value = self._normalize_match_label(value)
            if normalized_value:
                text_parts.append(normalized_value)

        if not text_parts:
            return None
        return " || ".join(text_parts)

    def _build_text_review_token_set(self, item: Optional[dict]) -> set[str]:
        """Build a token set for qualitative overlap checks."""
        raw_text = self._build_text_review_raw_text(item)
        if not raw_text:
            return set()
        return extract_key_tokens(raw_text)

    def _build_text_review_dedupe_signature(
        self, item: Optional[dict]
    ) -> Optional[str]:
        """Build a per-model duplicate signature for qualitative comparisons."""
        text_signature = self._build_text_review_signature(item)
        if text_signature is None:
            return None

        if not self.match_fields:
            return text_signature

        category_value = get_nested_value(item, self.match_fields[0])
        normalized_category = (
            normalize_for_matching(category_value) if category_value else None
        )
        if not normalized_category:
            return text_signature

        signature_parts = [normalized_category, text_signature]
        if len(self.match_fields) > 1:
            trailing_value = get_nested_value(item, self.match_fields[-1])
            normalized_trailing = self._normalize_match_label(trailing_value)
            if normalized_trailing:
                signature_parts.append(normalized_trailing)

        return "||".join(signature_parts)

    def _normalize_match_label(self, value: Any) -> Optional[str]:
        """Normalize match labels without dropping short identifiers like A/B/C."""
        if value is None:
            return None
        if isinstance(value, str):
            normalized = re.sub(r"\s+", " ", value.strip().lower())
            return normalized or None
        return str(value).strip().lower() or None

    def _format_item_id(
        self, item_key: Tuple, items: Dict[str, Optional[dict]]
    ) -> str:
        """Build a readable item identifier, including fallback text-review matches."""
        if item_key and item_key[0] == "__text_review__":
            for item in items.values():
                if not item:
                    continue
                match_values = [
                    get_nested_value(item, field_path)
                    for field_path in self.match_fields
                ]
                return " | ".join(
                    str(value) if value else "N/A" for value in match_values
                )
            return "text review match"
        if item_key and item_key[0] == "__judge_pair__":
            for item in items.values():
                if not item:
                    continue
                match_values = [
                    get_nested_value(item, field_path)
                    for field_path in self.match_fields
                ]
                return " | ".join(
                    str(value) if value else "N/A" for value in match_values
                )
            return "judge matched pair"

        return " | ".join(str(v) if v else "N/A" for v in item_key)

    def _collect_all_keys(self, indexes: Dict[str, Dict[Tuple, dict]]) -> set:
        """Collect all unique item keys."""
        all_keys = set()
        for index in indexes.values():
            all_keys.update(index.keys())
        return all_keys

    def _get_flat_fields(self, obj: dict, prefix: str = "") -> set:
        """Get all field paths from nested dict."""
        fields = set()
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                fields.update(self._get_flat_fields(value, path))
            else:
                fields.add(path)
        return fields

    # --- Potential Duplicate Detection ---

    def _detect_potential_duplicates(
        self, indexes: Dict[str, Dict[Tuple, dict]], models: List[str]
    ) -> List[PotentialDuplicate]:
        """
        Detect items that might be the same data with different keys.

        Finds ONLY items (items unique to one model) that have the same
        (value, unit) combination as ONLY items from other models.

        Example: gpt-5 has time_limit.reclamation_period=60days
                 gpt-4.1 has time_limit.drilling_operations=60days
        These might be the same source data, categorized differently.

        Args:
            indexes: Dict mapping model -> {key_tuple: item_dict}
            models: List of model names

        Returns:
            List of PotentialDuplicate instances
        """
        # First, find all keys and which models have them
        all_keys = self._collect_all_keys(indexes)

        # Identify ONLY items (present in exactly one model)
        only_items: Dict[str, List[Tuple[Tuple, dict]]] = defaultdict(list)
        for key in all_keys:
            models_with_key = [m for m in models if key in indexes[m]]
            if len(models_with_key) == 1:
                model = models_with_key[0]
                item = indexes[model][key]
                only_items[model].append((key, item))

        # Group ONLY items by (value, unit)
        value_groups: Dict[Tuple, List[Tuple[str, Tuple, dict]]] = defaultdict(
            list
        )
        for model, items in only_items.items():
            for key, item in items:
                value = item.get("value")
                unit = item.get("unit", "")
                if value is not None:
                    group_key = (value, unit)
                    value_groups[group_key].append((model, key, item))

        # Find groups with items from different models
        duplicates = []
        for (value, unit), group in value_groups.items():
            models_in_group = set(model for model, _, _ in group)
            if len(models_in_group) > 1:
                # Same value/unit from different models with different keys
                items_list = [(model, item) for model, _, item in group]
                duplicates.append(
                    PotentialDuplicate(
                        value=value,
                        unit=unit or "N/A",
                        items=items_list,
                        reason="Same value extracted with different requirement_type",
                    )
                )

        if duplicates:
            logger.info(f"Found {len(duplicates)} potential duplicate(s)")

        return duplicates

    # --- Completeness Calculation ---

    def _calculate_completeness(
        self, item_arrays: Dict[str, List[dict]], models: List[str]
    ) -> Dict[str, CompletenessResult]:
        """
        Calculate completeness metrics for each model.

        Compares extracted items against expected requirements from qa_qc_config.
        Expected requirements are an optional list in the run config lane, e.g.:
            lanes.quantitative.expected_requirements: [...]

        Args:
            item_arrays: Dict mapping model -> list of extracted items
            models: List of model names

        Returns:
            Dict mapping model -> CompletenessResult
        """
        expected_requirements = self.qa_qc_config.get("expected_requirements") or []
        expected_total = len(expected_requirements)

        if not expected_requirements:
            # No expected requirements configured - return basic stats
            return {
                model: CompletenessResult(
                    model=model,
                    items_extracted=len(items),
                    expected_found=0,
                    expected_total=0,
                    completeness_score=1.0,  # No requirements = 100% complete
                    missing_expected=[],
                )
                for model, items in item_arrays.items()
            }

        results = {}
        for model, items in item_arrays.items():
            # Create index of extracted items by canonical requirement_type key.
            extracted_keys = set()
            for item in items:
                key = item.get(self.match_fields[0])
                if key:
                    extracted_keys.add(key)

            # Check which expected requirements were found
            # expected_requirements is now a list of strings like ["setback__property_line_ft", ...]
            found_count = 0
            missing = []
            for expected in expected_requirements:
                if expected in extracted_keys:
                    found_count += 1
                else:
                    missing.append(expected)

            completeness_score = (
                found_count / expected_total if expected_total > 0 else 1.0
            )

            results[model] = CompletenessResult(
                model=model,
                items_extracted=len(items),
                expected_found=found_count,
                expected_total=expected_total,
                completeness_score=completeness_score,
                missing_expected=missing,
            )

            logger.debug(
                f"{model}: {found_count}/{expected_total} expected items found "
                f"({completeness_score:.1%} completeness)"
            )

        return results
