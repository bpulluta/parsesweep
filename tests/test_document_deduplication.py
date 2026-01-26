#!/usr/bin/env python3
"""
Comprehensive tests for document-level and row-level deduplication.

Tests various scenarios to ensure:
1. True duplicates are merged
2. Historical versions are preserved
3. Different ordinances are preserved
4. Amendments are preserved
5. Works across different document types (ordinances, tariffs, permits)
"""
import pandas as pd
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from streamline_extract.consolidation.document_deduplicator import DocumentDeduplicator
from streamline_extract.consolidation.deduplicator import Deduplicator
from streamline_extract.utils.schema_metadata import SchemaMetadata


class TestResults:
    """Track test results."""
    def __init__(self):
        self.passed = []
        self.failed = []
    
    def record(self, test_name, passed, expected, actual, message=""):
        if passed:
            self.passed.append(test_name)
            print(f"✓ {test_name}")
        else:
            self.failed.append(test_name)
            print(f"✗ {test_name}")
            print(f"  Expected: {expected}")
            print(f"  Actual: {actual}")
            if message:
                print(f"  {message}")
    
    def summary(self):
        total = len(self.passed) + len(self.failed)
        print(f"\n{'='*60}")
        print(f"Test Results: {len(self.passed)}/{total} passed")
        if self.failed:
            print(f"\nFailed tests:")
            for test in self.failed:
                print(f"  - {test}")
            return False
        return True


def create_mock_schema_metadata():
    """Create a minimal schema metadata for testing."""
    import tempfile
    import json
    
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": {
            "domain": "Test",
            "version": "1.0.0",
            "extraction": {
                "main_data_array": "items",
                "identifier_fields": ["jurisdiction.state", "jurisdiction.county"],
                "context_objects": ["jurisdiction"]
            },
            "consolidation": {
                "deduplication": {
                    "key_fields": ["category", "specific_subject", "value", "condition"],
                    "ignore_fields": ["notes", "section"]
                }
            }
        }
    }
    
    # Write to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(schema, f)
        temp_path = f.name
    
    return SchemaMetadata(temp_path)


def test_scenario_1_true_duplicates(results):
    """Test: Same document extracted twice with identical content - row-level dedup handles this."""
    print("\n" + "="*60)
    print("Scenario 1: True Duplicates (Same Document, Same Version)")
    print("="*60)
    
    data = {
        'State': ['CA', 'CA', 'CA', 'CA'],
        'County': ['Test County', 'Test County', 'Test County', 'Test County'],
        'Date_Adopted': ['2020-01-01', '2020-01-01', '2020-01-01', '2020-01-01'],
        'Section': ['5.1.2', '5.1.2', '5.1.2', '5.1.2'],
        'Ordinance_Code': ['', '', '', ''],  # Same (all empty)
        'Category': ['Setback', 'Height limit', 'Setback', 'Height limit'],
        'Specific Subject': ['from property line', 'maximum', 'from property line', 'maximum'],
        'Value': ['100', '50', '100', '50'],
        'Condition': ['', '', '', ''],
        'Notes': ['', '', '', '']
    }
    df = pd.DataFrame(data)
    
    schema_metadata = create_mock_schema_metadata()
    doc_dedup = DocumentDeduplicator(schema_metadata)
    row_dedup = Deduplicator(schema_metadata)
    
    # Document dedup (won't remove anything - single version)
    df_after_doc = doc_dedup.deduplicate_documents(df)
    
    # Row dedup (should merge duplicate rows)
    result_df = row_dedup.deduplicate(df_after_doc)
    
    initial_rows = len(df)
    final_rows = len(result_df)
    
    # Expect: Document dedup finds 1 version (no doc-level duplicates)
    # Row dedup merges duplicate rows: 4 -> 2
    expected_rows = 2
    passed = final_rows == expected_rows
    results.record(
        "Scenario 1: Row-level dedup works",
        passed,
        f"{expected_rows} rows (after row dedup)",
        f"{final_rows} rows",
        f"Single document version, row-level dedup merged {initial_rows - final_rows} rows"
    )
    
    return result_df


