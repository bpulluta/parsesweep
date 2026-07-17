"""
Utility functions for QA/QC Multi-Model Validation.

Common utilities used across QA/QC modules.

Note: Most utilities are in the shared utils/ directory:
    - utils/item_matcher.py - Item matching logic
    - utils/value_normalizer.py - Value normalization
    - utils/schema_metadata.py - Schema metadata parsing

This file contains QA/QC-specific utilities.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def find_companion_qaqc_schema(
    production_schema_path: Path,
) -> Tuple[Optional[Path], Optional[dict]]:
    """
    Find the companion QA/QC schema for a production schema.

    Looks for a simplified QA/QC schema in schemas/qaqc/ that corresponds
    to the production schema. The QA/QC schema is designed for focused
    extraction with fewer fields and simpler instructions.

    Naming convention:
        schemas/personal/foo_bar_schema.json -> schemas/qaqc/foo_bar_qaqc.json
        schemas/personal/electricity_tariff_schema.json -> schemas/qaqc/electricity_tariff_qaqc.json

    Also checks $metadata.companion_to in qaqc schemas to match.

    Args:
        production_schema_path: Path to the production schema file

    Returns:
        Tuple of (qaqc_schema_path, loaded_schema) or (None, None) if not found
    """
    production_name = production_schema_path.name
    production_stem = (
        production_schema_path.stem
    )  # e.g., "geothermal_ordinance_schema"

    # Look for QA/QC schema directory relative to schema location
    # schemas/personal/foo.json -> schemas/qaqc/
    qaqc_dir = production_schema_path.parent.parent / "qaqc"

    if not qaqc_dir.exists():
        logger.debug(f"QA/QC schema directory not found: {qaqc_dir}")
        return None, None

    # Strategy 1: Derive name from production schema
    # foo_bar_schema.json -> foo_bar_qaqc.json
    if production_stem.endswith("_schema"):
        base_name = production_stem.replace("_schema", "")
        derived_qaqc_name = f"{base_name}_qaqc.json"
        derived_path = qaqc_dir / derived_qaqc_name

        if derived_path.exists():
            try:
                with open(derived_path) as f:
                    qaqc_schema = json.load(f)
                logger.info(
                    f"Found companion QA/QC schema (by naming convention): {derived_path}"
                )
                return derived_path, qaqc_schema
            except Exception as e:
                logger.warning(
                    f"Error loading QA/QC schema {derived_path}: {e}"
                )

    # Strategy 2: Search all qaqc schemas for matching companion_to
    for qaqc_file in qaqc_dir.glob("*_qaqc.json"):
        try:
            with open(qaqc_file) as f:
                qaqc_schema = json.load(f)

            companion_to = qaqc_schema.get("$metadata", {}).get(
                "companion_to", ""
            )
            # Check if companion_to matches our production schema
            if companion_to and (
                companion_to == production_name
                or companion_to.endswith(f"/{production_name}")
                or production_name in companion_to
            ):
                logger.info(
                    f"Found companion QA/QC schema (by companion_to): {qaqc_file}"
                )
                return qaqc_file, qaqc_schema
        except Exception as e:
            logger.debug(f"Could not read {qaqc_file}: {e}")
            continue

    logger.debug(f"No companion QA/QC schema found for {production_name}")
    return None, None


def get_qa_qc_output_dir(base_dir: Path, document_name: str) -> Path:
    """
    Get the QA/QC output directory for a document.

    Args:
        base_dir: Base output directory (e.g., "processed/")
        document_name: Name of the document (without extension)

    Returns:
        Path to QA/QC output directory (e.g., "processed/qa_qc/austin_energy/")
    """
    return Path(base_dir) / "qa_qc" / document_name


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


def format_agreement_score(agreeing_models: int, total_models: int) -> str:
    """
    Format agreement score for display.

    Args:
        agreeing_models: Number of models that agree
        total_models: Total number of models

    Returns:
        Formatted string like "3/3" or "2/3"
    """
    return f"{agreeing_models}/{total_models}"


def calculate_agreement_percentage(
    field_comparisons: List[Dict],
    total_models: int,
) -> float:
    """
    Calculate the percentage of fields with full agreement.

    Args:
        field_comparisons: List of field comparison results
        total_models: Total number of models

    Returns:
        Percentage (0-100) of fields with full agreement
    """
    if not field_comparisons:
        return 0.0

    full_agreement_count = sum(
        1
        for fc in field_comparisons
        if fc.get("agreement_score") == f"{total_models}/{total_models}"
    )

    return (full_agreement_count / len(field_comparisons)) * 100


def get_needs_review_count(field_comparisons: List[Dict]) -> int:
    """
    Count the number of fields that need human review.

    Args:
        field_comparisons: List of field comparison results

    Returns:
        Count of fields needing review
    """
    return sum(1 for fc in field_comparisons if fc.get("needs_review", False))
