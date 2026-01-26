"""Test that components properly validate schema metadata requirements."""

import pytest
from pathlib import Path
from streamline_extract.consolidation.schema_detector import SchemaDetector
from streamline_extract.consolidation.deduplicator import Deduplicator
from streamline_extract.consolidation.cleaner import ExtractionCleaner
from streamline_extract.consolidation.consolidator import Consolidator
from streamline_extract.utils.exceptions import SchemaMetadataError


class TestComponentMetadataValidation:
    """Test that all consolidation components require schema metadata."""
    
    def test_consolidator_rejects_none_metadata(self):
        """Test that Consolidator raises error when schema_metadata is None."""
        with pytest.raises(SchemaMetadataError, match="metadata"):
            Consolidator(schema_metadata=None)
    
    def test_deduplicator_rejects_none_metadata(self):
        """Test that Deduplicator raises error when schema_metadata is None."""
        with pytest.raises(SchemaMetadataError, match="metadata"):
            Deduplicator(schema_metadata=None)
    
    def test_cleaner_rejects_none_metadata(self, tmp_path):
        """Test that ExtractionCleaner raises error when schema_metadata is None."""
        with pytest.raises(SchemaMetadataError, match="metadata"):
            ExtractionCleaner(
                extracted_dir=tmp_path,
                output_dir=tmp_path / "output",
                schema_metadata=None
            )
    
    def test_schema_detector_rejects_none_metadata(self):
        """Test that SchemaDetector raises error when schema_metadata is None."""
        with pytest.raises(SchemaMetadataError, match="metadata"):
            SchemaDetector(schema_metadata=None)


class TestMetadataValidationWithRealSchemas:
    """Integration tests using real schemas to validate metadata requirements."""
    
    def test_consolidator_with_valid_metadata(self):
        """Test that Consolidator works with valid schema metadata."""
        from streamline_extract.utils.schema_metadata import SchemaMetadata
        
        # Use a real schema
        schema_path = Path(__file__).parent.parent / "schemas" / "electricity_tariff_schema.json"
        if not schema_path.exists():
            pytest.skip("Electricity tariff schema not found")
        
        # Create SchemaMetadata - should work
        meta = SchemaMetadata(schema_path)
        
        # Create Consolidator with valid metadata - should work
        consolidator = Consolidator(schema_metadata=meta)
        
        assert consolidator is not None
        assert consolidator.schema_metadata is meta
    
    def test_all_real_schemas_have_valid_metadata(self):
        """Test that all production schemas have valid metadata in v2.0."""
        from streamline_extract.utils.schema_metadata import SchemaMetadata
        
        schemas_dir = Path(__file__).parent.parent / "schemas"
        if not schemas_dir.exists():
            pytest.skip("Schemas directory not found")
        
        schema_files = list(schemas_dir.glob("*.json"))
        if not schema_files:
            pytest.skip("No schema files found")
        
        # Track results
        valid_schemas = []
        invalid_schemas = []
        
        for schema_path in schema_files:
            # Skip archived schemas
            if "archive" in str(schema_path):
                continue
            
            try:
                meta = SchemaMetadata(schema_path)
                # Check required metadata fields
                assert meta.get_main_data_array() is not None
                assert meta.get_identifier_fields() is not None
                assert len(meta.get_identifier_fields()) > 0
                valid_schemas.append(schema_path.name)
            except SchemaMetadataError:
                invalid_schemas.append(schema_path.name)
        
        # In v2.0, all production schemas should have valid metadata
        assert len(invalid_schemas) == 0, f"Invalid schemas found: {invalid_schemas}"
        assert len(valid_schemas) > 0, "No valid schemas found"
        
        print(f"\n✓ All {len(valid_schemas)} production schemas have valid v2.0 metadata:")
        for schema in sorted(valid_schemas):
            print(f"  - {schema}")
