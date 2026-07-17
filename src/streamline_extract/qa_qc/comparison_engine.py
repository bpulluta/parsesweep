"""
Comparison Engine for QA/QC Multi-Model Validation.

Default behavior remains numeric-focused, but runtime QA/QC lanes can now
select alternative comparison approaches like qualitative text review.

This module is part of Phase 3-5 of the QA/QC Multi-Model Implementation Plan.
Phase 8 adds: Potential duplicate detection and completeness metrics.

Usage:
    from streamline_extract.qa_qc.comparison_engine import ComparisonEngine
    from streamline_extract.utils.schema_metadata import SchemaMetadata

    schema_metadata = SchemaMetadata(schema_path)
    engine = ComparisonEngine(schema_metadata)

    result = engine.compare_outputs(
        output_files={"gpt-4.1": path1, "gpt-4o": path2},
        document_name="austin_energy"
    )

Status: Phase 8 - Added completeness metrics and potential duplicate detection
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
from ..utils.value_normalizer import normalize_value, is_numeric_value
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

    Uses resolved QA/QC configuration:
    - runtime artifact pack lanes when available
    - schema $metadata.qa_qc as a fallback

    Why schema-driven?
    - Different domains have different field names
    - No hardcoded assumptions about field semantics
    - User can configure per schema
    """

    def __init__(
        self, schema_metadata, qa_qc_config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize with schema metadata.

        Args:
            schema_metadata: SchemaMetadata instance
        """
        self.schema_metadata = schema_metadata
        self.main_data_array = schema_metadata.get_main_data_array()
        self.identifier_fields = schema_metadata.get_identifier_fields()
        self.qa_qc_config = qa_qc_config or resolve_qaqc_runtime_config(
            schema_metadata
        )

        self.match_fields = list(self.qa_qc_config["match_fields"])
        self.compare_fields = set(self.qa_qc_config["compare_fields"])
        self.comparison_approach = self.qa_qc_config.get(
            "comparison_approach", "numeric_only"
        )
        self.config_source = self.qa_qc_config.get("source", "schema_metadata")
        self.lane_name = self.qa_qc_config.get("lane_name")
        self.lane_mode = self.qa_qc_config.get("mode", "schema_metadata")
        self.projection = self.qa_qc_config.get("projection") or None

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
        item_arrays = self._collapse_text_review_duplicates(item_arrays)
        item_arrays = self._merge_text_review_duplicate_keys(item_arrays)
        indexes = self._build_indexes(item_arrays)
        indexes = self._apply_text_review_fallback_matches(indexes, models)
        indexes = self._absorb_text_review_subsumed_items(indexes, models)
        all_keys = self._collect_all_keys(indexes)

        item_comparisons = []
        for key in sorted(all_keys, key=str):
            items = {m: indexes[m].get(key) for m in models}
            comps, skipped = self._compare_item_fields(key, items, models)
            item_comparisons.extend(comps)
            item_skipped += skipped

        # 3. Detect potential duplicates (Phase 8)
        potential_duplicates = self._detect_potential_duplicates(
            indexes, models
        )

        # 4. Calculate completeness (Phase 8)
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
        """Compare a context object across models (numeric only)."""
        comparisons = []
        skipped = 0

        # Get context from each model
        ctx_values = {m: outputs[m].get(ctx_name, {}) for m in models}

        # Collect all fields
        all_fields = set()
        for ctx in ctx_values.values():
            if isinstance(ctx, dict):
                all_fields.update(self._get_flat_fields(ctx))

        # Compare each field (numeric only)
        for field_path in sorted(all_fields):
            # Skip internal fields
            if field_path.startswith("_"):
                skipped += 1
                continue

            # Get values from each model
            model_values = {}
            for model in models:
                ctx = ctx_values[model]
                if isinstance(ctx, dict):
                    model_values[model] = get_nested_value(ctx, field_path)
                else:
                    model_values[model] = None

            # For context objects, skip all comparisons (mostly text metadata)
            # Context like jurisdiction, document_applicability are informational
            skipped += 1
            continue

        return comparisons, skipped

    def _compare_item_fields(
        self,
        item_key: Tuple,
        items: Dict[str, Optional[dict]],
        models: List[str],
    ) -> Tuple[List[FieldComparison], int]:
        """Compare fields for one item across models (numeric only)."""
        comparisons = []
        skipped = 0

        # Create readable item ID
        item_id = self._format_item_id(item_key, items)

        # Check which models have this item
        missing = [m for m in models if not items.get(m)]
        present = [m for m in models if items.get(m)]

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
                if items.get(model):
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
                pass
            else:
                # Numeric-review lanes compare configured numeric fields and keep
                # unit/source_text available for report context.
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

            comparison = self._create_comparison(
                item_id=item_id,
                field_path=field_path,
                model_values=model_values,
                models=models,
                missing_models=missing,  # Pass info about which models are missing this item
            )
            comparisons.append(comparison)

        return comparisons, skipped

    def _create_comparison(
        self,
        item_id: str,
        field_path: str,
        model_values: Dict[str, Any],
        models: List[str],
        missing_models: List[str] = None,
    ) -> FieldComparison:
        """Create a FieldComparison with agreement calculation."""
        missing_models = missing_models or []

        # Normalize values for comparison
        normalized = {m: normalize_value(v) for m, v in model_values.items()}

        # Count agreement
        agreement_count = self._count_agreement(normalized)
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

        return FieldComparison(
            item_id=item_id,
            field_path=field_path,
            model_values=model_values,
            agreement_score=agreement_score,
            needs_review=needs_review,
            notes=notes,
        )

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
            "qaqc_lane": self.lane_name,
            "qaqc_mode": self.lane_mode,
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
            # Phase 8: Potential duplicates
            "potential_duplicates_count": len(potential_duplicates),
            # Phase 8: Completeness per model
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
        """Identify narrow qualitative-only auxiliary rows that should not drive gate failure."""
        category_label, facility_label, subject_label = (
            self._extract_requirement_triplet(item_id)
        )

        if (category_label, subject_label) in {
            ("decommissioning", "financial assurance"),
            ("decommissioning", "facility removal"),
            ("decommissioning", "well plugging"),
            ("lighting requirement", "faa part 77 marking and lighting"),
            ("permit requirement", "all necessary permits"),
            ("permitted use district", "conditional use"),
        }:
            return True

        if category_label == "noise limit":
            return (
                facility_label == "power plant"
                and subject_label == "plant operations"
            )

        if category_label == "other" and subject_label in {
            "emergency response/action plan",
            "insurance",
            "radio/television interference",
            "roads and parking",
            "dust control",
            "identification/informational signage",
            "double-walled pipes across public waters",
            "pipeline siting and configuration",
            "electric transmission line siting",
        }:
            return True

        return False

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
            model: create_item_index(items, self.match_fields)
            for model, items in item_arrays.items()
        }

    def _collapse_text_review_duplicates(
        self,
        item_arrays: Dict[str, List[dict]],
    ) -> Dict[str, List[dict]]:
        """Collapse repeated qualitative rows within a single model before matching."""
        if self.comparison_approach != "text_review":
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
        if self.comparison_approach != "text_review":
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
        if self.comparison_approach != "text_review":
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

    def _absorb_text_review_subsumed_items(
        self,
        indexes: Dict[str, Dict[Tuple, dict]],
        models: List[str],
    ) -> Dict[str, Dict[Tuple, dict]]:
        """Drop one-sided qualitative rows when every opposing model has a unique broader row covering them."""
        if self.comparison_approach != "text_review":
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
        """Handle safe qualitative parent-child coverage patterns."""
        candidate_category = self._normalize_match_label(
            get_nested_value(candidate, self.match_fields[0])
            if self.match_fields
            else None
        )
        candidate_facility = self._normalize_match_label(
            get_nested_value(candidate, self.match_fields[1])
            if len(self.match_fields) > 1
            else None
        )
        candidate_subject = self._normalize_match_label(
            get_nested_value(candidate, self.match_fields[2])
            if len(self.match_fields) > 2
            else None
        )
        source_subject = self._normalize_match_label(
            get_nested_value(source_item, self.match_fields[2])
            if len(self.match_fields) > 2
            else None
        )

        procedural_prefixes = (
            "prior to",
            "upon completion",
            "when the operation",
            "drilling operations shall",
            "closure conditions",
        )
        parking_markers = {"parking", "spaces", "roads"}

        if (
            candidate_category == "decommissioning"
            and candidate_facility == "all facilities"
        ):
            if any(
                source_text.startswith(prefix)
                for prefix in procedural_prefixes
            ):
                return True

        if (
            candidate_category == "other"
            and candidate_subject == "roads and parking"
        ):
            if (
                source_subject == "parking"
                and "parking" in source_tokens
                and parking_markers.intersection(source_tokens)
            ):
                return True

        return False

    def _build_text_review_signature(
        self, item: Optional[dict]
    ) -> Optional[str]:
        """Build a normalized text signature for qualitative fallback matching."""
        if not isinstance(item, dict):
            return None

        signature_parts: List[str] = []
        for field_path in sorted(self.compare_fields):
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

    def _build_text_review_raw_text(
        self, item: Optional[dict]
    ) -> Optional[str]:
        """Build a normalized joined text string for qualitative overlap checks."""
        if not isinstance(item, dict):
            return None

        text_parts: List[str] = []
        for field_path in sorted(self.compare_fields):
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

    # --- Phase 8: Potential Duplicate Detection ---

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

    # --- Phase 8: Completeness Calculation ---

    def _calculate_completeness(
        self, item_arrays: Dict[str, List[dict]], models: List[str]
    ) -> Dict[str, CompletenessResult]:
        """
        Calculate completeness metrics for each model.

        Compares extracted items against expected requirements from schema.

        Args:
            item_arrays: Dict mapping model -> list of extracted items
            models: List of model names

        Returns:
            Dict mapping model -> CompletenessResult
        """
        expected_requirements = (
            self.schema_metadata.get_expected_requirements()
        )
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
