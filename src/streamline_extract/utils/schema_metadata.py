"""Schema metadata parsing and access.

This module enables schema-driven behavior without hardcoded domain logic.
All domain-specific knowledge should reside in schema metadata, not in code.

As of v2.0, all schemas MUST include a $metadata section.
"""

from pathlib import Path
from typing import Dict, Any, List, Optional
import json
import logging

from .exceptions import SchemaMetadataError

logger = logging.getLogger(__name__)


class SchemaMetadata:
    """
    Parse and provide access to schema metadata.
    
    Enables schema-driven behavior without hardcoded domain logic.
    
    As of v2.0, all schemas MUST include a $metadata section.
    Schemas without metadata will raise SchemaMetadataError.
    """
    
    def __init__(self, schema_path: Path):
        """
        Initialize with schema file path.
        
        Args:
            schema_path: Path to JSON schema file
            
        Raises:
            SchemaMetadataError: If schema lacks required $metadata section
        """
        self.schema_path = Path(schema_path)
        self.schema = self._load_schema()
        self.metadata = self.schema.get("$metadata", {})
        
        # Require metadata section in v2.0+
        if not self.metadata:
            raise SchemaMetadataError(
                "Schema missing required $metadata section.\n\n"
                "StreamlineExtract 2.0+ requires all schemas to include metadata.\n\n"
                "Required metadata structure:\n"
                "{\n"
                '  "$metadata": {\n'
                '    "extraction": {\n'
                '      "main_data_array": "your_main_array_key",\n'
                '      "context_objects": ["context_object_keys"],\n'
                '      "identifier_fields": ["path.to.identifier"]\n'
                '    },\n'
                '    "consolidation": {\n'
                '      "deduplication": {\n'
                '        "key_fields": ["unique_fields"],\n'
                '        "ignore_fields": ["fields_to_ignore"]\n'
                '      }\n'
                '    }\n'
                '  }\n'
                "}\n\n"
                "See: schemas/SCHEMA_BEST_PRACTICES.md for examples",
                schema_path=str(schema_path)
            )
        
        # Validate required metadata fields
        self._validate_metadata()
        
    def _load_schema(self) -> dict:
        """Load and parse JSON schema."""
        with open(self.schema_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def _validate_metadata(self) -> None:
        """
        Validate that metadata includes required fields.
        
        Raises:
            SchemaMetadataError: If required metadata fields are missing
        """
        extraction = self.metadata.get("extraction", {})
        
        # Check for required extraction metadata
        if not extraction.get("main_data_array"):
            raise SchemaMetadataError(
                "Schema metadata missing required field: extraction.main_data_array\n"
                "This field specifies the key containing the main data array.",
                schema_path=str(self.schema_path)
            )
        
        if not extraction.get("identifier_fields"):
            raise SchemaMetadataError(
                "Schema metadata missing required field: extraction.identifier_fields\n"
                "This field specifies the dot-notation paths to identifier fields.",
                schema_path=str(self.schema_path)
            )
    
    def has_metadata(self) -> bool:
        """Check if schema includes metadata section."""
        return bool(self.metadata)
    
    # --- Extraction Metadata ---
    
    def get_main_data_array(self) -> Optional[str]:
        """Get the key containing the main data array."""
        if self.metadata:
            return self.metadata.get("extraction", {}).get("main_data_array")
        return None
    
    def get_context_objects(self) -> List[str]:
        """Get keys containing context/metadata objects."""
        if self.metadata:
            return self.metadata.get("extraction", {}).get("context_objects", [])
        return []
    
    def get_identifier_fields(self) -> List[str]:
        """Get field paths that serve as identifiers."""
        if self.metadata:
            return self.metadata.get("extraction", {}).get("identifier_fields", [])
        return []
    
    def get_display_name_template(self) -> Optional[str]:
        """Get template for generating display names."""
        if self.metadata:
            return self.metadata.get("extraction", {}).get("display_name_template")
        return None
    
    def get_document_type(self) -> str:
        """Get human-readable document type."""
        if self.metadata:
            return self.metadata.get("extraction", {}).get("document_type", "Document")
        return "Document"
    
    # --- Consolidation Metadata ---
    
    def get_deduplication_key_fields(self) -> List[str]:
        """Get fields that define uniqueness for deduplication."""
        if self.metadata:
            dedup = self.metadata.get("consolidation", {}).get("deduplication", {})
            return dedup.get("key_fields", [])
        return []
    
    def get_deduplication_ignore_fields(self) -> List[str]:
        """Get fields to ignore during deduplication comparison."""
        if self.metadata:
            dedup = self.metadata.get("consolidation", {}).get("deduplication", {})
            return dedup.get("ignore_fields", [])
        return []
    
    def get_deduplication_strategy(self) -> str:
        """Get deduplication strategy (latest, earliest, merge)."""
        if self.metadata:
            dedup = self.metadata.get("consolidation", {}).get("deduplication", {})
            return dedup.get("strategy", "latest")
        return "latest"
    
    def get_comparison_mode(self) -> str:
        """Get comparison mode for deduplication (exact, fuzzy)."""
        if self.metadata:
            dedup = self.metadata.get("consolidation", {}).get("deduplication", {})
            return dedup.get("comparison_mode", "exact")
        return "exact"
    
    def get_output_format(self) -> str:
        """Get default output format."""
        if self.metadata:
            output = self.metadata.get("consolidation", {}).get("output", {})
            return output.get("default_format", "excel")
        return "excel"
    
    def get_column_order(self) -> List[str]:
        """Get preferred column order for output."""
        if self.metadata:
            output = self.metadata.get("consolidation", {}).get("output", {})
            return output.get("column_order", [])
        return []
    
    def get_freeze_columns(self) -> int:
        """Get number of columns to freeze in Excel output."""
        if self.metadata:
            output = self.metadata.get("consolidation", {}).get("output", {})
            return output.get("freeze_columns", 0)
        return 0
    
    def get_auto_width(self) -> bool:
        """Get whether to auto-size columns in Excel output."""
        if self.metadata:
            output = self.metadata.get("consolidation", {}).get("output", {})
            return output.get("auto_width", True)
        return True
    
    # --- Validation Metadata ---
    
    def get_required_fields(self) -> List[str]:
        """Get fields that must be present in extraction."""
        if self.metadata:
            validation = self.metadata.get("validation", {})
            return validation.get("required_fields", [])
        return []
    
    def get_quality_checks(self) -> List[Dict[str, Any]]:
        """Get custom quality validation rules."""
        if self.metadata:
            validation = self.metadata.get("validation", {})
            return validation.get("quality_checks", [])
        return []
    
    def get_completeness_threshold(self) -> float:
        """Get minimum completeness score (0.0-1.0)."""
        if self.metadata:
            validation = self.metadata.get("validation", {})
            return validation.get("completeness_threshold", 0.7)
        return 0.7
    
    # --- Helper Methods ---
    
    def extract_identifier_from_data(self, data: dict) -> str:
        """
        Extract identifier value from data using metadata.
        
        Args:
            data: Extracted data dictionary
            
        Returns:
            String identifier for the document/entity
            
        Raises:
            SchemaMetadataError: If identifier fields are not specified or found
        """
        identifier_fields = self.get_identifier_fields()
        
        # Metadata is required, so identifier_fields will always exist
        values = []
        for field_path in identifier_fields:
            value = self._get_nested_value(data, field_path)
            if value:
                values.append(str(value))
        
        if values:
            return " - ".join(values)
        
        # If no values found, that's a data problem, not a schema problem
        logger.warning(
            f"Could not extract identifier from data using fields: {identifier_fields}. "
            f"Data may be incomplete or fields may not exist in extraction."
        )
        return "UNKNOWN"
    
    def extract_context_from_data(self, data: dict) -> Dict[str, Any]:
        """
        Extract context fields from data using metadata.
        
        Args:
            data: Extracted data dictionary
            
        Returns:
            Dictionary of context fields with human-readable names
        """
        context = {}
        context_objects = self.get_context_objects()
        
        if context_objects:
            for context_key in context_objects:
                if context_key in data and isinstance(data[context_key], dict):
                    for k, v in data[context_key].items():
                        # Convert camelCase to Title Case with spaces
                        display_name = self._camel_to_title(k)
                        context[display_name] = v
        
        return context
    
    def extract_main_data_array(self, data: dict) -> List[Dict[str, Any]]:
        """
        Extract main data array from extraction results.
        
        Args:
            data: Extracted data dictionary
            
        Returns:
            List of data items, or empty list if not found
            
        Raises:
            SchemaMetadataError: If main data array key not specified
        """
        main_array_key = self.get_main_data_array()
        
        # Metadata is required, so main_array_key will always exist
        if main_array_key in data:
            array_data = data[main_array_key]
            if isinstance(array_data, list):
                return array_data
            else:
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
    
    def _get_nested_value(self, data: dict, path: str) -> Any:
        """
        Get value from nested dict using dot notation path.
        
        Args:
            data: Dictionary to traverse
            path: Dot-separated path (e.g., "company_info.cik")
            
        Returns:
            Value at path, or None if not found
        """
        keys = path.split('.')
        value = data
        
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
                if value is None:
                    return None
            else:
                return None
        
        return value
    
    def _camel_to_title(self, text: str) -> str:
        """
        Convert camelCase to Title Case with spaces.
        
        Args:
            text: camelCase string
            
        Returns:
            Title Case string
        """
        # Insert space before capital letters
        result = ''.join([' ' + c if c.isupper() else c for c in text]).strip()
        # Title case
        return result.title()
    
    def get_domain(self) -> str:
        """Get domain/category from metadata."""
        if self.metadata:
            return self.metadata.get("domain", "Unknown")
        return "Unknown"
    
    def get_version(self) -> str:
        """Get schema version from metadata."""
        if self.metadata:
            return self.metadata.get("version", "1.0.0")
        return "1.0.0"
    
    def get_description(self) -> str:
        """Get schema description from metadata."""
        if self.metadata:
            return self.metadata.get("description", "")
        return ""
