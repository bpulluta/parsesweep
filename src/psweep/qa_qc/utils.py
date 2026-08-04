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


def _parse_anchor_config(
    anchor_raw: Any, comparison_approach: str, model_count: int
) -> Optional[Dict[str, Any]]:
    """Parse and validate the optional record_matching.anchor block.

    Returns None if the block is absent (legacy path preserved unchanged).
    Raises ValueError with a copy-pasteable fix for any invalid value.
    """
    if anchor_raw is None:
        return None
    if not isinstance(anchor_raw, dict):
        raise ValueError(
            "qaqc.record_matching.anchor must be a mapping. Example:\n"
            "  record_matching:\n"
            "    anchor:\n"
            "      fields: [source_verbatim]\n"
            "      method: char_ngram\n"
            "      auto_threshold: 0.65\n"
            "      review_threshold: 0.35"
        )
    # Only supported with exactly 2-model, non-numeric approaches.
    if comparison_approach == "numeric_only":
        raise ValueError(
            "qaqc.record_matching.anchor is not supported with "
            "comparison_approach: numeric_only."
        )
    if model_count != 2:
        raise ValueError(
            "qaqc.record_matching.anchor currently supports exactly 2 models. "
            f"Configured models={model_count}."
        )
    fields = anchor_raw.get("fields")
    if not fields or not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise ValueError(
            "qaqc.record_matching.anchor.fields must be a non-empty list of field name strings, "
            "e.g. [source_verbatim]."
        )
    method = str(anchor_raw.get("method", "char_ngram")).strip()
    if method != "char_ngram":
        raise ValueError(
            f"qaqc.record_matching.anchor.method must be 'char_ngram' (got '{method}')."
        )
    ngram = anchor_raw.get("ngram", 4)
    if isinstance(ngram, bool) or not isinstance(ngram, int) or not (2 <= ngram <= 8):
        raise ValueError(
            "qaqc.record_matching.anchor.ngram must be an integer between 2 and 8."
        )
    auto_threshold = float(anchor_raw.get("auto_threshold", 0.65) or 0.65)
    review_threshold = float(anchor_raw.get("review_threshold", 0.35) or 0.35)
    if not (0.0 < review_threshold < auto_threshold <= 1.0):
        raise ValueError(
            "qaqc.record_matching.anchor thresholds must satisfy "
            "0 < review_threshold < auto_threshold <= 1.0. "
            f"Got review_threshold={review_threshold}, auto_threshold={auto_threshold}."
        )
    max_candidates = anchor_raw.get("max_candidates_per_row", 3)
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 1:
        raise ValueError(
            "qaqc.record_matching.anchor.max_candidates_per_row must be a positive integer."
        )
    min_chars = anchor_raw.get("min_anchor_chars", 12)
    if isinstance(min_chars, bool) or not isinstance(min_chars, int) or min_chars < 0:
        raise ValueError(
            "qaqc.record_matching.anchor.min_anchor_chars must be a non-negative integer."
        )
    return {
        "fields": list(fields),
        "method": method,
        "ngram": int(ngram),
        "auto_threshold": auto_threshold,
        "review_threshold": review_threshold,
        "max_candidates_per_row": int(max_candidates),
        "min_anchor_chars": int(min_chars),
    }


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

    max_calls = judge.get("max_calls_per_document")
    if max_calls is not None:
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls <= 0:
            raise ValueError(
                "qaqc.judge.max_calls_per_document must be a positive integer."
            )

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

    models = pack_qaqc.get("models") or []
    model_count = len(models) if isinstance(models, list) else 0
    anchor_config = _parse_anchor_config(
        record_matching.get("anchor"), comparison_approach, model_count
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
        "anchor_config": anchor_config,
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
