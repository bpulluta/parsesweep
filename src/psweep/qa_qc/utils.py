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
    preferred_lane: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve active QA/QC config from the runtime artifact, falling back to schema metadata."""
    pack_qaqc = (
        ((runtime_artifact or {}).get("resolved") or {})
        .get("pack", {})
        .get("qaqc")
    )

    if isinstance(pack_qaqc, dict):
        lanes = pack_qaqc.get("lanes") or {}
        lane_name = pack_qaqc.get("default_lane")
        lane_config = None

        if preferred_lane:
            candidate = lanes.get(preferred_lane)
            if not isinstance(candidate, dict):
                available_lanes = ", ".join(sorted(lanes)) or "none"
                raise ValueError(
                    f"Requested QA/QC lane '{preferred_lane}' is not defined in the runtime pack; available lanes: {available_lanes}"
                )
            lane_name = preferred_lane
            lane_config = candidate

        if (
            lane_config is None
            and lane_name
            and isinstance(lanes.get(lane_name), dict)
        ):
            candidate = lanes[lane_name]
            if candidate.get("enabled", True):
                lane_config = candidate

        if lane_config is None and not preferred_lane:
            for candidate_name, candidate in lanes.items():
                if isinstance(candidate, dict) and candidate.get(
                    "enabled", False
                ):
                    lane_name = candidate_name
                    lane_config = candidate
                    break

        if lane_config is not None:
            match_fields = lane_config.get("record_matching", {}).get(
                "key_fields"
            )
            compare_fields = lane_config.get("comparison", {}).get(
                "primary_fields"
            )

            return {
                "source": "runtime_artifact",
                "lane_name": lane_name,
                "mode": lane_config.get("mode", lane_name or "runtime"),
                "comparison_approach": lane_config.get(
                    "comparison_approach", "numeric_only"
                ),
                "match_fields": list(
                    match_fields or schema_metadata.get_qa_qc_match_fields()
                ),
                "compare_fields": list(
                    compare_fields
                    or schema_metadata.get_qa_qc_compare_fields()
                ),
                "projection": lane_config.get("projection"),
            }

    return {
        "source": "schema_metadata",
        "lane_name": None,
        "mode": "schema_metadata",
        "comparison_approach": "numeric_only",
        "match_fields": list(schema_metadata.get_qa_qc_match_fields()),
        "compare_fields": list(schema_metadata.get_qa_qc_compare_fields()),
        "projection": None,
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
