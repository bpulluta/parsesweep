"""Basic tests for extraction functionality."""

import pytest
from pathlib import Path
from streamline_extract.extraction.openai_client import OpenAIClient
from streamline_extract.extraction.text_processor import TextProcessor
from streamline_extract.extraction.schema_utils import (
    validate_schema,
    get_schema_fields,
    get_required_fields,
    is_field_required,
    count_total_fields,
)


class TestOpenAIClient:
    """Test OpenAI client initialization and configuration."""

    def test_client_initialization_openai(self):
        """Test OpenAI client initialization with default settings."""
        client = OpenAIClient(api_key="test-key")
        assert client.model == "gpt-4o-mini"
        assert client._client is not None

    def test_client_initialization_custom_model(self):
        """Test OpenAI client initialization with custom model."""
        client = OpenAIClient(api_key="test-key", model="gpt-4o")
        assert client.model == "gpt-4o"

    def test_client_initialization_azure(self):
        """Test Azure OpenAI client initialization."""
        client = OpenAIClient(
            api_key="test-key",
            use_azure=True,
            azure_endpoint="https://test.openai.azure.com/",
            azure_api_version="2024-02-15-preview",
        )
        assert client.model == "gpt-4o-mini"
        assert client._client is not None


class TestTextProcessor:
    """Test text processing utilities."""

    def test_text_optimization_truncation(self):
        """Test that text processor truncates long text correctly."""
        processor = TextProcessor(max_chars=100)
        long_text = "a" * 200
        optimized, _ = processor.optimize(long_text)
        assert len(optimized) <= 100

    def test_text_optimization_preserves_short_text(self):
        """Test that short text is preserved as-is."""
        processor = TextProcessor(max_chars=100)
        short_text = "Hello, world!"
        optimized, _ = processor.optimize(short_text)
        assert optimized == short_text

    def test_normalize_string_fields(self):
        """Test string field normalization removes extra whitespace."""
        processor = TextProcessor()
        data = {
            "field1": "  multiple   spaces  ",
            "field2": "normal text",
            "field3": None,
        }
        normalized = processor.normalize_string_fields(data)
        assert normalized["field1"] == "multiple spaces"
        assert normalized["field2"] == "normal text"
        assert normalized["field3"] is None


class TestSchemaUtils:
    """Test schema validation and utility functions."""

    def test_validate_schema_valid_object(self):
        """Test validation of valid object schema."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "number"},
            },
        }
        assert validate_schema(schema) is True

    def test_validate_schema_missing_type(self):
        """Test validation fails for schema missing type."""
        schema = {"properties": {"name": {"type": "string"}}}
        assert validate_schema(schema) is False

    def test_validate_schema_object_missing_properties(self):
        """Test validation fails for object schema missing properties."""
        schema = {"type": "object"}
        assert validate_schema(schema) is False

    def test_get_schema_fields(self):
        """Test extracting field names from schema."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "number"},
                "email": {"type": "string"},
            },
        }
        fields = get_schema_fields(schema)
        assert set(fields) == {"name", "age", "email"}

    def test_get_required_fields(self):
        """Test extracting required field names from schema."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "number"},
            },
            "required": ["name"],
        }
        required = get_required_fields(schema)
        assert required == ["name"]

    def test_is_field_required(self):
        """Test checking if specific field is required."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "number"},
            },
            "required": ["name"],
        }
        assert is_field_required(schema, "name") is True
        assert is_field_required(schema, "age") is False

    def test_count_total_fields(self):
        """Test counting total fields in schema."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "number"},
                "address": {
                    "type": "object",
                    "properties": {
                        "street": {"type": "string"},
                        "city": {"type": "string"},
                    },
                },
            },
        }
        # Without nested: 3 fields (name, age, address)
        assert count_total_fields(schema, include_nested=False) == 3
        # With nested: 5 fields (name, age, address, street, city)
        assert count_total_fields(schema, include_nested=True) == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