def test_scenario_2_amendment_versions(results):
    """Test: Same ordinance but different amendment dates - SHOULD NOT merge."""
    print("\n" + "="*60)
    print("Scenario 2: Historical Amendments (Different Amendment Dates)")
    print("="*60)
    
    data = {
        'State': ['NY', 'NY', 'NY', 'NY'],
        'County': ['Test County', 'Test County', 'Test County', 'Test County'],
        'Date_Adopted': ['2015-01-01', '2015-01-01', '2015-01-01', '2015-01-01'],
        'Date_Last_Amended': ['', '', '2020-06-15', '2020-06-15'],  # Amendment!
        'Section': ['3.2.1', '3.2.1', '3.2.1', '3.2.1'],
        'Ordinance_Code': ['Title 3', 'Title 3', 'Title 3', 'Title 3'],
        'Category': ['Noise limit', 'Noise limit', 'Noise limit', 'Noise limit'],
        'Specific Subject': ['daytime', 'daytime', 'daytime', 'daytime'],
        'Value': ['65', '65', '60', '60'],  # Value changed in amendment
        'Notes': ['', '', '', '']
    }
    df = pd.DataFrame(data)
    
    schema_metadata = create_mock_schema_metadata()
    doc_dedup = DocumentDeduplicator(schema_metadata)
    
    initial_rows = len(df)
    result_df = doc_dedup.deduplicate_documents(df)
    final_rows = len(result_df)
    
    # Expect: All 4 rows preserved (different amendment dates = different versions)
    expected_rows = 4
    passed = final_rows == expected_rows
    results.record(
        "Scenario 2: Amendments preserved",
        passed,
        f"{expected_rows} rows (all preserved)",
        f"{final_rows} rows",
        "Different Date_Last_Amended should prevent merging"
    )
    
    return result_df


def test_scenario_3_different_values(results):
    """Test: Same requirement but different values - SHOULD NOT merge."""
    print("\n" + "="*60)
    print("Scenario 3: Different Requirement Values")
    print("="*60)
    
    data = {
        'State': ['TX', 'TX', 'TX', 'TX'],
        'County': ['Test County', 'Test County', 'Test County', 'Test County'],
        'Date_Adopted': ['2018-01-01', '2018-01-01', '2018-01-01', '2018-01-01'],
        'Section': ['2.5', '2.5', '2.5', '2.5'],
        'Ordinance_Code': ['Code A', 'Code A', 'Code B', 'Code B'],  # Different codes
        'Category': ['Setback', 'Setback', 'Setback', 'Setback'],
        'Specific Subject': ['from road', 'from road', 'from road', 'from road'],
        'Value': ['50', '50', '100', '100'],  # Different values!
        'Notes': ['', '', '', '']
    }
    df = pd.DataFrame(data)
    
    schema_metadata = create_mock_schema_metadata()
    doc_dedup = DocumentDeduplicator(schema_metadata)
    
    initial_rows = len(df)
    result_df = doc_dedup.deduplicate_documents(df)
    final_rows = len(result_df)
    
    # Expect: All 4 rows preserved (different values = substantive difference)
    expected_rows = 4
    passed = final_rows == expected_rows
    results.record(
        "Scenario 3: Different values preserved",
        passed,
        f"{expected_rows} rows (all preserved)",
        f"{final_rows} rows",
        "Same category but different values should prevent merging"
    )
    
    return result_df


def test_scenario_4_supersedes_relationship(results):
    """Test: Documents with supersedes relationship - SHOULD NOT merge."""
    print("\n" + "="*60)
    print("Scenario 4: Supersedes Relationship")
    print("="*60)
    
    data = {
        'State': ['FL', 'FL', 'FL', 'FL'],
        'County': ['Test County', 'Test County', 'Test County', 'Test County'],
        'Date_Adopted': ['2019-01-01', '2019-01-01', '2019-01-01', '2019-01-01'],
        'Supersedes': ['', '', 'Ordinance 2015-10', 'Ordinance 2015-10'],  # Lineage!
        'Section': ['4.1', '4.1', '4.1', '4.1'],
        'Ordinance_Code': ['Ord 2019-5', 'Ord 2019-5', 'Ord 2019-5', 'Ord 2019-5'],
        'Category': ['Permit fee', 'Permit fee', 'Permit fee', 'Permit fee'],
        'Specific Subject': ['application', 'application', 'application', 'application'],
        'Value': ['500', '500', '500', '500'],
        'Notes': ['', '', '', '']
    }
    df = pd.DataFrame(data)
    
    schema_metadata = create_mock_schema_metadata()
    doc_dedup = DocumentDeduplicator(schema_metadata)
    
    initial_rows = len(df)
    result_df = doc_dedup.deduplicate_documents(df)
    final_rows = len(result_df)
    
    # Expect: All 4 rows preserved (different supersedes = different lineage)
    expected_rows = 4
    passed = final_rows == expected_rows
    results.record(
        "Scenario 4: Supersedes relationship preserved",
        passed,
        f"{expected_rows} rows (all preserved)",
        f"{final_rows} rows",
        "Different Supersedes values should prevent merging"
    )
    
    return result_df


