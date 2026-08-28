"""Schema metadata parsing and access.

This module enables schema-driven behavior without hardcoded domain logic.
All domain-specific knowledge should reside in schema metadata, not in code.

As of v2.0, all schemas MUST include a $metadata section.
"""

from copy import deepcopy
from pathlib import Path
from typing import Dict, Any, List, Optional
import logging

from psweep.exceptions import SchemaMetadataError
from ..extraction.schema_utils import load_schema

logger = logging.getLogger(__name__)


# Shared severity token sets used to classify how impactful a field
# difference is (e.g. by both schema metadata inference and compilation
# deduplication). Single source of truth — do not duplicate elsewhere.
HIGH_SEVERITY_TOKENS = frozenset(
    {
        "unit",
        "amount",
        "value",
        "rate",
        "cost",
        "price",
        "capacity",
        "limit",
        "minimum",
        "maximum",
        "fee",
        "charge",
        "quantity",
        "output",
    }
)

MEDIUM_SEVERITY_TOKENS = frozenset(
    {
        "category",
        "type",
        "subject",
        "classification",
        "period",
        "season",
        "term",
        "description",
        "details",
        "section",
    }
)


class SchemaMetadata:
    """
    Parse and provide access to schema metadata.

    Enables schema-driven behavior without hardcoded domain logic.

    As of v2.0, all schemas MUST include a $metadata section.
    Schemas without metadata will raise SchemaMetadataError.
    """

    def __init__(
        self,
        schema_path: Path,
        metadata_overrides: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize with schema file path.

        Parameters
        ----------
        schema_path : Path
            Path to JSON schema file
        metadata_overrides : Optional[Dict[str, Any]]
            Optional runtime metadata overrides merged onto $metadata

        Raises
        ------
        SchemaMetadataError
            If schema lacks required $metadata section
        """
        self.schema_path = Path(schema_path)
        self.schema = self._load_schema()
        self.metadata = deepcopy(self.schema.get("$metadata", {}))

        if metadata_overrides:
            self.metadata = self._deep_merge_dicts(
                self.metadata, metadata_overrides
            )

        # Require metadata section in v2.0+
        if not self.metadata:
            raise SchemaMetadataError(
                "Schema missing required $metadata section.\n\n"
                "ParseSweep 2.0+ requires all schemas to include metadata.\n\n"
                "Required metadata structure:\n"
                "{\n"
                '  "$metadata": {\n'
                '    "extraction": {\n'
                '      "main_data_array": "your_main_array_key",\n'
                '      "context_objects": ["context_object_keys"],\n'
                '      "identifier_fields": ["path.to.identifier"]\n'
                "    },\n"
                '    "identity": {\n'
                '      "deduplication": {\n'
                '        "key_fields": ["unique_fields"],\n'
                '        "ignore_fields": ["fields_to_ignore"]\n'
                "      }\n"
                "    }\n"
                "  }\n"
                "}\n\n"
                "See: schemas/SCHEMA_BEST_PRACTICES.md for examples",
                schema_path=str(schema_path),
            )

        # Validate required metadata fields
        self._validate_metadata()

    def _load_schema(self) -> dict:
        """Load and parse JSON schema."""
        return load_schema(self.schema_path)

    def _validate_metadata(self) -> None:
        """
        Validate that metadata includes required fields.

        Raises
        ------
        SchemaMetadataError
            If required metadata fields are missing
        """
        extraction = self.metadata.get("extraction", {})
        identity = self.metadata.get("identity", {})
        legacy_compilation = self.metadata.get("compilation", {})

        # Check for required extraction metadata
        if not extraction.get("main_data_array"):
            raise SchemaMetadataError(
                "Schema metadata missing required field: extraction.main_data_array\n"
                "This field specifies the key containing the main data array.",
                schema_path=str(self.schema_path),
            )

        if not extraction.get("identifier_fields"):
            raise SchemaMetadataError(
                "Schema metadata missing required field: extraction.identifier_fields\n"
                "This field specifies the dot-notation paths to identifier fields.",
                schema_path=str(self.schema_path),
            )

        # Breaking-change guard: deduplication moved to metadata.identity.
        if (
            isinstance(legacy_compilation, dict)
            and isinstance(legacy_compilation.get("deduplication"), dict)
        ):
            raise SchemaMetadataError(
                "Schema metadata uses deprecated field: compilation.deduplication\n"
                "Move this block to identity.deduplication.\n"
                "Example:\n"
                '"identity": {\n'
                '  "deduplication": {\n'
                '    "key_fields": ["field_a"],\n'
                '    "ignore_fields": ["notes"]\n'
                "  }\n"
                "}\n",
                schema_path=str(self.schema_path),
            )

        if identity and not isinstance(identity, dict):
            raise SchemaMetadataError(
                "Schema metadata field identity must be an object when present.",
                schema_path=str(self.schema_path),
            )

        self._validate_cross_feature_redundancy(identity)

    def _validate_cross_feature_redundancy(self, identity: Dict[str, Any]) -> None:
        """Strictly validate the optional cross_feature_redundancy dedup block."""
        if not isinstance(identity, dict):
            return
        dedup = identity.get("deduplication", {})
        if not isinstance(dedup, dict):
            return
        cfg = dedup.get("cross_feature_redundancy")
        if cfg is None:
            return
        loc = "identity.deduplication.cross_feature_redundancy"
        if not isinstance(cfg, dict):
            raise SchemaMetadataError(
                f"Schema metadata field {loc} must be an object.",
                schema_path=str(self.schema_path),
            )
        allowed = {
            "group_by",
            "feature_field",
            "authority_ranking",
            "redundant_features",
        }
        unknown = sorted(set(cfg) - allowed)
        if unknown:
            raise SchemaMetadataError(
                f"{loc} has unknown key(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(allowed))}.",
                schema_path=str(self.schema_path),
            )

        def _require_str_list(key: str, *, allow_empty: bool) -> None:
            value = cfg.get(key)
            if value is None or not isinstance(value, list) or (
                not allow_empty and not value
            ):
                raise SchemaMetadataError(
                    f"{loc}.{key} must be a{'' if allow_empty else ' non-empty'} "
                    "array of strings.",
                    schema_path=str(self.schema_path),
                )
            if not all(isinstance(v, str) and v for v in value):
                raise SchemaMetadataError(
                    f"{loc}.{key} must contain only non-empty strings.",
                    schema_path=str(self.schema_path),
                )

        _require_str_list("group_by", allow_empty=False)
        _require_str_list("authority_ranking", allow_empty=True)
        _require_str_list("redundant_features", allow_empty=False)
        feature_field = cfg.get("feature_field")
        if not isinstance(feature_field, str) or not feature_field:
            raise SchemaMetadataError(
                f"{loc}.feature_field must be a non-empty string.",
                schema_path=str(self.schema_path),
            )

    def has_metadata(self) -> bool:
        """Check if schema includes metadata section."""
        return bool(self.metadata)

    def _deep_merge_dicts(
        self, base: Dict[str, Any], overrides: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Recursively merge override values onto the base metadata dictionary."""
        merged = deepcopy(base)

        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._deep_merge_dicts(merged[key], value)
            else:
                merged[key] = deepcopy(value)

        return merged

    # --- Extraction Metadata ---

    def get_main_data_array(self) -> str:
        """Get the key containing the main data array."""
        return self.metadata.get("extraction", {}).get("main_data_array")

    def get_context_objects(self) -> List[str]:
        """Get keys containing context/metadata objects."""
        return self.metadata.get("extraction", {}).get("context_objects", [])

    def get_identifier_fields(self) -> List[str]:
        """Get field paths that serve as identifiers."""
        return self.metadata.get("extraction", {}).get("identifier_fields", [])

    def get_display_name_template(self) -> Optional[str]:
        """Get template for generating display names."""
        return self.metadata.get("extraction", {}).get("display_name_template")

    def get_document_type(self) -> str:
        """Get human-readable document type."""
        return self.metadata.get("extraction", {}).get(
            "document_type", "Document"
        )

    # --- Compilation Metadata ---

    def get_deduplication_key_fields(self) -> List[str]:
        """Get fields that define uniqueness for deduplication."""
        dedup = self.metadata.get("identity", {}).get("deduplication", {})
        return dedup.get("key_fields", [])

    def get_deduplication_ignore_fields(self) -> List[str]:
        """Get fields to ignore during deduplication comparison."""
        dedup = self.metadata.get("identity", {}).get("deduplication", {})
        return dedup.get("ignore_fields", [])

    def get_deduplication_fuzzy_fields(self) -> List[str]:
        """Get key fields that use empty-as-wildcard + case-insensitive matching.

        Declared as ``identity.deduplication.fuzzy_key_fields`` in the schema.
        Accepts either a plain list of field names, or a list of objects with
        ``field`` and optional ``normalize`` keys::

            "fuzzy_key_fields": [
                "condition",
                {"field": "applies_to", "normalize": "words"}
            ]

        Normalization modes (applied before comparison when both values are
        non-empty):
        - ``"words"`` — lowercase, strip leading/trailing whitespace, collapse
          internal whitespace.  Removes common English articles and prepositions
          (``a``, ``an``, ``the``, ``of``, ``in``, ``at``, ``by``, ``for``,
          ``to``, ``from``) so phrasing variations like ``"compressor station"``
          vs ``"a compressor station"`` still match.  This is a generic
          text-normalization operation — no domain vocabulary.
        - ``None`` (default) — exact case-insensitive string equality after
          strip.

        Returns an empty list when not declared — callers fall back to strict
        exact matching for all key fields.
        """
        dedup = self.metadata.get("identity", {}).get("deduplication", {})
        raw = dedup.get("fuzzy_key_fields", [])
        # Normalize to plain list of field names for backward compat
        result = []
        for entry in raw:
            if isinstance(entry, str):
                result.append(entry)
            elif isinstance(entry, dict) and "field" in entry:
                result.append(entry["field"])
        return result

    def get_deduplication_fuzzy_field_config(self) -> dict:
        """Return per-fuzzy-field config keyed by field name.

        Each value is a dict with optional ``normalize`` key.  Fields declared
        as plain strings in ``fuzzy_key_fields`` get an empty config dict.
        """
        dedup = self.metadata.get("identity", {}).get("deduplication", {})
        raw = dedup.get("fuzzy_key_fields", [])
        config: dict = {}
        for entry in raw:
            if isinstance(entry, str):
                config[entry] = {}
            elif isinstance(entry, dict) and "field" in entry:
                config[entry["field"]] = {k: v for k, v in entry.items() if k != "field"}
        return config

    def get_deduplication_partition_fields(self) -> List[str]:
        """Get fields that isolate deduplication scope (never merge across them).

        Declared as ``identity.deduplication.partition_fields`` in the schema.
        Returns an empty list when not declared — no isolation boundaries are
        applied and all rows within the DataFrame are candidates for dedup.

        Schemas that need cross-document isolation (e.g. jurisdiction-based
        ordinance extraction where the same item name may legitimately appear
        in different counties) should declare this explicitly::

            "partition_fields": ["jurisdiction.state", "jurisdiction.county"]
        """
        dedup = self.metadata.get("identity", {}).get("deduplication", {})
        explicit = dedup.get("partition_fields")
        if explicit is not None:
            return explicit
        return []

    def get_cross_feature_redundancy_config(self) -> Optional[Dict[str, Any]]:
        """Config for the optional cross-feature approval-redundancy dedup pass.

        Declared as ``identity.deduplication.cross_feature_redundancy``. When
        absent (the default), the pass is a no-op — it is entirely opt-in and
        domain-neutral; the ranking, trigger columns, and interchangeable
        feature values all come from the schema, not from code.

        Expected shape::

            "cross_feature_redundancy": {
                "group_by": ["applies_to", "obligation"],
                "feature_field": "feature",
                "authority_ranking": ["permit_approval", "zoning_districts"],
                "redundant_features": ["permit_approval", "zoning_districts"]
            }
        """
        dedup = self.metadata.get("identity", {}).get("deduplication", {})
        cfg = dedup.get("cross_feature_redundancy")
        return cfg if isinstance(cfg, dict) else None

    def get_deduplication_strategy(self) -> str:
        """Get deduplication strategy (latest, earliest, merge)."""
        dedup = self.metadata.get("identity", {}).get("deduplication", {})
        return dedup.get("strategy", "latest")

    def get_comparison_mode(self) -> str:
        """Get comparison mode for deduplication (exact, fuzzy)."""
        dedup = self.metadata.get("identity", {}).get("deduplication", {})
        return dedup.get("comparison_mode", "exact")

    def get_output_format(self) -> str:
        """Get default output format."""
        output = self.metadata.get("compilation", {}).get("output", {})
        return output.get("default_format", "excel")

    def get_output_exclude_fields(self) -> List[str]:
        """Get fields that should be excluded from compiled outputs."""
        output = self.metadata.get("compilation", {}).get("output", {})
        return output.get("exclude_fields", [])

    def _schema_item_properties(self) -> List[str]:
        """Return field names from the main data array's item properties, in declaration order."""
        main_key = self.get_main_data_array()
        props = self.schema.get("properties", {})
        item_schema = props.get(main_key, {}).get("items", {})
        return list(item_schema.get("properties", {}).keys())

    def _schema_context_properties(self) -> List[str]:
        """Return field names from all context objects (e.g. jurisdiction), in declaration order."""
        props = self.schema.get("properties", {})
        fields: List[str] = []
        for obj_key in self.get_context_objects():
            obj_schema = props.get(obj_key, {})
            fields.extend(obj_schema.get("properties", {}).keys())
        return fields

    @staticmethod
    def _to_snake_case(name: str) -> str:
        """Convert a CamelCase or Title Case column header to snake_case."""
        import re
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
        s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
        return s.replace(" ", "_").lower()

    def get_column_renames(self) -> Dict[str, str]:
        """Return column rename mapping.

        Merges two sources in priority order (highest last):
        1. Auto-derived snake_case renames for every item field AND context
           object field in the schema (Title Case → snake_case).
        2. Explicit overrides from compilation.output.column_renames in config/schema.
        This ensures new schema fields are always renamed correctly without
        manual config updates.
        """
        auto: Dict[str, str] = {}
        for field in self._schema_item_properties() + self._schema_context_properties():
            snake = self._to_snake_case(field)
            if snake != field:
                auto[field] = snake
            title = field.replace("_", " ").title()
            if title != field:
                auto[title] = snake
            # Handle Title_Underscore form produced by the flattener (e.g. Jurisdiction_Type)
            title_us = "_".join(w.capitalize() for w in field.split("_"))
            if title_us != field:
                auto[title_us] = snake

        output = self.metadata.get("compilation", {}).get("output", {})
        explicit = output.get("column_renames", {})
        auto.update(explicit)
        return auto

    def get_column_order(self) -> List[str]:
        """Return column ordering.

        If compilation.output.column_order is set, it acts as a *priority prefix*:
        those columns appear first (in the listed order), then all remaining
        schema item fields appear in their declaration order, then context fields.
        If column_order is empty/absent, item fields then context fields are used.
        """
        output = self.metadata.get("compilation", {}).get("output", {})
        priority = output.get("column_order", [])

        schema_fields = [self._to_snake_case(f) for f in self._schema_item_properties()]
        context_fields = [self._to_snake_case(f) for f in self._schema_context_properties()]

        if not priority:
            return schema_fields + context_fields

        priority_set = set(priority)
        remainder_items = [f for f in schema_fields if f not in priority_set]
        remainder_ctx = [f for f in context_fields if f not in priority_set]
        return priority + remainder_items + remainder_ctx

    def get_freeze_columns(self) -> int:
        """Get number of columns to freeze in Excel output."""
        output = self.metadata.get("compilation", {}).get("output", {})
        return output.get("freeze_columns", 0)

    def get_auto_width(self) -> bool:
        """Get whether to auto-size columns in Excel output."""
        output = self.metadata.get("compilation", {}).get("output", {})
        return output.get("auto_width", True)

    def get_flattening_config(self) -> Dict[str, Any]:
        """Return the optional ``compilation.flattening`` block.

        Domain-neutral knobs the flattener consults to decide how arrays of
        objects become spreadsheet columns. Every key is optional; when a key
        is absent the flattener falls back to its documented general defaults,
        so schemas that declare nothing keep the legacy behavior.
        """
        return self.metadata.get("compilation", {}).get("flattening", {})

    def get_state_normalization_column(self) -> Optional[str]:
        """Column whose US-state names should be normalized to abbreviations.

        Driven by ``compilation.normalization.state_column``. Defaults to
        ``"State"`` to preserve existing behavior for schemas that have a
        jurisdiction State column.  Set to ``null``/``""`` in a schema to
        opt a non-US / non-jurisdiction domain out entirely.

        To make state normalization opt-in for a new domain schema, set
        ``compilation.normalization.state_column: null`` explicitly.
        """
        normalization = self.metadata.get("compilation", {}).get(
            "normalization", {}
        )
        if "state_column" not in normalization:
            return "State"
        column = normalization.get("state_column")
        return column or None

    def get_compilation_field_severity_hints(self) -> Dict[str, str]:
        """Return schema-derived severity hints for compiled output fields."""
        hints: Dict[str, str] = {}
        properties = self.schema.get("properties", {})

        for context_key in self.get_context_objects():
            context_schema = properties.get(context_key)
            if isinstance(context_schema, dict):
                self._collect_object_field_severity_hints(
                    context_schema,
                    hints,
                    prefix=None,
                )

        main_array_key = self.get_main_data_array()
        main_array_schema = properties.get(main_array_key)
        if isinstance(main_array_schema, dict):
            item_schema = main_array_schema.get("items", {})
            if isinstance(item_schema, dict):
                self._collect_object_field_severity_hints(
                    item_schema,
                    hints,
                    prefix=None,
                )

        return hints

    # --- Validation Metadata ---

    def get_required_fields(self) -> List[str]:
        """Get fields that must be present in extraction."""
        validation = self.metadata.get("validation", {})
        return validation.get("required_fields", [])

    def get_quality_checks(self) -> List[Dict[str, Any]]:
        """Get custom quality validation rules."""
        validation = self.metadata.get("validation", {})
        return validation.get("quality_checks", [])

    def get_completeness_threshold(self) -> float:
        """Get minimum completeness score (0.0-1.0)."""
        validation = self.metadata.get("validation", {})
        return validation.get("completeness_threshold", 0.7)

    # --- Helper Methods ---

    def extract_main_data_array(self, data: dict) -> List[Dict[str, Any]]:
        """
        Extract main data array from extraction results.

        Parameters
        ----------
        data : dict
            Extracted data dictionary

        Returns
        -------
        List[Dict[str, Any]]
            List of data items, or empty list if not found

        Raises
        ------
        SchemaMetadataError
            If main data array key not specified
        """
        main_array_key = self.get_main_data_array()

        # Metadata is required, so main_array_key will always exist
        if main_array_key in data:
            array_data = data[main_array_key]
            if isinstance(array_data, list):
                return array_data
            if isinstance(array_data, dict):
                # Some providers occasionally emit array-like objects keyed by
                # string indices ("0", "1", ...). Normalize these deterministically.
                try:
                    normalized = [
                        array_data[key]
                        for key in sorted(array_data.keys(), key=int)
                    ]
                except (TypeError, ValueError):
                    logger.warning(
                        f"Field '{main_array_key}' exists but is not a list. "
                        "Received dict with non-numeric keys; expected array-like data."
                    )
                    return []
                logger.warning(
                    f"Field '{main_array_key}' was emitted as an object-map; "
                    "normalizing to list by numeric key order."
                )
                return normalized
            logger.warning(
                f"Field '{main_array_key}' exists but is not a list. "
                f"Expected array but got {type(array_data).__name__}."
            )
        else:
            logger.warning(
                f"Main data array key '{main_array_key}' not found in extraction data. "
                f"Available keys: {list(data.keys())}"
            )

        return []

    def get_domain(self) -> str:
        """Get domain/category from metadata."""
        return self.metadata.get("domain", "Unknown")

    def get_version(self) -> str:
        """Get schema version from metadata."""
        return self.metadata.get("version", "1.0.0")

    def get_description(self) -> str:
        """Get schema description from metadata."""
        return self.metadata.get("description", "")

    def _collect_object_field_severity_hints(
        self,
        schema_node: Dict[str, Any],
        hints: Dict[str, str],
        *,
        prefix: Optional[str],
    ) -> None:
        """Collect severity hints from an object schema using compilation naming rules."""
        properties = schema_node.get("properties", {})
        if not isinstance(properties, dict):
            return

        for field_name, field_schema in properties.items():
            if not isinstance(field_schema, dict):
                continue

            column_name = f"{prefix}_{field_name}" if prefix else field_name
            severity = self._infer_schema_field_severity(
                field_name, field_schema
            )
            if severity:
                existing = hints.get(column_name)
                hints[column_name] = self._max_severity(existing, severity)

            field_type = field_schema.get("type")
            if field_type == "object":
                self._collect_object_field_severity_hints(
                    field_schema,
                    hints,
                    prefix=column_name,
                )
            elif field_type == "array":
                item_schema = field_schema.get("items", {})
                if (
                    isinstance(item_schema, dict)
                    and item_schema.get("type") == "object"
                ):
                    # Nested object arrays can become direct row columns when projected.
                    self._collect_object_field_severity_hints(
                        item_schema,
                        hints,
                        prefix=None,
                    )

    def _infer_schema_field_severity(
        self,
        field_name: str,
        field_schema: Dict[str, Any],
    ) -> Optional[str]:
        """Infer severity from the user-authored schema field definition."""
        normalized_name = field_name.replace("_", " ").lower()
        field_types = field_schema.get("type")
        if isinstance(field_types, str):
            field_types = [field_types]
        elif not isinstance(field_types, list):
            field_types = []

        if any(
            field_type in {"number", "integer"} for field_type in field_types
        ):
            return "high"

        if any(
            token in normalized_name for token in HIGH_SEVERITY_TOKENS
        ):
            return "high"

        if field_schema.get("enum"):
            return "medium"

        if any(
            token in normalized_name for token in MEDIUM_SEVERITY_TOKENS
        ):
            return "medium"

        return None

    def _max_severity(self, current: Optional[str], candidate: str) -> str:
        """Keep the highest-severity hint when multiple schema paths map to a field."""
        order = {"low": 0, "medium": 1, "high": 2}
        if current is None:
            return candidate
        return candidate if order[candidate] > order[current] else current

    # --- QA/QC ---
    # All QA/QC runtime config (record_matching, comparison fields, expected_requirements,
    # model tiers, lane behavior) lives in config/<domain>/run.yaml.
    # Schema metadata carries only the extraction contract — nothing QA/QC-specific.
