"""
Comparison Engine for QA/QC Multi-Model Validation.

SIMPLIFIED VERSION: Compares ONLY numeric values across models.
Text comparisons are skipped as they produce unreliable results
due to verbosity differences between models.

Key Principle: "Compare what's easy - numbers. Skip what's hard - text."

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
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Import shared utilities
from ..utils.item_matcher import create_item_index, get_nested_value
from ..utils.value_normalizer import normalize_value, is_numeric_value

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
    potential_duplicates: List[PotentialDuplicate] = field(default_factory=list)
    completeness: Dict[str, CompletenessResult] = field(default_factory=dict)
    
    @property
    def field_comparisons(self) -> List[FieldComparison]:
        """All field comparisons (context + items combined)."""
        return self.context_comparisons + self.item_comparisons


class ComparisonEngine:
    """
    Compare outputs from multiple models - SCHEMA-DRIVEN.
    
    Uses schema $metadata.qa_qc configuration:
    - record_matching.key_fields: Fields to use for matching items
    - comparison.primary_fields: Fields to compare for agreement
    
    Why schema-driven?
    - Different domains have different field names
    - No hardcoded assumptions about field semantics
    - User can configure per schema
    """

    def __init__(self, schema_metadata):
        """
        Initialize with schema metadata.
        
        Args:
            schema_metadata: SchemaMetadata instance
        """
        self.schema_metadata = schema_metadata
        self.main_data_array = schema_metadata.get_main_data_array()
        self.identifier_fields = schema_metadata.get_identifier_fields()
        
        # Get match and compare fields from schema qa_qc config
        self.match_fields = schema_metadata.get_qa_qc_match_fields()
        self.compare_fields = set(schema_metadata.get_qa_qc_compare_fields())
        
        logger.debug(f"ComparisonEngine: main_data_array={self.main_data_array}")
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
            ComparisonResult with summary and numeric field comparisons
        """
        # Load outputs
        outputs = self._load_outputs(output_files)
        
        if len(outputs) < 2:
            logger.warning(f"Only {len(outputs)} valid outputs, need 2+ for comparison")
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
            comps, skipped = self._compare_context_object(ctx_name, outputs, models)
            context_comparisons.extend(comps)
            context_skipped += skipped
        
        # 2. Compare main data array items
        item_arrays = self._extract_item_arrays(outputs)
        indexes = self._build_indexes(item_arrays)
        all_keys = self._collect_all_keys(indexes)
        
        item_comparisons = []
        for key in sorted(all_keys, key=str):
            items = {m: indexes[m].get(key) for m in models}
            comps, skipped = self._compare_item_fields(key, items, models)
            item_comparisons.extend(comps)
            item_skipped += skipped
        
        # 3. Detect potential duplicates (Phase 8)
        potential_duplicates = self._detect_potential_duplicates(indexes, models)
        
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
            completeness
        )
        
        return ComparisonResult(
            document_name=document_name,
            models=models,
            summary=summary,
            context_comparisons=context_comparisons,
            item_comparisons=item_comparisons,
            potential_duplicates=potential_duplicates,
            completeness=completeness
        )

    def _compare_context_object(
        self,
        ctx_name: str,
        outputs: Dict[str, dict],
        models: List[str]
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
            if field_path.startswith('_'):
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
        models: List[str]
    ) -> Tuple[List[FieldComparison], int]:
        """Compare fields for one item across models (numeric only)."""
        comparisons = []
        skipped = 0
        
        # Create readable item ID
        item_id = " | ".join(str(v) if v else "N/A" for v in item_key)
        
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
        
        # Compare each field (NUMERIC values only)
        for field_path in sorted(all_fields):
            # Skip internal fields
            if field_path.startswith('_'):
                skipped += 1
                continue
            
            # Get field name (last part of path)
            field_name = field_path.split('.')[-1].lower()
            
            # Only compare fields specified in schema qa_qc.comparison.primary_fields
            if field_name not in self.compare_fields:
                skipped += 1
                continue
            
            # Get values - show actual value for present items, None for missing
            model_values = {}
            for model in models:
                if items.get(model):
                    model_values[model] = get_nested_value(items[model], field_path)
                else:
                    model_values[model] = None  # Model doesn't have this item
            
            # CRITICAL: For missing items, always include comparison to show what was extracted
            # For items present in all models, only compare if at least one value is numeric
            # EXCEPTION: Always include 'unit' and 'source_text' fields for display purposes
            if missing:
                # Item missing from some models - include comparison regardless of value type
                # This lets user see what was extracted vs not extracted
                pass
            elif field_name in ("unit", "source_text"):
                # Always include unit and source_text fields for display (not for agreement)
                pass
            else:
                # Item exists in all models - only compare if at least one value is numeric
                # Text values like "Required", "Exempt", etc. are skipped for agreement analysis
                has_numeric = any(
                    is_numeric_value(v) for v in model_values.values() if v is not None
                )
                if not has_numeric:
                    skipped += 1
                    continue
            
            comparison = self._create_comparison(
                item_id=item_id,
                field_path=field_path,
                model_values=model_values,
                models=models,
                missing_models=missing  # Pass info about which models are missing this item
            )
            comparisons.append(comparison)
        
        return comparisons, skipped

    def _create_comparison(
        self,
        item_id: str,
        field_path: str,
        model_values: Dict[str, Any],
        models: List[str],
        missing_models: List[str] = None
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
                unique = set(str(v) for v in normalized.values() if v is not None)
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
            notes=notes
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
        completeness: Dict[str, 'CompletenessResult'] = None
    ) -> Dict[str, Any]:
        """Calculate summary statistics."""
        potential_duplicates = potential_duplicates or []
        completeness = completeness or {}
        
        # Count agreements
        context_agree = sum(
            1 for c in context_comparisons
            if c.agreement_score == f"{num_models}/{num_models}"
        )
        item_agree = sum(
            1 for c in item_comparisons
            if c.agreement_score == f"{num_models}/{num_models}"
        )
        
        total = len(context_comparisons) + len(item_comparisons)
        full_agree = context_agree + item_agree
        needs_review = total - full_agree
        
        summary = {
            # Comparison approach
            "comparison_approach": "numeric_only",
            "skipped_non_numeric": context_skipped + item_skipped,
            
            # Item counts per model
            "items_per_model": {m: len(items) for m, items in item_arrays.items()},
            
            # Overall stats
            "total_comparisons": total,
            "full_agreement_count": full_agree,
            "full_agreement_pct": round(full_agree / total * 100, 1) if total else 0.0,
            "needs_review_count": needs_review,
            "needs_review_pct": round(needs_review / total * 100, 1) if total else 0.0,
            
            # Context breakdown
            "context_comparisons": len(context_comparisons),
            "context_agreement_pct": round(
                context_agree / len(context_comparisons) * 100, 1
            ) if context_comparisons else 0.0,
            
            # Item breakdown
            "item_comparisons": len(item_comparisons),
            "item_agreement_pct": round(
                item_agree / len(item_comparisons) * 100, 1
            ) if item_comparisons else 0.0,
            
            # Phase 8: Potential duplicates
            "potential_duplicates_count": len(potential_duplicates),
            
            # Phase 8: Completeness per model
            "completeness_per_model": {
                model: {
                    "items_extracted": result.items_extracted,
                    "expected_found": result.expected_found,
                    "expected_total": result.expected_total,
                    "completeness_score": round(result.completeness_score * 100, 1)
                }
                for model, result in completeness.items()
            } if completeness else {}
        }
        
        return summary

    # --- Helper Methods ---

    def _load_outputs(self, output_files: Dict[str, Path]) -> Dict[str, dict]:
        """Load output JSON files."""
        outputs = {}
        for model, path in output_files.items():
            if path and Path(path).exists():
                try:
                    with open(path) as f:
                        loaded = json.load(f)
                        if isinstance(loaded, dict) and isinstance(loaded.get("payload"), dict):
                            outputs[model] = loaded["payload"]
                        else:
                            outputs[model] = loaded
                except Exception as e:
                    logger.error(f"Failed to load {model}: {e}")
        return outputs

    def _extract_item_arrays(self, outputs: Dict[str, dict]) -> Dict[str, List[dict]]:
        """Extract main data arrays."""
        arrays = {}
        for model, output in outputs.items():
            items = output.get(self.main_data_array, [])
            arrays[model] = items if isinstance(items, list) else []
        return arrays

    def _build_indexes(self, item_arrays: Dict[str, List[dict]]) -> Dict[str, Dict[Tuple, dict]]:
        """Build item indexes for matching."""
        return {
            model: create_item_index(items, self.match_fields)
            for model, items in item_arrays.items()
        }

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
        self,
        indexes: Dict[str, Dict[Tuple, dict]],
        models: List[str]
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
        value_groups: Dict[Tuple, List[Tuple[str, Tuple, dict]]] = defaultdict(list)
        for model, items in only_items.items():
            for key, item in items:
                value = item.get('value')
                unit = item.get('unit', '')
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
                duplicates.append(PotentialDuplicate(
                    value=value,
                    unit=unit or "N/A",
                    items=items_list,
                    reason="Same value extracted with different requirement_type"
                ))
        
        if duplicates:
            logger.info(f"Found {len(duplicates)} potential duplicate(s)")
        
        return duplicates

    # --- Phase 8: Completeness Calculation ---

    def _calculate_completeness(
        self,
        item_arrays: Dict[str, List[dict]],
        models: List[str]
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
        expected_requirements = self.schema_metadata.get_expected_requirements()
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
                    missing_expected=[]
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
            
            completeness_score = found_count / expected_total if expected_total > 0 else 1.0
            
            results[model] = CompletenessResult(
                model=model,
                items_extracted=len(items),
                expected_found=found_count,
                expected_total=expected_total,
                completeness_score=completeness_score,
                missing_expected=missing
            )
            
            logger.debug(
                f"{model}: {found_count}/{expected_total} expected items found "
                f"({completeness_score:.1%} completeness)"
            )
        
        return results