def test_scenario_5_different_jurisdictions(results):
    """Test: Different jurisdictions - SHOULD NOT merge."""
    print("\n" + "="*60)
    print("Scenario 5: Different Jurisdictions")
    print("="*60)
    
    data = {
        'State': ['CA', 'CA', 'NY', 'NY'],
        'County': ['County A', 'County A', 'County B', 'County B'],
        'Date_Adopted': ['2020-01-01', '2020-01-01', '2020-01-01', '2020-01-01'],
        'Section': ['1.1', '1.1', '1.1', '1.1'],
        'Ordinance_Code': ['', '', '', ''],
        'Category': ['Height limit', 'Height limit', 'Height limit', 'Height limit'],
        'Specific Subject': ['maximum', 'maximum', 'maximum', 'maximum'],
        'Value': ['100', '100', '100', '100'],
        'Condition': ['', '', '', ''],
        'Notes': ['', '', '', '']
    }
    df = pd.DataFrame(data)
    
    schema_metadata = create_mock_schema_metadata()
    doc_dedup = DocumentDeduplicator(schema_metadata)
    row_dedup = Deduplicator(schema_metadata)
    
    # Document dedup
    df_after_doc = doc_dedup.deduplicate_documents(df)
    
    # Row dedup
    result_df = row_dedup.deduplicate(df_after_doc)
    
    initial_rows = len(df)
    final_rows = len(result_df)
    
    # Expect: Different jurisdictions group separately
    # CA County A: 2 rows -> 1 row (row dedup)
    # NY County B: 2 rows -> 1 row (row dedup)
    # Total: 2 rows
    expected_rows = 2
    passed = final_rows == expected_rows
    results.record(
        "Scenario 5: Jurisdictions handled correctly",
        passed,
        f"{expected_rows} rows (1 per jurisdiction after row dedup)",
        f"{final_rows} rows",
        "Different jurisdictions should group separately, row dedup within each"
    )
    
    return result_df


def test_scenario_6_cross_document_types(results):
    """Test: Different document types (tariff, ordinance, permit) - verify logic works."""
    print("\n" + "="*60)
    print("Scenario 6: Cross-Document Types (Tariff, Ordinance, Permit)")
    print("="*60)
    
    # Tariff data (different field names but same dedup logic)
    data = {
        'State': ['CA', 'CA', 'CA', 'CA'],
        'Utility': ['Test Electric', 'Test Electric', 'Test Electric', 'Test Electric'],
        'Date_Adopted': ['2021-01-01', '2021-01-01', '2021-01-01', '2021-01-01'],
        'Section': ['Schedule A', 'Schedule A', 'Schedule A', 'Schedule A'],
        'Ordinance_Code': ['', '', '', ''],  # Not really ordinance but testing field
        'Category': ['Rate', 'Rate', 'Rate', 'Rate'],
        'Specific Subject': ['residential', 'residential', 'residential', 'residential'],
        'Value': ['0.12', '0.12', '0.12', '0.12'],
        'Notes': ['', '', '', '']
    }
    df = pd.DataFrame(data)
    
    # Note: Using Utility instead of County, but logic should still work
    schema_metadata = create_mock_schema_metadata()
    doc_dedup = DocumentDeduplicator(schema_metadata)
    
    initial_rows = len(df)
    result_df = doc_dedup.deduplicate_documents(df)
    final_rows = len(result_df)
    
    # Expect: Without County field, doc dedup may not activate, but should not crash
    # At minimum, should return data unchanged if can't deduplicate
    passed = final_rows >= 1 and final_rows <= initial_rows
    results.record(
        "Scenario 6: Cross-document types handled",
        passed,
        f"1-{initial_rows} rows (graceful handling)",
        f"{final_rows} rows",
        "Should handle non-ordinance document types without crashing"
    )
    
    return result_df


