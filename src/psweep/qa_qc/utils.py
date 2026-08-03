"""
Utility functions for QA/QC Multi-Model Validation.

Common utilities used across QA/QC modules.

Note: Most utilities are in the shared utils/ directory:
    - utils/item_matcher.py - Item matching logic
    - utils/value_normalizer.py - Value normalization
    - utils/schema_metadata.py - Schema metadata parsing

This file contains QA/QC-specific utilities.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def resolve_qaqc_runtime_config(
    schema_metadata,
    runtime_artifact: Optional[Dict[str, Any]] = None,
    runtime_qaqc: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve active QA/QC config from the run config.

    QA/QC configuration always lives in config/<domain>/run.yaml under
    ``qaqc``. Schema metadata is intentionally excluded from this path —
    it owns extraction contracts, not runtime QA/QC behavior.

    Raises:
        ValueError: If no QA/QC config can be found, if deprecated lane-style
            keys are present, or if required fields are missing.
    """
    pack_qaqc = (
        ((runtime_artifact or {}).get("resolved") or {})
        .get("pack", {})
        .get("qaqc")
    )
    if not isinstance(pack_qaqc, dict) and isinstance(runtime_qaqc, dict):
        pack_qaqc = runtime_qaqc

    if not isinstance(pack_qaqc, dict):
        raise ValueError(
            "No QA/QC configuration found. "
            "Add a qaqc: block to your run config YAML:\n"
            "  qaqc:\n"
            "    models: [primary, secondary]\n"
            "    comparison_approach: mixed\n"
            "    record_matching:\n"
            "      key_fields: [feature, applies_to, specific_subject]\n"
            "    comparison:\n"
            "      primary_fields: [value, units, value_interpretation]\n"
            "Then run: pixi run psweep validate --config config/<domain>/run.yaml"
        )

    if "lanes" in pack_qaqc or "default_lane" in pack_qaqc:
        raise ValueError(
            "Deprecated QA/QC lane-style config detected (default_lane/lanes). "
            "Migrate to the simplified qaqc shape:\n"
            "  qaqc:\n"
            "    models: [primary, secondary]\n"
            "    comparison_approach: mixed\n"
            "    record_matching:\n"
            "      key_fields: [feature, applies_to, specific_subject]\n"
            "    comparison:\n"
            "      primary_fields: [value, units, value_interpretation, obligation]\n"
            "    judge:\n"
            "      enabled: true\n"
            "      model: judge"
        )

    record_matching = pack_qaqc.get("record_matching") or {}
    comparison = pack_qaqc.get("comparison") or {}
    judge = pack_qaqc.get("judge") or {}

    match_fields = record_matching.get("key_fields")
    if not match_fields:
        raise ValueError(
            "QA/QC config is missing required record_matching.key_fields. "
            "Add it to your run config:\n"
            "  qaqc:\n"
            "    record_matching:\n"
            "      key_fields: [feature, applies_to, specific_subject]"
        )

    compare_fields = comparison.get("primary_fields") or ["value"]
    unit_equivalence_groups = comparison.get("unit_equivalence_groups") or []
    if not isinstance(unit_equivalence_groups, list):
        raise ValueError(
            "qaqc.comparison.unit_equivalence_groups must be a list of synonym lists, "
            "for example: [[\"feet\", \"ft\"], [\"hours\", \"hrs\", \"hr\"]]."
        )
    comparison_approach = str(
        pack_qaqc.get("comparison_approach", "mixed")
    ).strip().lower()
    if comparison_approach not in {"mixed", "numeric_only", "text_review"}:
        raise ValueError(
            "Unsupported qaqc.comparison_approach. "
            "Use one of: mixed, numeric_only, text_review."
        )

    enable_text_fallback_matching = comparison_approach == "mixed"
    if "text_fallback_matching" in record_matching:
        raise ValueError(
            "record_matching.text_fallback_matching is deprecated. "
            "Fallback matching is automatic in mixed comparison_approach."
        )

    enable_judge_pair_matching = bool(judge.get("enabled")) and bool(
        judge.get("pair_unmatched", True)
    )
    if "llm_pair_matching" in record_matching:
        raise ValueError(
            "record_matching.llm_pair_matching is deprecated. "
            "Use judge.pair_unmatched (default true) to control judge pairing."
        )

    text_fallback_fields = list(
        record_matching.get("text_fallback_fields")
        or [
            "feature",
            "applies_to",
            "specific_subject",
            "requirement_description",
            "condition",
            "source_verbatim",
            "value",
            "units",
        ]
    )

    return {
        "source": "runtime_artifact",
        "lane_name": "main",
        "comparison_approach": comparison_approach,
        "match_fields": list(match_fields),
        "compare_fields": list(compare_fields),
        "unit_equivalence_groups": list(unit_equivalence_groups),
        "projection": pack_qaqc.get("projection"),
        "enable_text_fallback_matching": enable_text_fallback_matching,
        "enable_judge_pair_matching": enable_judge_pair_matching,
        "text_fallback_fields": text_fallback_fields,
        "semantic_match_threshold": float(
            record_matching.get("semantic_match_threshold", 0.38) or 0.38
        ),
        "fuzzy_match_fields": list(
            record_matching.get("fuzzy_key_fields", []) or []
        ),
        "scope_variant_keys": [],
        "judge": judge,
        "report": pack_qaqc.get("report") or {},
    }


def sanitize_model_name(model_name: str) -> str:
    """
    Sanitize model name for use as filename.

    Args:
        model_name: Model name (e.g., "gpt-4o", "claude-3.5-sonnet")

    Returns:
        Sanitized name safe for filenames
    """
    # Replace characters that might cause issues in filenames
    sanitized = model_name.replace("/", "-").replace("\\", "-")
    sanitized = sanitized.replace(":", "-").replace("*", "-")
    sanitized = sanitized.replace("?", "-").replace('"', "-")
    sanitized = sanitized.replace("<", "-").replace(">", "-")
    sanitized = sanitized.replace("|", "-")
    return sanitized
