"""Schema structure detection for automatic consolidation."""

from typing import Dict, List, Any
import logging

from ..utils.exceptions import SchemaMetadataError

logger = logging.getLogger(__name__)


class SchemaDetector:
    """
    Automatically detect and analyze schema structure from extracted data.
    
    Requires schema metadata for operation (v2.0+).
    
    Identifies:
    - Main data arrays (e.g., requirements, rate_schedules)
    - Identifier/context fields (e.g., jurisdiction, utility_details)
    - Schema patterns from metadata
    
    This enables universal consolidation without manual configuration.
    """
    
    def __init__(self, schema_metadata):
        """
        Initialize detector.
        
        Args:
            schema_metadata: SchemaMetadata instance (required in v2.0+)
            
        Raises:
            SchemaMetadataError: If schema_metadata is not provided
        """
        if not schema_metadata:
            raise SchemaMetadataError(
                "SchemaDetector requires schema metadata.\\n"
                "Schema metadata is required as of StreamlineExtract v2.0."
            )
        self.schema_metadata = schema_metadata
    
    def detect_structure(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Automatically detect schema structure using metadata.
        
        Args:
            data: Sample data from first extraction file
            
        Returns:
            Dict with:
            - type: schema type name (e.g., "Ordinance/Regulation", "Utility Tariff")
            - main_array_key: key containing the main data array
            - id_fields: list of identifier/context field keys
            - object_keys: all object field keys
            
        Example:
            >>> detector = SchemaDetector(schema_metadata)
            >>> info = detector.detect_structure({"requirements": [...], "jurisdiction_details": {...}})
            >>> print(info['main_array_key'])
            'requirements'
        """
        # Use metadata (required in v2.0+)
        return {
            'type': self.schema_metadata.get_document_type(),
            'main_array_key': self.schema_metadata.get_main_data_array(),
            'id_fields': self.schema_metadata.get_context_objects(),
            'object_keys': self._find_object_keys(data),
        }
    
    def _find_object_keys(self, data: Dict[str, Any]) -> List[str]:
        """Find all object-type keys in data."""
        return [k for k, v in data.items() if isinstance(v, dict)]
    
    def extract_context(self, data: Dict[str, Any], schema_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract context/identifier fields from the document.
        
        Args:
            data: Extraction data
            schema_info: Schema structure info from detect_structure()
            
        Returns:
            Dict of context fields with human-readable names
            
        Example:
            >>> context = detector.extract_context(data, schema_info)
            >>> # Returns: {"Jurisdiction": "Austin", "State": "TX"}
        """
        context = {}
        
        # Get excluded fields from schema metadata
        exclude_fields = self._get_exclude_fields()
        
        for id_field_key in schema_info['id_fields']:
            id_obj = data.get(id_field_key, {})
            for k, v in id_obj.items():
                # Skip excluded fields
                if k in exclude_fields:
                    continue
                
                # Convert camelCase to Title Case with spaces
                display_name = ''.join([' ' + c if c.isupper() else c for c in k]).strip().title()
                context[display_name] = v
        
        return context
    
    def _get_exclude_fields(self) -> List[str]:
        """
        Get list of field names to exclude from consolidated output.
        
        Returns:
            List of field names (in snake_case) to exclude
        """
        metadata = self.schema_metadata.metadata
        if 'consolidation' in metadata and 'output' in metadata['consolidation']:
            return metadata['consolidation']['output'].get('exclude_fields', [])
        return []
