#!/usr/bin/env python3
"""
Test semantic deduplication for approval redundancy fix.

Verifies that when both 'zoning_districts' and 'permit_approval' rows exist
for the same applies_to + obligation (e.g., both stating "required" for
compressor stations), the less authoritative 'zoning_districts' row is dropped
and only the more explicit 'permit_approval' row is kept.

Reference: Audit checkpoint for natural gas pipelines CUP duplication issue.
"""

import pandas as pd
import pytest
from pathlib import Path
import json
import tempfile

from psweep.compilation.deduplicator import Deduplicator
from psweep.utils.schema_metadata import SchemaMetadata


@pytest.fixture
def minimal_schema_path(tmp_path):
    """Create a minimal schema file for testing."""
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": {
            "domain": "test",
            "version": "2.0.0",
            "extraction": {
                "main_data_array": "requirements",
                "identifier_fields": ["state", "county"],
                "context_objects": [],
            },
            "identity": {
                "deduplication": {
                    "key_fields": [
                        "feature",
                        "applies_to",
                        "obligation",
                        "value",
                    ],
                    "ignore_fields": ["notes", "reasoning"],
                    "fuzzy_key_fields": [],
                    "partition_fields": ["state", "county"],
                    "cross_feature_redundancy": {
                        "group_by": ["applies_to", "obligation"],
                        "feature_field": "feature",
                        "authority_ranking": [
                            "permit_approval",
                            "location",
                            "equipment_standard",
                            "zoning_districts",
                        ],
                        "redundant_features": [
                            "permit_approval",
                            "zoning_districts",
                        ],
                    },
                }
            },
        },
        "type": "object",
        "properties": {
            "requirements": {
                "type": "array",
                "items": {"type": "object"},
            }
        },
    }
    schema_file = tmp_path / "test_schema.json"
    with open(schema_file, "w") as f:
        json.dump(schema, f)
    return schema_file


@pytest.fixture
def minimal_metadata(minimal_schema_path):
    """Minimal schema metadata for testing."""
    return SchemaMetadata(minimal_schema_path)


def test_approval_redundancy_zoning_districts_removed(minimal_metadata):
    """
    Test that zoning_districts row is dropped when permit_approval exists
    for same applies_to + obligation.

    Scenario:
    - Row 1: feature='zoning_districts', applies_to='Compressor station', obligation='required'
    - Row 2: feature='permit_approval', applies_to='Compressor station', obligation='required'

    Expected: Row 1 (zoning_districts) is removed; Row 2 (permit_approval) is kept.
    """
    # Create test DataFrame
    df = pd.DataFrame(
        [
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "zoning_districts",
                "applies_to": "Compressor station",
                "obligation": "required",
                "value": None,
                "rule_kind": "approval",
                "value_category": "qualitative",
                "summary": "Compressor stations allowed in commercial zones",
                "Notes": "",
            },
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "permit_approval",
                "applies_to": "Compressor station",
                "obligation": "required",
                "value": None,
                "rule_kind": "approval",
                "value_category": "qualitative",
                "summary": "Conditional use permit required",
                "Notes": "",
            },
        ]
    )

    dedup = Deduplicator(minimal_metadata)
    result = dedup.deduplicate(df)

    # Verify only permit_approval row remains
    assert len(result) == 1
    assert result.iloc[0]["feature"] == "permit_approval"
    assert result.iloc[0]["applies_to"] == "Compressor station"
    assert "Removed redundant" in result.iloc[0]["Notes"]
    print(f"✓ Test passed: zoning_districts removed, permit_approval kept")
    print(f"  Annotation: {result.iloc[0]['Notes']}")


def test_approval_redundancy_natural_gas_pipeline_scenario(minimal_metadata):
    """
    Test realistic natural gas pipeline scenario.

    A document lists "Compressor Stations (Conditional Use Permit)" in the
    zoning use table AND has a separate section stating "A conditional use
    permit is required for compressor stations."

    This should result in only ONE row being extracted (permit_approval),
    or if both are extracted, dedup should remove the zoning_districts one.
    """
    df = pd.DataFrame(
        [
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "zoning_districts",
                "applies_to": "Natural Gas Compressor Stations",
                "obligation": "required",
                "value": None,
                "applicable_values": "C1, M1, M2",
                "rule_kind": "approval",
                "value_category": "qualitative",
                "summary": "Compressor stations conditional in commercial/manufacturing zones",
                "section": "Sec 4.2",
                "Notes": "",
            },
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "permit_approval",
                "applies_to": "Natural Gas Compressor Stations",
                "obligation": "required",
                "value": None,
                "rule_kind": "approval",
                "value_category": "qualitative",
                "summary": "Conditional use permit required for compressor stations",
                "section": "Sec 5.3",
                "Notes": "",
            },
        ]
    )

    dedup = Deduplicator(minimal_metadata)
    result = dedup.deduplicate(df)

    # Should have only 1 row after dedup
    assert len(result) == 1
    kept_feature = result.iloc[0]["feature"]
    assert kept_feature == "permit_approval"
    print(f"✓ Natural gas pipeline scenario: kept '{kept_feature}' (removed zoning_districts)")


