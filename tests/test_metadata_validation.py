"""Test that components properly validate schema metadata requirements."""

import pytest
from psweep.compilation.deduplicator import Deduplicator
from psweep.compilation.data_compiler import DataCompiler
from psweep.exceptions import SchemaMetadataError


class TestComponentMetadataValidation:
    """Test that all compilation components require schema metadata."""

    def test_data_compiler_rejects_none_metadata(self):
        """Test that DataCompiler raises error when schema_metadata is None."""
        with pytest.raises(SchemaMetadataError, match="metadata"):
            DataCompiler(schema_metadata=None)

    def test_deduplicator_rejects_none_metadata(self):
        """Test that Deduplicator raises error when schema_metadata is None."""
        with pytest.raises(SchemaMetadataError, match="metadata"):
            Deduplicator(schema_metadata=None)
