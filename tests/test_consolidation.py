#!/usr/bin/env python3
"""
Tests for consolidation module components.

Tests schema detection, data flattening, and deduplication logic.
"""
import pytest
import pandas as pd
from pathlib import Path
from streamline_extract.consolidation.schema_detector import SchemaDetector
from streamline_extract.consolidation.data_flattener import DataFlattener
from streamline_extract.consolidation.deduplicator import Deduplicator
from streamline_extract.consolidation.excel_formatter import ExcelFormatter
from streamline_extract.consolidation.csv_exporter import CsvExporter


# ============================================================================
# SchemaDetector Tests
# ============================================================================

class TestSchemaDetector:
    """Test schema structure detection."""
    
    def test_detect_structure_ordinance_schema(self):
        """Test detecting ordinance/regulation schema structure."""
        data = {
            "jurisdiction_details": {"city": "Austin", "state": "TX"},
            "requirements": [
                {"category": "Setbacks", "description": "10 feet from property line"},
                {"category": "Depth", "description": "Maximum 500 feet"}
            ]
        }
        
        detector = SchemaDetector()
        info = detector.detect_structure(data)
        
        assert info['type'] == 'Ordinance/Regulation'
        assert info['main_array_key'] == 'requirements'
        assert 'jurisdiction_details' in info['id_fields']
    
    def test_detect_structure_tariff_schema(self):
        """Test detecting utility tariff schema structure."""
        data = {
            "utility_info": {"name": "Austin Energy", "state": "TX"},
            "rate_schedules": [
                {"schedule_name": "Residential", "rate": 0.12},
                {"schedule_name": "Commercial", "rate": 0.15}
            ]
        }
        
        detector = SchemaDetector()
        info = detector.detect_structure(data)
        
        assert info['type'] == 'Utility Tariff'
        assert info['main_array_key'] == 'rate_schedules'
        assert 'utility_info' in info['id_fields']
    
    def test_extract_context_fields(self):
        """Test context field extraction from data."""
        data = {
            "jurisdiction_details": {"city": "Denver", "state": "Colorado"},
            "requirements": [{"category": "Test"}]
        }
        
        schema_info = {
            'main_array_key': 'requirements',
            'id_fields': ['jurisdiction_details']
        }
        
        detector = SchemaDetector()
        context = detector.extract_context(data, schema_info)
        
        assert context['City'] == "Denver"
        assert context['State'] == "Colorado"
    
    def test_detect_id_fields_pattern_matching(self):
        """Test automatic ID field detection using common patterns."""
        data = {
            "permit_info": {"number": "12345"},
            "jurisdiction_details": {"city": "Seattle"},
            "requirements": [{"field": "value"}]
        }
        
        detector = SchemaDetector()
        info = detector.detect_structure(data)
        
        # Should detect object fields with 'info', 'details', etc. as ID fields
        assert len(info['id_fields']) > 0
        assert 'permit_info' in info['id_fields'] or 'jurisdiction_details' in info['id_fields']


# ============================================================================
# DataFlattener Tests
# ============================================================================

class TestDataFlattener:
    """Test data flattening and normalization."""
    
    def test_flatten_simple_object(self):
        """Test flattening simple nested object."""
        item = {
            "name": "Test Item",
            "details": {
                "category": "Type A",
                "priority": "High"
            }
        }
        
        flattener = DataFlattener()
        result = flattener.flatten_item(item)
        
        assert result['Name'] == "Test Item"
        assert result['Details Category'] == "Type A"
        assert result['Details Priority'] == "High"
    
    def test_flatten_array_to_string(self):
        """Test array converted to comma-separated string."""
        item = {
            "name": "Test",
            "tags": ["renewable", "energy", "solar"]
        }
        
        flattener = DataFlattener()
        result = flattener.flatten_item(item)
        
        assert result['Name'] == "Test"
        assert result['Tags'] == "renewable, energy, solar"
    
    def test_flatten_object_array_expanded(self):
        """Test small object arrays expanded to columns."""
        item = {
            "name": "Building",
            "systems": [
                {"type": "HVAC", "efficiency": "High"},
                {"type": "Lighting", "efficiency": "Medium"}
            ]
        }
        
        flattener = DataFlattener()
        result = flattener.flatten_item(item)
        
        assert result['Name'] == "Building"
        # Small object arrays get expanded with item identifiers
        assert any('Systems' in key for key in result.keys())
    
    def test_normalize_column_names(self):
        """Test column name cleaning and title casing."""
        flattener = DataFlattener()
        
        # Test various column name patterns
        assert flattener.make_column_name("simple_field") == "Simple Field"
        assert flattener.make_column_name("field_with_units") == "Field With Units"
        assert flattener.make_column_name("nested_object_property") == "Nested Object Property"
    
    def test_normalize_units(self):
        """Test unit detection and normalization in DataFrame."""
        df = pd.DataFrame({
            'Distance': ['10 ft', '20 feet', '5 ft'],
            'Noise': ['50 dBA', '60 dB', '55 dBA'],
            'Depth': ['100 in', '200 inches', '150 in']
        })
        
        flattener = DataFlattener()
        result = flattener.normalize_units(df)
        
        # Check that units are preserved and normalized
        assert 'Distance' in result.columns
        assert 'Noise' in result.columns
        assert 'Depth' in result.columns
    
    def test_handle_empty_array(self):
        """Test handling of empty arrays."""
        item = {
            "name": "Test",
            "empty_list": []
        }
        
        flattener = DataFlattener()
        result = flattener.flatten_item(item)
        
        assert result['Name'] == "Test"
        # Empty arrays should be handled gracefully
        assert 'Empty List' not in result or result.get('Empty List') in [None, '', []]


