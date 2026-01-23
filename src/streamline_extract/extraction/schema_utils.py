"""Schema manipulation and validation utilities."""

import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def load_schema(schema_path: Path) -> Dict[str, Any]:
    """
    Load JSON schema from file.
    
    Args:
        schema_path: Path to JSON schema file
        
    Returns:
        Parsed JSON schema as dictionary
        
    Raises:
        FileNotFoundError: If schema file doesn't exist
        json.JSONDecodeError: If schema file is not valid JSON
    """
    with open(schema_path, 'r') as f:
        return json.load(f)


def validate_schema(schema: Dict[str, Any]) -> bool:
    """
    Validate that a schema has the required structure.
    
    Checks:
    - Schema is a dictionary
    - Has 'type' field
    - For object types, has 'properties' field
    
    Args:
        schema: JSON schema to validate
        
    Returns:
        True if schema is valid, False otherwise
    """
    if not isinstance(schema, dict):
        logger.error("Schema must be a dictionary")
        return False
    
    if 'type' not in schema:
        logger.error("Schema missing 'type' field")
        return False
    
    if schema.get('type') == 'object' and 'properties' not in schema:
        logger.error("Object schema missing 'properties' field")
        return False
    
    return True


def get_schema_fields(schema: Dict[str, Any]) -> list:
    """
    Extract all field names from a JSON schema.
    
    Args:
        schema: JSON schema dictionary
        
    Returns:
        List of field names
    """
    if schema.get('type') == 'object':
        return list(schema.get('properties', {}).keys())
    return []


def get_required_fields(schema: Dict[str, Any]) -> list:
    """
    Extract required field names from a JSON schema.
    
    Args:
        schema: JSON schema dictionary
        
    Returns:
        List of required field names
    """
    return schema.get('required', [])


def is_field_required(schema: Dict[str, Any], field_name: str) -> bool:
    """
    Check if a specific field is required in the schema.
    
    Args:
        schema: JSON schema dictionary
        field_name: Name of the field to check
        
    Returns:
        True if field is required, False otherwise
    """
    return field_name in schema.get('required', [])


def get_field_type(schema: Dict[str, Any], field_name: str) -> Optional[str]:
    """
    Get the type of a specific field from the schema.
    
    Args:
        schema: JSON schema dictionary
        field_name: Name of the field
        
    Returns:
        Field type (e.g., 'string', 'number', 'array', 'object') or None if not found
    """
    properties = schema.get('properties', {})
    field_schema = properties.get(field_name, {})
    return field_schema.get('type')


def count_total_fields(schema: Dict[str, Any], include_nested: bool = True) -> int:
    """
    Count total number of fields in a schema.
    
    Args:
        schema: JSON schema dictionary
        include_nested: If True, count nested object fields recursively
        
    Returns:
        Total number of fields
    """
    if schema.get('type') != 'object':
        return 0
    
    properties = schema.get('properties', {})
    count = len(properties)
    
    if include_nested:
        for field_schema in properties.values():
            if field_schema.get('type') == 'object':
                count += count_total_fields(field_schema, include_nested=True)
            elif field_schema.get('type') == 'array':
                items = field_schema.get('items', {})
                if items.get('type') == 'object':
                    count += count_total_fields(items, include_nested=True)
    
    return count