def test_approval_redundancy_different_obligations_not_merged(minimal_metadata):
    """
    Ensure rows with different obligations are NOT merged.

    Scenario:
    - Row 1: feature='zoning_districts', applies_to='Compressor', obligation='allowed'
    - Row 2: feature='permit_approval', applies_to='Compressor', obligation='required'

    Expected: Both rows kept (different obligations = different rules).
    """
    df = pd.DataFrame(
        [
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "zoning_districts",
                "applies_to": "Compressor station",
                "obligation": "allowed",
                "value": None,
                "rule_kind": "approval",
                "value_category": "qualitative",
                "summary": "Compressor allowed in M1",
                "Notes": "",
            },
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "permit_approval",
                "applies_to": "Compressor station",
                "obligation": "required",
                "value": None,
                "rule_kind": "approval",
                "value_category": "qualitative",
                "summary": "CUP required",
                "Notes": "",
            },
        ]
    )

    dedup = Deduplicator(minimal_metadata)
    result = dedup.deduplicate(df)

    # Both rows should be kept (different obligations)
    assert len(result) == 2
    obligations = set(result["obligation"])
    assert "allowed" in obligations and "required" in obligations
    print(f"✓ Different obligations preserved: {obligations}")


def test_approval_redundancy_different_applies_to_not_merged(minimal_metadata):
    """
    Ensure rows with different applies_to are NOT merged.

    Scenario:
    - Row 1: feature='zoning_districts', applies_to='Compressor station', obligation='required'
    - Row 2: feature='permit_approval', applies_to='Pipeline', obligation='required'

    Expected: Both rows kept (different facilities).
    """
    df = pd.DataFrame(
        [
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "zoning_districts",
                "applies_to": "Compressor station",
                "obligation": "required",
                "value": None,
                "rule_kind": "approval",
                "value_category": "qualitative",
                "summary": "Compressor conditional",
                "Notes": "",
            },
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "permit_approval",
                "applies_to": "Pipeline",
                "obligation": "required",
                "value": None,
                "rule_kind": "approval",
                "value_category": "qualitative",
                "summary": "Pipeline CUP required",
                "Notes": "",
            },
        ]
    )

    dedup = Deduplicator(minimal_metadata)
    result = dedup.deduplicate(df)

    # Both rows should be kept (different applies_to)
    assert len(result) == 2
    applies_tos = set(result["applies_to"])
    assert "Compressor station" in applies_tos and "Pipeline" in applies_tos
    print(f"✓ Different applies_to preserved: {applies_tos}")


def test_approval_redundancy_permits_other_features(minimal_metadata):
    """
    Test that semantic dedup only affects approval features.

    Scenario:
    - Row 1: feature='setback', applies_to='Compressor', obligation='required'
    - Row 2: feature='zoning_districts', applies_to='Compressor', obligation='required'

    Expected: Both rows kept (setback is not an approval-related feature).
    """
    df = pd.DataFrame(
        [
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "property lines distance",
                "applies_to": "Compressor station",
                "obligation": "required",
                "value": 100,
                "units": "feet",
                "rule_kind": "distance",
                "value_category": "quantitative",
                "summary": "100 foot setback required",
                "Notes": "",
            },
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "zoning_districts",
                "applies_to": "Compressor station",
                "obligation": "required",
                "value": None,
                "rule_kind": "approval",
                "value_category": "qualitative",
                "summary": "Conditional in M1",
                "Notes": "",
            },
        ]
    )

    dedup = Deduplicator(minimal_metadata)
    result = dedup.deduplicate(df)

    # Both rows should be kept (setback is not approval-related)
    assert len(result) == 2
    features = set(result["feature"])
    assert "property lines distance" in features and "zoning_districts" in features
    print(f"✓ Non-approval features unaffected: {features}")


def test_approval_redundancy_no_features_present(minimal_metadata):
    """
    Test with empty DataFrame.

    Expected: Returns empty DataFrame.
    """
    df = pd.DataFrame(columns=[
        "state", "county", "feature", "applies_to", "obligation",
        "value", "rule_kind", "value_category", "summary", "Notes"
    ])

    dedup = Deduplicator(minimal_metadata)
    result = dedup.deduplicate(df)

    assert len(result) == 0
    print(f"✓ Empty DataFrame handled correctly")


def test_cross_feature_redundancy_is_noop_without_schema_block(tmp_path):
    """When a schema omits cross_feature_redundancy, the pass does nothing.

    Guards domain-neutrality: the approval-redundancy collapse must be entirely
    opt-in, not hardcoded behavior applied to every domain.
    """
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$metadata": {
            "domain": "test_neutral",
            "version": "2.0.0",
            "extraction": {
                "main_data_array": "requirements",
                "identifier_fields": ["state", "county"],
                "context_objects": [],
            },
            "identity": {
                "deduplication": {
                    # Same key fields, but NO cross_feature_redundancy block.
                    "key_fields": ["feature", "applies_to", "obligation", "value"],
                    "ignore_fields": ["notes", "reasoning"],
                    "fuzzy_key_fields": [],
                    "partition_fields": ["state", "county"],
                }
            },
        },
        "type": "object",
        "properties": {"requirements": {"type": "array", "items": {"type": "object"}}},
    }
    schema_file = tmp_path / "neutral_schema.json"
    schema_file.write_text(json.dumps(schema), encoding="utf-8")

    df = pd.DataFrame(
        [
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "zoning_districts",
                "applies_to": "Compressor station",
                "obligation": "required",
                "value": None,
                "summary": "matrix row",
                "Notes": "",
            },
            {
                "state": "TX",
                "county": "Weatherford",
                "feature": "permit_approval",
                "applies_to": "Compressor station",
                "obligation": "required",
                "value": None,
                "summary": "CUP required",
                "Notes": "",
            },
        ]
    )

    dedup = Deduplicator(SchemaMetadata(schema_file))
    result = dedup.deduplicate(df)

    # Both rows survive — no cross-feature collapse without the schema block.
    assert len(result) == 2
    assert set(result["feature"]) == {"zoning_districts", "permit_approval"}


if __name__ == "__main__":
    # Run tests for quick validation
    pytest.main([__file__, "-v"])
