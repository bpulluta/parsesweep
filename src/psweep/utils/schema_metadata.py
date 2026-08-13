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

    def get_column_renames(self) -> Dict[str, str]:
        """Get output column rename mapping."""
        output = self.metadata.get("compilation", {}).get("output", {})
        return output.get("column_renames", {})

    def get_column_order(self) -> List[str]:
        """Get preferred column order for output."""
        output = self.metadata.get("compilation", {}).get("output", {})
        return output.get("column_order", [])

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
        ``"State"`` (legacy behavior: normalize a column literally named
        ``State`` when present). Set to ``null``/``""`` in a schema to opt a
        non-US / non-jurisdiction domain out entirely.
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
