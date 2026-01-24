"""Schema structure detection for automatic consolidation."""

from typing import Dict, List, Any


class SchemaDetector:
    """
    Automatically detect and analyze schema structure from extracted data.
    
    Identifies:
    - Main data arrays (e.g., requirements, rate_schedules)
    - Identifier/context fields (e.g., jurisdiction, utility_details)
    - Schema patterns (ordinances, tariffs, etc.)
    
    This enables universal consolidation without manual configuration.
    """
    
    def detect_structure(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Automatically detect schema structure.
        
        Args:
            data: Sample data from first extraction file
            
        Returns:
            Dict with:
            - type: schema type name (e.g., "Ordinance/Regulation", "Utility Tariff")
            - main_array_key: key containing the main data array
            - id_fields: list of identifier/context field keys
            - object_keys: all object field keys
            
        Raises:
            ValueError: If no arrays found in data
            
        Example:
            >>> detector = SchemaDetector()
            >>> info = detector.detect_structure({"requirements": [...], "jurisdiction_details": {...}})
            >>> print(info['main_array_key'])
            'requirements'
        """
        # Find arrays in the data
        array_keys = [k for k, v in data.items() if isinstance(v, list) and len(v) > 0]
        object_keys = [k for k, v in data.items() if isinstance(v, dict)]
        
        if not array_keys:
            raise ValueError("No arrays found in schema. Unable to consolidate.")
        
        # Determine main array (usually the longest or most specific name)
        main_array_key = array_keys[0]
        if len(array_keys) > 1:
            # Prefer arrays with more specific names
            for key in array_keys:
                if any(word in key.lower() for word in ['requirement', 'plan', 'charge', 'item', 'entry']):
                    main_array_key = key
                    break
        
        # Determine schema type from key names
        schema_type = "Generic Document"
        if 'ordinance' in main_array_key.lower() or 'requirement' in main_array_key.lower():
            schema_type = "Ordinance/Regulation"
        elif 'rate' in main_array_key.lower() or 'tariff' in main_array_key.lower():
            schema_type = "Utility Tariff"
        elif 'plan' in main_array_key.lower():
            schema_type = "Service Plan"
        
        # Find identifier object(s)
        id_fields = []
        for obj_key in object_keys:
            if any(word in obj_key.lower() for word in ['details', 'info', 'jurisdiction', 'utility', 'company', 'metadata']):
                id_fields.append(obj_key)
        
        return {
            'type': schema_type,
            'main_array_key': main_array_key,
            'id_fields': id_fields,
            'object_keys': object_keys
        }
    
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
        
        for id_field_key in schema_info['id_fields']:
            id_obj = data.get(id_field_key, {})
            for k, v in id_obj.items():
                # Convert camelCase to Title Case with spaces
                display_name = ''.join([' ' + c if c.isupper() else c for c in k]).strip().title()
                context[display_name] = v
        
        return context
