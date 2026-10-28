"""Text processing utilities for extraction."""

import logging

from jsonschema import FormatChecker
from jsonschema.validators import validator_for

from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


def find_data_arrays(data: Dict[str, Any]) -> List[Tuple[str, list]]:
    """Return ``(field_name, list_value)`` for every non-empty list-valued
    top-level field, in document (insertion) order.

    This is the single source of truth for "which top-level fields hold the
    extracted item arrays", used by item counting, completeness scoring, and
    record writing so the "find the data arrays" scan is not reimplemented.
    """
    return [
        (key, value)
        for key, value in data.items()
        if isinstance(value, list) and value
    ]


def find_main_data_array(data: Dict[str, Any]) -> Tuple[Optional[str], int]:
    """Return ``(field_name, item_count)`` for the largest non-empty list-valued
    top-level field, or ``(None, 0)`` when there are none.

    On ties the first such field in document order wins.
    """
    arrays = find_data_arrays(data)
    if not arrays:
        return None, 0
    key, value = max(arrays, key=lambda kv: len(kv[1]))
    return key, len(value)


class TextProcessor:
    """Handles text optimization and preparation for extraction."""

    def __init__(self, max_chars: int = 400000):
        """
        Initialize text processor.

        Parameters
        ----------
        max_chars : int
            Maximum characters to extract from document (default: 400000).
            Proven reliable for fast processing. Increase for very long documents.
        """
        self.max_chars = max_chars

    def optimize(self, text: str) -> Tuple[str, bool]:
        """
        Optimize text for extraction by truncating if needed.

        Parameters
        ----------
        text : str
            Full document text

        Returns
        -------
        Tuple[str, bool]
            Tuple of (optimized_text, was_truncated)
        """
        original_length = len(text)
        was_truncated = original_length > self.max_chars

        if was_truncated:
            text = text[: self.max_chars]
            logger.info(
                f"Document truncated: {original_length:,} chars -> {self.max_chars:,} chars. "
                f"Consider increasing max_context_chars if critical info is at end of document."
            )

        return text, was_truncated

    def normalize_with_schema(
        self, data: Dict[str, Any], schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Fill absent optional fields with null only when their schema permits it.

        Required fields must already be present; non-nullable optional fields
        remain absent rather than becoming invalid null values.

        Parameters
        ----------
        data : Dict[str, Any]
            Extracted data dictionary
        schema : Dict[str, Any]
            JSON schema defining expected structure

        Returns
        -------
        Dict[str, Any]
            Data with absent nullable optional fields filled
        """
        validator = validator_for(schema)(schema, format_checker=FormatChecker())

        def fill_nullable(value, subschema):
            if not isinstance(subschema, dict):
                return
            if isinstance(value, dict):
                required = subschema.get("required", [])
                for field, field_schema in subschema.get("properties", {}).items():
                    if field in value:
                        fill_nullable(value[field], field_schema)
                    elif (
                        field not in required
                        and validator.evolve(schema=field_schema).is_valid(None)
                    ):
                        value[field] = None
            elif isinstance(value, list):
                item_schema = subschema.get("items", {})
                for item in value:
                    fill_nullable(item, item_schema)

        fill_nullable(data, schema)
        return data

    def normalize_string_fields(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize string fields for consistency.

        Cleans whitespace, removes extra spaces, and standardizes formatting.
        Works generically with any schema structure.

        Parameters
        ----------
        data : Dict[str, Any]
            Extracted data dictionary

        Returns
        -------
        Dict[str, Any]
            Data with normalized string fields
        """

        def clean_string(value):
            """Clean individual string value."""
            if not isinstance(value, str):
                return value
            # Remove extra whitespace
            value = " ".join(value.split())
            return value.strip()

        def clean_dict(obj):
            """Recursively clean all strings in a dict."""
            if isinstance(obj, dict):
                return {k: clean_dict(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [clean_dict(item) for item in obj]
            elif isinstance(obj, str):
                return clean_string(obj)
            return obj

        return clean_dict(data)

    def run_sanity_checks(
        self, data: Dict[str, Any], schema_metadata=None
    ) -> List[str]:
        """
        Run post-extraction sanity checks to catch obvious errors.

        Generic checks that work for any schema type. Uses schema_metadata
        identifier_fields (when available) to build human-readable item labels.

        Parameters
        ----------
        data : Dict[str, Any]
            Extracted data dictionary
        schema_metadata
            Optional SchemaMetadata for identifier field lookup

        Returns
        -------
        List[str]
            List of warning messages
        """
        warnings = []

        # Find main array fields dynamically
        main_arrays = find_data_arrays(data)

        # Check 1: At least some items extracted
        total_items = sum(len(items) for _, items in main_arrays)
        if total_items == 0:
            warnings.append(
                "⚠️ WARNING: No items extracted - check if document contains expected data"
            )

        # Resolve item-level label fields from schema metadata.
        # Dedup key_fields are per-item identifying fields defined in the schema.
        id_field_names: List[str] = []
        if schema_metadata:
            try:
                key_fields = schema_metadata.get_deduplication_key_fields() or []
                id_field_names = [str(f) for f in key_fields if f]
            except Exception:
                pass

        # Check 2: Look for "or" in string fields (indicates multiple options not resolved)
        for array_name, items in main_arrays:
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue

                # Build a short identifier from the item's fields.
                # Use schema key_fields if available; otherwise pick the first
                # 2 short string values from the item (fully dynamic, no
                # hardcoded field names).
                id_parts = []
                if id_field_names:
                    for id_field in id_field_names:
                        val = item.get(id_field)
                        if val and isinstance(val, str) and len(val) < 80:
                            id_parts.append(val)
                else:
                    for val in item.values():
                        if isinstance(val, str) and 2 < len(val) < 60:
                            id_parts.append(val)
                            if len(id_parts) >= 2:
                                break
                item_label = " / ".join(id_parts[:2]) if id_parts else f"#{idx}"

                for field, value in item.items():
                    if isinstance(value, str):
                        if " or " in value.lower() or " / " in value:
                            warnings.append(
                                f"⚠️ WARNING: {array_name}[{idx}] ({item_label}).{field} contains multiple options: '{value}'"
                            )

        return warnings

    def calculate_completeness(
        self, data: Dict[str, Any], validation_notes: List[str]
    ) -> float:
        """
        Calculate completeness score based on fields populated.

        Generic scoring that works for any schema:

        - 0.3: Base score for successful extraction
        - 0.2: Has context fields (entity identifiers, location, etc.)
        - 0.5: Has item arrays with populated fields

        Parameters
        ----------
        data : Dict[str, Any]
            Extracted data dictionary
        validation_notes : List[str]
            List of validation notes (unused but kept for compatibility)

        Returns
        -------
        float
            Score from 0-1 indicating data completeness
        """
        score = 0.3  # Base score for successful extraction

        # Count non-empty fields in all top-level objects
        context_fields = 0
        total_context_fields = 0

        for key, value in data.items():
            if isinstance(value, dict):
                # This is a context object (metadata, jurisdiction, facility, etc.)
                for field_key, field_value in value.items():
                    total_context_fields += 1
                    if field_value not in [None, "", [], {}]:
                        context_fields += 1

        # Score context completeness
        if total_context_fields > 0:
            score += 0.2 * (context_fields / total_context_fields)

        # Count items in arrays and their completeness
        total_items = 0
        complete_items = 0

        for _key, value in find_data_arrays(data):
            total_items += len(value)
            # Count items with at least 50% of fields populated
            for item in value:
                if isinstance(item, dict):
                    item_fields = len(item)
                    populated_fields = sum(
                        1
                        for v in item.values()
                        if v not in [None, "", [], {}]
                    )
                    if (
                        item_fields > 0
                        and populated_fields / item_fields >= 0.5
                    ):
                        complete_items += 1

        # Score item completeness
        if total_items > 0:
            score += 0.5 * (complete_items / total_items)

        return min(score, 1.0)
