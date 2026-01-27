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