def test_scenario_7_row_level_fuzzy_dedup(results):
    """Test: Row-level fuzzy deduplication with empty/filled Condition fields."""
    print("\n" + "="*60)
    print("Scenario 7: Row-Level Fuzzy Deduplication")
    print("="*60)
    
    data = {
        'State': ['WA', 'WA', 'WA', 'WA'],
        'County': ['Test County', 'Test County', 'Test County', 'Test County'],
        'Date_Adopted': ['2022-01-01', '2022-01-01', '2022-01-01', '2022-01-01'],
        'Section': ['7.3', '7.3', '7.3', '7.3'],
        'Ordinance_Code': ['Code 1', 'Code 1', 'Code 1', 'Code 1'],
        'Category': ['Setback', 'Setback', 'Noise limit', 'Noise limit'],
        'Specific Subject': ['from road', 'from road', 'nighttime', 'nighttime'],
        'Value': ['75', '75', '50', '50'],
        'Condition': ['', 'unless approved', '', 'measured at boundary'],  # Fuzzy!
        'Notes': ['', '', '', '']
    }
    df = pd.DataFrame(data)
    
    schema_metadata = create_mock_schema_metadata()
    
    # First do document dedup (should not remove anything - only 1 version)
    doc_dedup = DocumentDeduplicator(schema_metadata)
    df_after_doc = doc_dedup.deduplicate_documents(df)
    
    # Then do row-level dedup (should merge fuzzy duplicates)
    row_dedup = Deduplicator(schema_metadata)
    result_df = row_dedup.deduplicate(df_after_doc)
    
    initial_rows = len(df)
    final_rows = len(result_df)
    
    # Expect: 2 rows removed (fuzzy matches on Condition field)
    # Setback rows: '' matches 'unless approved' -> merge to 1
    # Noise rows: '' matches 'measured at boundary' -> merge to 1
    expected_rows = 2
    passed = final_rows == expected_rows
    results.record(
        "Scenario 7: Fuzzy dedup on Condition field",
        passed,
        f"{expected_rows} rows",
        f"{final_rows} rows",
        "Empty Condition should match filled Condition (fuzzy)"
    )
    
    return result_df


def test_scenario_8_conservative_section_formats(results):
    """Test: Different section formats preserved (conservative approach)."""
    print("\n" + "="*60)
    print("Scenario 8: Conservative Section Format Handling")
    print("="*60)
    
    data = {
        'State': ['UT', 'UT', 'UT', 'UT'],
        'County': ['Test County', 'Test County', 'Test County', 'Test County'],
        'Date_Adopted': ['2020-01-01', '2020-01-01', '2020-01-01', '2020-01-01'],
        'Section': ['17.35.030', '17.35.030', '§ 17.35.030', '§ 17.35.030'],  # Different format!
        'Ordinance_Code': ['', '', 'Title 17', 'Title 17'],
        'Category': ['Setback', 'Setback', 'Setback', 'Setback'],
        'Specific Subject': ['from road', 'from road', 'from road', 'from road'],
        'Value': ['100', '100', '100', '100'],
        'Notes': ['', '', '', '']
    }
    df = pd.DataFrame(data)
    
    schema_metadata = create_mock_schema_metadata()
    doc_dedup = DocumentDeduplicator(schema_metadata)
    
    initial_rows = len(df)
    result_df = doc_dedup.deduplicate_documents(df)
    final_rows = len(result_df)
    
    # Expect: Conservative approach - different section formats = different versions
    # Should preserve both versions (2 rows from each version)
    # But within each version, duplicates should merge (2 -> 1)
    expected_rows = 2  # One from each version
    passed = final_rows == expected_rows
    results.record(
        "Scenario 8: Conservative section format handling",
        passed,
        f"{expected_rows} rows (different formats preserved as separate versions)",
        f"{final_rows} rows",
        "Different section formats treated conservatively"
    )
    
    return result_df


def main():
    """Run all test scenarios."""
    print("\n" + "="*60)
    print("COMPREHENSIVE DEDUPLICATION TEST SUITE")
    print("="*60)
    
    results = TestResults()
    
    try:
        # Run all test scenarios
        test_scenario_1_true_duplicates(results)
        test_scenario_2_amendment_versions(results)
        test_scenario_3_different_values(results)
        test_scenario_4_supersedes_relationship(results)
        test_scenario_5_different_jurisdictions(results)
        test_scenario_6_cross_document_types(results)
        test_scenario_7_row_level_fuzzy_dedup(results)
        test_scenario_8_conservative_section_formats(results)
        
        # Print summary
        success = results.summary()
        
        if success:
            print("\n✓ All tests passed! Deduplication logic is robust.")
            return 0
        else:
            print("\n✗ Some tests failed. Review logic.")
            return 1
            
    except Exception as e:
        print(f"\n✗ Test suite crashed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())
