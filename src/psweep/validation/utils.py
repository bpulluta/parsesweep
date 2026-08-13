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


def _parse_report_config(report_raw: Any) -> Dict[str, Any]:
    """Parse and validate optional validation.report settings."""
    if report_raw is None:
        report_raw = {}
    if not isinstance(report_raw, dict):
        raise ValueError("validation.report must be a mapping/object when provided.")

    allowed_keys = {
        "include_csv",
        "include_missing_in_queue",
        "include_low_signal_presence_in_queue",
        "evidence_detail",
        "evidence_max_chars",
        "identity_columns",
        "include_value_unit_column",
    }
    unknown = set(report_raw.keys()) - allowed_keys
    if unknown:
        raise ValueError(
            "Unknown validation.report key(s): "
            + ", ".join(sorted(unknown))
            + ". Allowed keys: "
            + ", ".join(sorted(allowed_keys))
        )

    def _require_bool(key: str, default: bool) -> bool:
        value = report_raw.get(key, default)
        if isinstance(value, bool):
            return value
        raise ValueError(f"validation.report.{key} must be true or false.")

    evidence_detail = str(report_raw.get("evidence_detail", "full")).strip().lower()
    if evidence_detail not in {"full", "compact", "off"}:
        raise ValueError(
            "validation.report.evidence_detail must be one of: full, compact, off."
        )

    evidence_max_chars = report_raw.get("evidence_max_chars", 200)
    if (
        isinstance(evidence_max_chars, bool)
        or not isinstance(evidence_max_chars, int)
        or not (50 <= evidence_max_chars <= 2000)
    ):
        raise ValueError(
            "validation.report.evidence_max_chars must be an integer between 50 and 2000."
        )

    identity_columns = str(report_raw.get("identity_columns", "compact")).strip().lower()
    if identity_columns not in {"compact", "expanded"}:
        raise ValueError(
            "validation.report.identity_columns must be one of: compact, expanded."
        )

    return {
        "include_csv": _require_bool("include_csv", False),
        "include_missing_in_queue": _require_bool("include_missing_in_queue", True),
        "include_low_signal_presence_in_queue": _require_bool(
            "include_low_signal_presence_in_queue", False
        ),
        "evidence_detail": evidence_detail,
        "evidence_max_chars": int(evidence_max_chars),
        "identity_columns": identity_columns,
        "include_value_unit_column": _require_bool(
            "include_value_unit_column", True
        ),
    }


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
            "validation.record_matching.anchor must be a mapping. Example:\n"
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
            "validation.record_matching.anchor is not supported with "
            "comparison_approach: numeric_only."
        )
    if model_count != 2:
        raise ValueError(
            "validation.record_matching.anchor currently supports exactly 2 models. "
            f"Configured models={model_count}."
        )
    fields = anchor_raw.get("fields")
    if not fields or not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise ValueError(
            "validation.record_matching.anchor.fields must be a non-empty list of field name strings, "
            "e.g. [source_verbatim]."
        )
    method = str(anchor_raw.get("method", "char_ngram")).strip()
    if method != "char_ngram":
        raise ValueError(
            f"validation.record_matching.anchor.method must be 'char_ngram' (got '{method}')."
        )
    ngram = anchor_raw.get("ngram", 4)
    if isinstance(ngram, bool) or not isinstance(ngram, int) or not (2 <= ngram <= 8):
        raise ValueError(
            "validation.record_matching.anchor.ngram must be an integer between 2 and 8."
        )
    auto_threshold = float(anchor_raw.get("auto_threshold", 0.65) or 0.65)
    review_threshold = float(anchor_raw.get("review_threshold", 0.35) or 0.35)
    if not (0.0 < review_threshold < auto_threshold <= 1.0):
        raise ValueError(
            "validation.record_matching.anchor thresholds must satisfy "
            "0 < review_threshold < auto_threshold <= 1.0. "
            f"Got review_threshold={review_threshold}, auto_threshold={auto_threshold}."
        )
    max_candidates = anchor_raw.get("max_candidates_per_row", 3)
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 1:
        raise ValueError(
            "validation.record_matching.anchor.max_candidates_per_row must be a positive integer."
        )
    min_chars = anchor_raw.get("min_anchor_chars", 12)
    if isinstance(min_chars, bool) or not isinstance(min_chars, int) or min_chars < 0:
        raise ValueError(
            "validation.record_matching.anchor.min_anchor_chars must be a non-negative integer."
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


def resolve_validation_runtime_config(
    schema_metadata,
    runtime_artifact: Optional[Dict[str, Any]] = None,
    runtime_validation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve active QA/QC config from the run config.

    QA/QC configuration always lives in config/<domain>/run.yaml under
    ``validation``. Schema metadata is intentionally excluded from this path —
    it owns extraction contracts, not runtime QA/QC behavior.

    Raises
    ------
    ValueError
        If no QA/QC config can be found, if deprecated lane-style
        keys are present, or if required fields are missing.
    """
    pack_validation = (
        ((runtime_artifact or {}).get("resolved") or {})
        .get("pack", {})
        .get("validation")
    )
    if not isinstance(pack_validation, dict) and isinstance(runtime_validation, dict):
        pack_validation = runtime_validation

    if not isinstance(pack_validation, dict):
        raise ValueError(
            "No QA/QC configuration found. "
            "Add a validation: block to your run config YAML:\n"
            "  validation:\n"
            "    models: [primary, secondary]\n"
            "    comparison_approach: mixed\n"
            "    record_matching:\n"
            "      key_fields: [feature, applies_to, specific_subject]\n"
            "    comparison:\n"
            "      primary_fields: [value, units, value_interpretation]\n"
            "Then run: pixi run psweep validate --config config/<domain>/run.yaml"
        )

    if "lanes" in pack_validation or "default_lane" in pack_validation:
        raise ValueError(
            "Deprecated QA/QC lane-style config detected (default_lane/lanes). "
            "Migrate to the simplified validation shape:\n"
            "  validation:\n"
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

    record_matching = pack_validation.get("record_matching") or {}
    comparison = pack_validation.get("comparison") or {}
    judge = pack_validation.get("judge") or {}

    max_calls = judge.get("max_calls_per_document")
    if max_calls is not None:
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls <= 0:
            raise ValueError(
                "validation.judge.max_calls_per_document must be a positive integer."
            )

    match_fields = record_matching.get("key_fields")
    if not match_fields:
        raise ValueError(
            "QA/QC config is missing required record_matching.key_fields. "
            "Add it to your run config:\n"
            "  validation:\n"
            "    record_matching:\n"
            "      key_fields: [feature, applies_to, specific_subject]"
        )

    compare_fields = comparison.get("primary_fields") or ["value"]
    collapse_percent_context = comparison.get("collapse_percent_context", False)
    if not isinstance(collapse_percent_context, bool):
        raise ValueError(
            "validation.comparison.collapse_percent_context must be true or false."
        )
    unit_equivalence_groups = comparison.get("unit_equivalence_groups") or []
    if not isinstance(unit_equivalence_groups, list):
        raise ValueError(
            "validation.comparison.unit_equivalence_groups must be a list of synonym lists, "
            "for example: [[\"feet\", \"ft\"], [\"hours\", \"hrs\", \"hr\"]]."
        )
    comparison_approach = str(
        pack_validation.get("comparison_approach", "mixed")
    ).strip().lower()
    if comparison_approach not in {"mixed", "numeric_only", "text_review"}:
        raise ValueError(
            "Unsupported validation.comparison_approach. "
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

    models = pack_validation.get("models") or []
    model_count = len(models) if isinstance(models, list) else 0
    anchor_config = _parse_anchor_config(
        record_matching.get("anchor"), comparison_approach, model_count
    )
    raw_scope_variant_keys = record_matching.get("scope_variant_keys") or []
    if not isinstance(raw_scope_variant_keys, list):
        raise ValueError(
            "validation.record_matching.scope_variant_keys must be a list of "
            "[category, subject] pairs."
        )
    scope_variant_keys: list[list[str]] = []
    for pair in raw_scope_variant_keys:
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) < 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], str)
            or not pair[0].strip()
            or not pair[1].strip()
        ):
             raise ValueError(
                "Each validation.record_matching.scope_variant_keys entry must be "
                "[category, subject] with two non-empty strings."
            )
        scope_variant_keys.append([pair[0].strip(), pair[1].strip()])
    report = _parse_report_config(pack_validation.get("report"))

    return {
        "comparison_approach": comparison_approach,
        "match_fields": list(match_fields),
        "compare_fields": list(compare_fields),
        "collapse_percent_context": collapse_percent_context,
        "unit_equivalence_groups": list(unit_equivalence_groups),
        "projection": pack_validation.get("projection"),
        "enable_text_fallback_matching": enable_text_fallback_matching,
        "enable_judge_pair_matching": enable_judge_pair_matching,
        "text_fallback_fields": text_fallback_fields,
        "semantic_match_threshold": float(
            record_matching.get("semantic_match_threshold", 0.38) or 0.38
        ),
        "fuzzy_match_fields": list(
            record_matching.get("fuzzy_key_fields", []) or []
        ),
        "scope_variant_keys": scope_variant_keys,
        "judge": judge,
        "report": report,
        "anchor_config": anchor_config,
    }


def sanitize_model_name(model_name: str) -> str:
    """
    Sanitize model name for use as filename.

    Parameters
    ----------
    model_name : str
        Model name (e.g., "gpt-4o", "claude-3.5-sonnet")

    Returns
    -------
    str
        Sanitized name safe for filenames
    """
    # Replace characters that might cause issues in filenames
    sanitized = model_name.replace("/", "-").replace("\\", "-")
    sanitized = sanitized.replace(":", "-").replace("*", "-")
    sanitized = sanitized.replace("?", "-").replace('"', "-")
    sanitized = sanitized.replace("<", "-").replace(">", "-")
    sanitized = sanitized.replace("|", "-")
    return sanitized
