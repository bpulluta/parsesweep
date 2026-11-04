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
                '      "context_objects": ["context_object_keys"]\n'
                "    },\n"
                '    "identity": {\n'
                '      "deduplication": {\n'
                '        "key_fields": ["unique_fields"],\n'
                '        "ignore_fields": ["fields_to_ignore"]\n'
                "      }\n"
                "    }\n"
                "  }\n"
                "}\n\n"
                "See: docs/schemas/best-practices.md for examples",
                schema_path=str(schema_path),
            )

        # Validate required metadata fields
        self._validate_metadata()

    @classmethod
    def for_compilation(cls, schema_path: Path, resolved: Dict[str, Any]):
        """Build the effective schema/runtime compilation contract once."""
        compilation = {
            target: resolved[source]
            for source, target in (
                ("compilation_output", "output"),
                ("normalization", "normalization"),
                ("severity_tokens", "severity_tokens"),
            )
            if isinstance(resolved.get(source), dict)
        }
        return cls(schema_path, {"compilation": compilation} if compilation else None)

    def _schema_node_at_path(
        self, node: dict, parts: list[str],
    ) -> Optional[dict]:
        """Resolve local references along the path and at its leaf."""
        for part in [*parts, None]:
            visited = set()
            while isinstance(node, dict) and "$ref" in node:
                ref = node["$ref"]
                if not ref.startswith("#/") or ref in visited:
                    return None
                visited.add(ref)
                node = self.schema
                for token in ref[2:].split("/"):
                    if not isinstance(node, dict):
                        return None
                    key = token.replace("~1", "/").replace("~0", "~")
                    node = node.get(key, {})
            if not isinstance(node, dict):
                return None
            if part is not None:
                node = node.get("properties", {}).get(part)
        return node

    def resolve_schema_field(
        self, path: str, *, root: Optional[dict] = None,
    ) -> tuple[str, dict]:
        """Resolve an item/context alias or an exact root-relative path."""
        if root is not None:
            found = self._schema_node_at_path(root, path.split("."))
            if found is None:
                self._raise_meta(f"Compilation unknown schema field: {path}")
            return path, found
        properties = self.schema.get("properties", {})
        main = self.get_main_data_array()
        item = properties.get(main, {}).get("items", {})
        descend = self._schema_node_at_path

        candidates = []
        parts = path.split(".")
        array_node = properties.get(parts[0], {})
        if parts[0] == main or (len(parts) > 1 and "items" in array_node):
            scoped_item = array_node.get("items", {})
            found = descend(scoped_item, parts[1:])
            if found is not None:
                canonical = (
                    path if len(parts) > 1 and parts[1] in self.get_context_objects()
                    else ".".join(parts[1:])
                )
                candidates.append((canonical, found))
        else:
            found = descend(item, parts)
            if found is not None:
                candidates.append((path, found))
            if parts[0] in self.get_context_objects():
                found = descend(properties.get(parts[0], {}), parts[1:])
                if found is not None:
                    candidates.append((path, found))
            else:
                for context in self.get_context_objects():
                    found = descend(properties.get(context, {}), parts)
                    if found is not None:
                        candidates.append((f"{context}.{path}", found))
        if len(candidates) != 1:
            reason = "ambiguous" if candidates else "unknown"
            self._raise_meta(f"Compilation {reason} schema field: {path}")
        return candidates[0]

    def get_output_formats(self) -> List[str]:
        """Resolve effective formats; an absent setting retains CSV + Excel."""
        requested = self.metadata.get("compilation", {}).get("output", {}).get(
            "default_format", "both"
        )
        formats = {"csv": ["csv"], "excel": ["excel"], "xlsx": ["excel"],
                   "both": ["csv", "excel"], "all": ["csv", "excel"]}
        normalized = str(requested).strip().lower()
        if normalized not in formats:
            self._raise_meta("compilation.output.default_format must be csv, excel, or both")
        return formats[normalized]

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
        # Check for required extraction metadata
        if not extraction.get("main_data_array"):
            raise SchemaMetadataError(
                "Schema metadata missing required field: extraction.main_data_array\n"
                "This field specifies the key containing the main data array.",
                schema_path=str(self.schema_path),
            )

        if identity and not isinstance(identity, dict):
            raise SchemaMetadataError(
                "Schema metadata field identity must be an object when present.",
                schema_path=str(self.schema_path),
            )

        self._validate_cross_feature_redundancy(identity)
        self._validate_identity_deduplication(identity)
        self._validate_compilation_block()

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

    def _raise_meta(self, message: str) -> None:
        """Raise a SchemaMetadataError carrying the schema path (helper)."""
        raise SchemaMetadataError(message, schema_path=str(self.schema_path))

    def _require_str_list_meta(
        self, value: Any, loc: str, *, allow_empty: bool = True
    ) -> None:
        """Require ``value`` to be a list of non-empty strings (or absent)."""
        if value is None:
            return
        if not isinstance(value, list) or (not allow_empty and not value):
            qualifier = "an" if allow_empty else "a non-empty"
            self._raise_meta(
                f"Schema metadata field {loc} must be {qualifier} array of "
                "strings."
            )
        if not all(isinstance(v, str) and v for v in value):
            self._raise_meta(
                f"Schema metadata field {loc} must contain only non-empty "
                "strings."
            )

    def _require_positive_int_meta(self, value: Any, loc: str) -> None:
        """Require ``value`` to be a positive integer (or absent)."""
        if value is None:
            return
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            self._raise_meta(
                f"Schema metadata field {loc} must be a positive integer "
                f"(got {value!r})."
            )

    def _validate_identity_deduplication(
        self, identity: Dict[str, Any]
    ) -> None:
        """Strictly validate the ``identity.deduplication`` block structure.

        Domain-neutral structural/type checks with unknown-key rejection so a
        mistyped key (e.g. ``key_field`` for ``key_fields``) fails loudly
        instead of silently reverting to defaults. The nested
        ``cross_feature_redundancy`` block is validated separately by
        :meth:`_validate_cross_feature_redundancy`.
        """
        if not isinstance(identity, dict):
            return
        dedup = identity.get("deduplication")
        if dedup is None:
            return
        loc = "identity.deduplication"
        if not isinstance(dedup, dict):
            self._raise_meta(f"Schema metadata field {loc} must be an object.")

        allowed = {
            "key_fields",
            "ignore_fields",
            "fuzzy_key_fields",
            "partition_fields",
            "strategy",
            "comparison_mode",
            "cross_feature_redundancy",
        }
        unknown = sorted(set(dedup) - allowed)
        if unknown:
            self._raise_meta(
                f"{loc} has unknown key(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(allowed))}."
            )

        for key in ("key_fields", "ignore_fields", "partition_fields"):
            self._require_str_list_meta(dedup.get(key), f"{loc}.{key}")

        for key in ("strategy", "comparison_mode"):
            value = dedup.get(key)
            if value is not None and (not isinstance(value, str) or not value):
                self._raise_meta(
                    f"{loc}.{key} must be a non-empty string."
                )

        # fuzzy_key_fields accepts a mixed list of plain field-name strings and
        # objects of the form {"field": "...", "normalize": "..."} (see
        # get_deduplication_fuzzy_field_config).
        fuzzy = dedup.get("fuzzy_key_fields")
        if fuzzy is not None:
            if not isinstance(fuzzy, list):
                self._raise_meta(
                    f"{loc}.fuzzy_key_fields must be an array."
                )
            for entry in fuzzy:
                if isinstance(entry, str):
                    if not entry:
                        self._raise_meta(
                            f"{loc}.fuzzy_key_fields string entries must be "
                            "non-empty."
                        )
                elif isinstance(entry, dict):
                    field = entry.get("field")
                    if not isinstance(field, str) or not field:
                        self._raise_meta(
                            f"{loc}.fuzzy_key_fields object entries require a "
                            "non-empty string 'field'."
                        )
                    normalize = entry.get("normalize")
                    if normalize is not None and not isinstance(normalize, str):
                        self._raise_meta(
                            f"{loc}.fuzzy_key_fields['{field}'].normalize must "
                            "be a string."
                        )
                else:
                    self._raise_meta(
                        f"{loc}.fuzzy_key_fields entries must be strings or "
                        "objects with a 'field' key."
                    )

    def _validate_compilation_block(self) -> None:
        """Strictly validate the schema-side ``compilation`` sub-blocks.

        Only the framework-owned sub-blocks the code actually consumes are
        checked (``flattening``, ``normalization``, ``output``); other
        ``compilation`` keys some schemas carry (e.g. temporal_precision) are
        left untouched. Unknown keys *within* a validated sub-block are
        rejected so a typo fails loudly instead of silently reverting to a
        default.
        """
        compilation = self.metadata.get("compilation")
        if compilation is None:
            return
        if not isinstance(compilation, dict):
            self._raise_meta(
                "Schema metadata field compilation must be an object."
            )

        flattening = compilation.get("flattening")
        if flattening is not None:
            self._validate_flattening_block(flattening)

        normalization = compilation.get("normalization")
        if normalization is not None:
            self._validate_normalization_block(normalization)

        output = compilation.get("output")
        if output is not None:
            self._validate_output_block(output)

        severity_tokens = compilation.get("severity_tokens")
        if severity_tokens is not None:
            self._validate_severity_tokens_block(severity_tokens)

    def _validate_severity_tokens_block(self, severity_tokens: Any) -> None:
        """Validate ``compilation.severity_tokens`` (see get_severity_tokens)."""
        loc = "compilation.severity_tokens"
        if not isinstance(severity_tokens, dict):
            self._raise_meta(f"Schema metadata field {loc} must be an object.")
        allowed = {"high", "medium"}
        unknown = sorted(set(severity_tokens) - allowed)
        if unknown:
            self._raise_meta(
                f"{loc} has unknown key(s): {', '.join(unknown)}. "
                "Allowed: high, medium."
            )
        for key in ("high", "medium"):
            self._require_str_list_meta(
                severity_tokens.get(key), f"{loc}.{key}"
            )

    def _validate_flattening_block(self, flattening: Any) -> None:
        loc = "compilation.flattening"
        if not isinstance(flattening, dict):
            self._raise_meta(f"Schema metadata field {loc} must be an object.")
        allowed = {"row_array", "unit_fields", "unit_normalizations"}
        unknown = sorted(set(flattening) - allowed)
        if unknown:
            self._raise_meta(
                f"{loc} has unknown key(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(allowed))}."
            )
        row_array = flattening.get("row_array")
        if row_array is not None and (not isinstance(row_array, str) or not row_array):
            self._raise_meta(f"{loc}.row_array must be a nonempty field name or null.")
        self._require_str_list_meta(flattening.get("unit_fields"), f"{loc}.unit_fields")
        unit_norm = flattening.get("unit_normalizations")
        if unit_norm is not None:
            if not isinstance(unit_norm, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in unit_norm.items()
            ):
                self._raise_meta(f"{loc}.unit_normalizations must map strings to strings.")
            if not flattening.get("unit_fields"):
                self._raise_meta(f"{loc}.unit_normalizations requires {loc}.unit_fields.")
        self._require_output_fields(flattening.get("unit_fields") or [], f"{loc}.unit_fields")

    def _require_output_fields(self, fields: List[str], loc: str) -> None:
        unknown = [field for field in fields if field not in self._output_properties()]
        if unknown:
            self._raise_meta(f"{loc} names unknown schema field(s): {', '.join(unknown)}.")

    def get_row_array(self) -> Optional[str]:
        """Resolve nested row expansion from the schema, never from data order."""
        main = self._schema_node_at_path(self.schema, [self.get_main_data_array()]) or {}
        item = self._schema_node_at_path(main.get("items", {}), []) or {}
        candidates = []
        for name in item.get("properties", {}):
            node = self._schema_node_at_path(item, [name]) or {}
            types = node.get("type", [])
            child = self._schema_node_at_path(node.get("items", {}), []) or {}
            if ("array" == types or "array" in types) and (
                child.get("type") == "object" or "properties" in child
            ):
                candidates.append(name)
        flattening = self.get_flattening_config() or {}
        if "row_array" in flattening:
            selected = flattening["row_array"]
            if selected is not None and selected not in candidates:
                self._raise_meta("compilation.flattening.row_array must name an object array in the main item schema")
            return selected
        if len(candidates) > 1:
            self._raise_meta(
                "Multiple nested object arrays require compilation.flattening.row_array "
                "(use null to retain arrays as columns)"
            )
        return candidates[0] if candidates else None

    def _validate_normalization_block(self, normalization: Any) -> None:
        loc = "compilation.normalization"
        if not isinstance(normalization, dict):
            self._raise_meta(f"Schema metadata field {loc} must be an object.")
        allowed = {"state_column", "county_fips"}
        unknown = sorted(set(normalization) - allowed)
        if unknown:
            self._raise_meta(
                f"{loc} has unknown key(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(allowed))}."
            )
        column = normalization.get("state_column")
        if column is not None:
            if not isinstance(column, str) or not column:
                self._raise_meta(f"{loc}.state_column must be a non-empty string.")
            self._require_output_fields([column], f"{loc}.state_column")
        county_fips = normalization.get("county_fips")
        if county_fips is not None:
            if not isinstance(county_fips, dict) or set(county_fips) != {"state", "county"} or not all(
                isinstance(value, str) and value for value in county_fips.values()
            ):
                self._raise_meta(
                    f"{loc}.county_fips must be an object naming the state and county "
                    "fields, e.g. {state: state, county: county}."
                )
            self._require_output_fields(list(county_fips.values()), f"{loc}.county_fips")

    def _validate_output_block(self, output: Any) -> None:
        loc = "compilation.output"
        if not isinstance(output, dict):
            self._raise_meta(f"Schema metadata field {loc} must be an object.")
        allowed = {
            "default_format",
            "exclude_fields",
            "column_order",
            "freeze_columns",
            "auto_width",
            "annotation_column",
            "center_align_columns",
            "center_align_max_length",
        }
        unknown = sorted(set(output) - allowed)
        if unknown:
            self._raise_meta(
                f"{loc} has unknown key(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(allowed))}."
            )

        self.get_output_formats()

        for key in ("default_format", "annotation_column"):
            value = output.get(key)
            if value is not None and (not isinstance(value, str) or not value):
                self._raise_meta(f"{loc}.{key} must be a non-empty string.")

        for key in ("exclude_fields", "column_order", "center_align_columns"):
            self._require_str_list_meta(output.get(key), f"{loc}.{key}")

        max_len = output.get("center_align_max_length")
        if max_len is not None and (
            isinstance(max_len, bool)
            or not isinstance(max_len, (int, float))
            or max_len < 0
        ):
            self._raise_meta(
                f"{loc}.center_align_max_length must be a non-negative number "
                f"(got {max_len!r})."
            )

        freeze = output.get("freeze_columns")
        if freeze is not None and (
            isinstance(freeze, bool) or not isinstance(freeze, int) or freeze < 0
        ):
            self._raise_meta(
                f"{loc}.freeze_columns must be a non-negative integer "
                f"(got {freeze!r})."
            )

        auto_width = output.get("auto_width")
        if auto_width is not None and not isinstance(auto_width, bool):
            self._raise_meta(f"{loc}.auto_width must be a boolean.")

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
        # Both supported forms expose the same field list to callers.
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

    def get_output_exclude_fields(self) -> List[str]:
        """Get fields that should be excluded from compiled outputs."""
        output = self.metadata.get("compilation", {}).get("output", {})
        return output.get("exclude_fields", [])

    @staticmethod
    def _types(prop: Dict[str, Any]) -> set:
        declared = prop.get("type")
        if isinstance(declared, str):
            return {declared}
        if isinstance(declared, list):
            return {t for t in declared if isinstance(t, str)}
        return set()

    def _properties(self, node: Any, path: List[str]) -> Dict[str, Any]:
        found = self._schema_node_at_path(node, path) or {}
        return {
            name: self._schema_node_at_path(found, [name]) or {}
            for name in found.get("properties", {})
        }

    def _output_properties(self) -> Dict[str, Dict[str, Any]]:
        """Context, item, and row-array child properties keyed by output field, in schema order."""
        result: Dict[str, Dict[str, Any]] = {}
        for context in self.get_context_objects():
            result.update(self._properties(self.schema, [context]))
        main = self._schema_node_at_path(self.schema, [self.get_main_data_array()]) or {}
        item = main.get("items", {})
        row_array = self.get_row_array()
        for name, prop in self._properties(item, []).items():
            if name != row_array:
                result[name] = prop
        if row_array:
            child = (self._schema_node_at_path(item, [row_array]) or {}).get("items", {})
            result.update(self._properties(child, []))
        return result

    def get_output_fields(self) -> List[str]:
        return list(self._output_properties())

    def get_field_types(self) -> Dict[str, set]:
        return {name: self._types(prop) for name, prop in self._output_properties().items()}

    def get_column_titles(self) -> Dict[str, str]:
        """Output header per field: the schema ``title`` when set (else the field name)."""
        return {
            name: prop["title"]
            for name, prop in self._output_properties().items()
            if isinstance(prop.get("title"), str) and prop["title"]
        }

    def get_column_order(self) -> List[str]:
        """Output headers listed first; remaining columns keep their default order."""
        return self.metadata.get("compilation", {}).get("output", {}).get("column_order", [])

    def get_freeze_columns(self) -> int:
        """Get number of columns to freeze in Excel output."""
        output = self.metadata.get("compilation", {}).get("output", {})
        return output.get("freeze_columns", 0)

    def get_auto_width(self) -> bool:
        """Get whether to auto-size columns in Excel output."""
        output = self.metadata.get("compilation", {}).get("output", {})
        return output.get("auto_width", True)

    def get_center_align_columns(self) -> Optional[List[str]]:
        """Return explicit center-aligned Excel column names, if configured.

        ``None`` means "unset" — the formatter uses its built-in default name
        list. An empty list disables name-based centering entirely.
        """
        output = self.metadata.get("compilation", {}).get("output", {})
        return output.get("center_align_columns")

    def get_center_align_max_length(self) -> Optional[float]:
        """Return the average-length threshold for center-aligning columns.

        ``None`` means "unset" — the formatter uses its built-in default
        threshold.
        """
        output = self.metadata.get("compilation", {}).get("output", {})
        return output.get("center_align_max_length")

    def get_flattening_config(self) -> Dict[str, Any]:
        return self.metadata.get("compilation", {}).get("flattening", {})

    def get_state_normalization_column(self) -> Optional[str]:
        """Field whose US-state names are abbreviated (opt-in)."""
        normalization = self.metadata.get("compilation", {}).get("normalization", {})
        return normalization.get("state_column")

    def get_county_fips_columns(self) -> Optional[Dict[str, str]]:
        """``{state, county}`` fields that derive the ``county_fips`` column (opt-in)."""
        normalization = self.metadata.get("compilation", {}).get("normalization", {})
        return normalization.get("county_fips")

    def get_severity_tokens(self) -> tuple[frozenset, frozenset]:
        """Return the ``(high, medium)`` field-name severity token sets.

        These name-heuristic tokens classify how impactful a differing field is
        when the schema provides no explicit severity hint. A schema may extend
        either set via ``$metadata.compilation.severity_tokens.{high,medium}``;
        the configured tokens are unioned with the built-in defaults (the
        generic tokens are always retained), matching the supplement idiom used
        elsewhere. When unconfigured, the module defaults are returned unchanged.
        """
        compilation = self.metadata.get("compilation", {}) or {}
        tokens = compilation.get("severity_tokens", {}) or {}
        high = HIGH_SEVERITY_TOKENS | {
            str(t).lower() for t in (tokens.get("high") or [])
        }
        medium = MEDIUM_SEVERITY_TOKENS | {
            str(t).lower() for t in (tokens.get("medium") or [])
        }
        return frozenset(high), frozenset(medium)

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
        high_tokens, medium_tokens = self.get_severity_tokens()
        field_types = field_schema.get("type")
        if isinstance(field_types, str):
            field_types = [field_types]
        elif not isinstance(field_types, list):
            field_types = []

        if any(
            field_type in {"number", "integer"} for field_type in field_types
        ):
            return "high"

        if any(token in normalized_name for token in high_tokens):
            return "high"

        if field_schema.get("enum"):
            return "medium"

        if any(token in normalized_name for token in medium_tokens):
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
