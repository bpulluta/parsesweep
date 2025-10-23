"""Basic tests for the permit toolkit."""

import pytest
from pathlib import Path


def test_import_modules():
    """Test that all modules can be imported."""
    from permit_toolkit.scrapers.base import BaseScraper
    from permit_toolkit.extraction import PermitExtractor
    from permit_toolkit.consolidation import PermitConsolidator
    from permit_toolkit.utils import Config
    
    assert BaseScraper is not None
    assert PermitExtractor is not None
    assert PermitConsolidator is not None
    assert Config is not None


def test_config():
    """Test configuration management."""
    from permit_toolkit.utils import Config
    
    config = Config()
    assert config.project_root is not None
    assert config.data_dir is not None


def test_schema_exists():
    """Test that the schema file exists."""
    schema_path = Path("schemas/air_quality_permits_schema.json")
    assert schema_path.exists(), "Schema file not found"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