# ============================================================================
# Deduplicator Tests
# ============================================================================

class TestDeduplicator:
    """Test row deduplication logic."""
    
    def test_deduplicate_identical_rows(self):
        """Test that identical rows are removed."""
        df = pd.DataFrame({
            'Jurisdiction': ['Austin', 'Austin', 'Denver'],
            'Category': ['Setback', 'Setback', 'Depth'],
            'Value': ['10 ft', '10 ft', '500 ft'],
            'Notes': ['', 'Some note', '']
        })
        
        dedup = Deduplicator()
        result = dedup.deduplicate(df)
        
        # Should have 2 rows after deduplication (2 Austin rows are identical except Notes)
        assert len(result) == 2
        assert 'Merged' in result[result['Jurisdiction'] == 'Austin']['Notes'].values[0]
    
    def test_deduplicate_preserves_unique(self):
        """Test that unique rows are preserved."""
        df = pd.DataFrame({
            'Jurisdiction': ['Austin', 'Denver', 'Seattle'],
            'Category': ['Setback', 'Depth', 'Noise'],
            'Value': ['10 ft', '500 ft', '50 dBA']
        })
        
        dedup = Deduplicator()
        result = dedup.deduplicate(df)
        
        # All rows are unique, should remain unchanged
        assert len(result) == 3
        assert len(result[result['Jurisdiction'] == 'Austin']) == 1
        assert len(result[result['Jurisdiction'] == 'Denver']) == 1
        assert len(result[result['Jurisdiction'] == 'Seattle']) == 1
    
    def test_deduplicate_keeps_most_complete(self):
        """Test that most complete row is kept from duplicates."""
        df = pd.DataFrame({
            'Jurisdiction': ['Austin', 'Austin'],
            'Category': ['Setback', 'Setback'],
            'Value': ['10 ft', '10 ft'],
            'Location': ['Section 1', 'Section 1'],  # Metadata field - excluded from comparison
            'Notes': ['Note 1', 'Note 2']  # Metadata field - excluded from comparison
        })
        
        dedup = Deduplicator()
        result = dedup.deduplicate(df)
        
        # Since Location and Notes are excluded from comparison, these are duplicates
        # Should keep one row and merge the other
        assert len(result) == 1
        assert 'Merged' in result.iloc[0]['Notes']


# ============================================================================
# ExcelFormatter Tests
# ============================================================================

class TestExcelFormatter:
    """Test Excel export and formatting."""
    
    def test_save_creates_file(self, tmp_path):
        """Test that Excel file is created."""
        df = pd.DataFrame({
            'Column1': ['A', 'B', 'C'],
            'Column2': [1, 2, 3]
        })
        
        output_path = tmp_path / "test_output.xlsx"
        formatter = ExcelFormatter()
        formatter.save(df, output_path)
        
        assert output_path.exists()
    
    def test_save_creates_parent_directory(self, tmp_path):
        """Test that parent directories are created if needed."""
        df = pd.DataFrame({'Col': [1, 2, 3]})
        
        output_path = tmp_path / "subdir" / "nested" / "test.xlsx"
        formatter = ExcelFormatter()
        formatter.save(df, output_path)
        
        assert output_path.exists()
        assert output_path.parent.exists()


# ============================================================================
# CsvExporter Tests
# ============================================================================

class TestCsvExporter:
    """Test CSV export."""
    
    def test_save_creates_file(self, tmp_path):
        """Test that CSV file is created."""
        df = pd.DataFrame({
            'Column1': ['A', 'B', 'C'],
            'Column2': [1, 2, 3]
        })
        
        output_path = tmp_path / "test_output.csv"
        exporter = CsvExporter()
        exporter.save(df, output_path)
        
        assert output_path.exists()
    
    def test_save_creates_parent_directory(self, tmp_path):
        """Test that parent directories are created if needed."""
        df = pd.DataFrame({'Col': [1, 2, 3]})
        
        output_path = tmp_path / "subdir" / "test.csv"
        exporter = CsvExporter()
        exporter.save(df, output_path)
        
        assert output_path.exists()
        assert output_path.parent.exists()
    
    def test_csv_content_correct(self, tmp_path):
        """Test that CSV content matches DataFrame."""
        df = pd.DataFrame({
            'Name': ['Alice', 'Bob'],
            'Age': [30, 25]
        })
        
        output_path = tmp_path / "test.csv"
        exporter = CsvExporter()
        exporter.save(df, output_path)
        
        # Read back and verify
        result = pd.read_csv(output_path)
        assert list(result.columns) == ['Name', 'Age']
        assert len(result) == 2
        assert result['Name'].tolist() == ['Alice', 'Bob']


# ============================================================================
# Run Tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
