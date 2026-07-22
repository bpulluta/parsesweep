"""
Tests for QA/QC Comparison Engine - Simplified Numeric-Only Version.

Tests the ComparisonEngine class which compares NUMERIC outputs only
from multiple AI models to identify discrepancies.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
import tempfile
import shutil

from psweep.qa_qc.comparison_engine import (
    ComparisonEngine,
    ComparisonResult,
    FieldComparison,
)
from psweep.qa_qc.utils import resolve_qaqc_runtime_config


class TestFieldComparison:
    """Tests for FieldComparison dataclass."""

    def test_basic_creation(self):
        """Test creating a FieldComparison."""
        fc = FieldComparison(
            item_id="test_item",
            field_path="value",
            model_values={"model_a": 100, "model_b": 100},
            agreement_score="2/2",
            needs_review=False,
            notes=""
        )
        assert fc.item_id == "test_item"
        assert fc.agreement_score == "2/2"
        assert fc.needs_review is False

    def test_disagreement(self):
        """Test FieldComparison with disagreement."""
        fc = FieldComparison(
            item_id="test_item",
            field_path="value",
            model_values={"model_a": 100, "model_b": 200, "model_c": 100},
            agreement_score="2/3",
            needs_review=True,
            notes="2 different values"
        )
        assert fc.needs_review is True
        assert "different values" in fc.notes


class TestComparisonResult:
    """Tests for ComparisonResult dataclass."""

    def test_basic_creation(self):
        """Test creating a ComparisonResult."""
        result = ComparisonResult(
            document_name="test_doc",
            models=["model_a", "model_b"],
            summary={"total_comparisons": 10},
            context_comparisons=[],
            item_comparisons=[]
        )
        assert result.document_name == "test_doc"
        assert len(result.models) == 2
        assert result.context_comparisons == []
        assert result.item_comparisons == []


class TestComparisonEngine:
    """Tests for ComparisonEngine class - Numeric Only comparison."""

    @pytest.fixture
    def mock_schema_metadata(self):
        """Create a mock SchemaMetadata with minimal needed methods."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["jurisdiction.state"]
        mock.get_deduplication_key_fields.return_value = [
            "category", "specific_subject", "section"
        ]
        mock.get_context_objects.return_value = ["jurisdiction"]
        # QA/QC config - match on category + specific_subject, compare value + unit
        mock.get_qa_qc_match_fields.return_value = ["category", "specific_subject"]
        mock.get_qa_qc_compare_fields.return_value = ["value", "unit"]
        return mock

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for test files."""
        tmp = tempfile.mkdtemp()
        yield Path(tmp)
        shutil.rmtree(tmp)

    def test_init_with_schema_metadata(self, mock_schema_metadata):
        """Test engine initialization."""
        engine = ComparisonEngine(mock_schema_metadata)
        assert engine.main_data_array == "requirements"
        # match_fields comes from qa_qc config
        assert "category" in engine.match_fields
        assert "specific_subject" in engine.match_fields
        # compare_fields comes from qa_qc config  
        assert "value" in engine.compare_fields
        assert "unit" in engine.compare_fields

    def test_schema_driven_match_fields(self, mock_schema_metadata):
        """Test that match fields come from schema qa_qc config."""
        mock_schema_metadata.get_qa_qc_match_fields.return_value = ["category", "applies_to"]
        engine = ComparisonEngine(mock_schema_metadata)
        
        # Match fields should come directly from schema
        assert engine.match_fields == ["category", "applies_to"]

    def test_schema_driven_compare_fields(self):
        """Test that compare fields come from schema qa_qc config."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "items"
        mock.get_identifier_fields.return_value = []
        mock.get_deduplication_key_fields.return_value = []
        mock.get_context_objects.return_value = []
        mock.get_qa_qc_match_fields.return_value = ["name"]
        mock.get_qa_qc_compare_fields.return_value = ["price", "quantity"]
        
        engine = ComparisonEngine(mock)
        # Compare fields come from schema
        assert "price" in engine.compare_fields
        assert "quantity" in engine.compare_fields

    def test_runtime_qaqc_config_overrides_schema_metadata(self):
        """Runtime QA/QC lane config should replace schema-level compare config when provided."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "items"
        mock.get_identifier_fields.return_value = []
        mock.get_context_objects.return_value = []
        mock.get_qa_qc_match_fields.side_effect = AssertionError("schema QA/QC config should not be used")
        mock.get_qa_qc_compare_fields.side_effect = AssertionError("schema QA/QC config should not be used")

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "quantitative",
                "mode": "quantitative",
                "comparison_approach": "numeric_only",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["value", "unit"],
            },
        )

        assert engine.match_fields == ["category", "facility_type", "specific_subject"]
        assert engine.compare_fields == {"value", "unit"}
        assert engine.config_source == "runtime_artifact"
        assert engine.lane_name == "quantitative"

    def test_resolve_qaqc_runtime_config_uses_runtime_artifact_lane(self):
        """Runtime QA/QC config resolution should prefer the compiled pack lane."""
        mock = MagicMock()
        mock.get_qa_qc_match_fields.side_effect = AssertionError("schema fallback should not be used")
        mock.get_qa_qc_compare_fields.side_effect = AssertionError("schema fallback should not be used")

        config = resolve_qaqc_runtime_config(
            mock,
            runtime_artifact={
                "resolved": {
                    "pack": {
                        "qaqc": {
                            "default_lane": "quantitative",
                            "lanes": {
                                "quantitative": {
                                    "enabled": True,
                                    "mode": "quantitative",
                                    "comparison_approach": "numeric_only",
                                    "record_matching": {"key_fields": ["referenceNumber", "make", "model"]},
                                    "comparison": {"primary_fields": ["ratedCapacityKW", "operatingHoursPerUnitLimit"]},
                                }
                            },
                        }
                    }
                }
            },
        )

        assert config["source"] == "runtime_artifact"
        assert config["lane_name"] == "quantitative"
        assert config["match_fields"] == ["referenceNumber", "make", "model"]
        assert config["compare_fields"] == ["ratedCapacityKW", "operatingHoursPerUnitLimit"]
        assert config["projection"] is None

    def test_resolve_qaqc_runtime_config_includes_projection(self):
        """Runtime QA/QC config should preserve projection metadata when configured."""
        mock = MagicMock()
        mock.get_qa_qc_match_fields.side_effect = AssertionError("schema fallback should not be used")
        mock.get_qa_qc_compare_fields.side_effect = AssertionError("schema fallback should not be used")

        config = resolve_qaqc_runtime_config(
            mock,
            runtime_artifact={
                "resolved": {
                    "pack": {
                        "qaqc": {
                            "default_lane": "quantitative",
                            "lanes": {
                                "quantitative": {
                                    "enabled": True,
                                    "mode": "quantitative",
                                    "comparison_approach": "numeric_only",
                                    "projection": {
                                        "type": "nested_array_items",
                                        "source_array": "rate_schedules",
                                        "nested_array": "charges",
                                        "parent_fields": ["rate_name"],
                                    },
                                    "record_matching": {"key_fields": ["rate_name", "charge_type"]},
                                    "comparison": {"primary_fields": ["rate", "unit"]},
                                }
                            },
                        }
                    }
                }
            },
        )

        assert config["projection"] == {
            "type": "nested_array_items",
            "source_array": "rate_schedules",
            "nested_array": "charges",
            "parent_fields": ["rate_name"],
        }

    def test_resolve_qaqc_runtime_config_can_select_disabled_qualitative_lane(self):
        """Explicit lane selection should allow evaluating a disabled qualitative lane."""
        mock = MagicMock()
        mock.get_qa_qc_match_fields.side_effect = AssertionError("schema fallback should not be used")
        mock.get_qa_qc_compare_fields.side_effect = AssertionError("schema fallback should not be used")

        config = resolve_qaqc_runtime_config(
            mock,
            runtime_artifact={
                "resolved": {
                    "pack": {
                        "qaqc": {
                            "default_lane": "quantitative",
                            "lanes": {
                                "quantitative": {
                                    "enabled": True,
                                    "mode": "quantitative",
                                    "comparison_approach": "numeric_only",
                                    "record_matching": {"key_fields": ["category"]},
                                    "comparison": {"primary_fields": ["value"]},
                                },
                                "qualitative": {
                                    "enabled": False,
                                    "mode": "qualitative",
                                    "comparison_approach": "text_review",
                                    "record_matching": {"key_fields": ["category"]},
                                    "comparison": {"primary_fields": ["details"]},
                                },
                            },
                        }
                    }
                }
            },
            preferred_lane="qualitative",
        )

        assert config["source"] == "runtime_artifact"
        assert config["lane_name"] == "qualitative"
        assert config["mode"] == "qualitative"
        assert config["comparison_approach"] == "text_review"
        assert config["match_fields"] == ["category"]
        assert config["compare_fields"] == ["details"]

    def test_compare_outputs_text_review_compares_configured_text_fields(self, temp_dir):
        """Qualitative text-review lanes should compare configured text fields instead of skipping them."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = []
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        model_a = {
            "requirements": [
                {
                    "category": "Permit required",
                    "specific_subject": "Commercial Use",
                    "details": "Permit required before operations begin",
                }
            ]
        }
        model_b = {
            "requirements": [
                {
                    "category": "Permit required",
                    "specific_subject": "Commercial Use",
                    "details": "Permit required before drilling begins",
                }
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Doc",
        )

        details_comparison = next(
            (fc for fc in result.item_comparisons if fc.field_path == "details"),
            None,
        )
        assert details_comparison is not None
        assert details_comparison.agreement_score == "1/2"
        assert details_comparison.needs_review is True
        assert result.summary["comparison_approach"] == "text_review"
        assert result.summary["qaqc_lane"] == "qualitative"
        assert result.summary["review_category_counts"] == {"text_difference": 1}
        breakdown = result.summary["qualitative_mismatch_breakdown"]
        assert breakdown["text_difference_by_category"] == [{"label": "permit required", "count": 1}]
        assert breakdown["top_text_difference_requirements"] == [
            {"label": "permit required | commercial use", "count": 1}
        ]

        gate = result.summary["qualitative_advisory_gate"]
        assert gate["mode"] == "advisory"
        assert gate["status"] == "fail"
        assert gate["aligned_pct"] == 0.0
        assert gate["missing_item_pct"] == 0.0

    def test_qualitative_advisory_gate_passes_for_mostly_aligned_results(self, temp_dir):
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "specific_subject"]
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        model_a = {
            "requirements": [
                {"category": "Permit", "specific_subject": "A", "details": "same"},
                {"category": "Permit", "specific_subject": "B", "details": "same"},
                {"category": "Permit", "specific_subject": "C", "details": "same"},
                {"category": "Permit", "specific_subject": "D", "details": "same"},
                {"category": "Permit", "specific_subject": "E", "details": "different a"},
            ]
        }
        model_b = {
            "requirements": [
                {"category": "Permit", "specific_subject": "A", "details": "same"},
                {"category": "Permit", "specific_subject": "B", "details": "same"},
                {"category": "Permit", "specific_subject": "C", "details": "same"},
                {"category": "Permit", "specific_subject": "D", "details": "same"},
                {"category": "Permit", "specific_subject": "E", "details": "different b"},
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Pass Doc",
        )

        gate = result.summary["qualitative_advisory_gate"]
        assert result.summary["review_category_counts"] == {"aligned": 4, "text_difference": 1}
        assert gate["status"] == "pass"
        assert gate["aligned_pct"] == 80.0

    def test_qualitative_advisory_gate_warns_for_moderate_divergence(self, temp_dir):
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "specific_subject"]
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        model_a = {
            "requirements": [
                {"category": "Permit", "specific_subject": "A", "details": "same"},
                {"category": "Permit", "specific_subject": "B", "details": "same"},
                {"category": "Permit", "specific_subject": "C", "details": "different a"},
                {"category": "Permit", "specific_subject": "D", "details": "different a"},
            ]
        }
        model_b = {
            "requirements": [
                {"category": "Permit", "specific_subject": "A", "details": "same"},
                {"category": "Permit", "specific_subject": "B", "details": "same"},
                {"category": "Permit", "specific_subject": "C", "details": "different b"},
                {"category": "Permit", "specific_subject": "D", "details": "different b"},
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Warn Doc",
        )

        gate = result.summary["qualitative_advisory_gate"]
        assert result.summary["review_category_counts"] == {"aligned": 2, "text_difference": 2}
        assert gate["status"] == "warn"
        assert gate["aligned_pct"] == 50.0

    def test_qualitative_advisory_gate_fails_for_missing_item_heavy_results(self, temp_dir):
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "specific_subject"]
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        model_a = {
            "requirements": [
                {"category": "Permit", "specific_subject": "A", "details": "same"},
                {"category": "Permit", "specific_subject": "B", "details": "same"},
                {"category": "Permit", "specific_subject": "C", "details": "same"},
                {"category": "Permit", "specific_subject": "D", "details": "same"},
            ]
        }
        model_b = {
            "requirements": [
                {"category": "Permit", "specific_subject": "A", "details": "same"},
                {"category": "Permit", "specific_subject": "B", "details": "same"},
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Missing Doc",
        )

        gate = result.summary["qualitative_advisory_gate"]
        assert result.summary["review_category_counts"] == {"aligned": 2, "missing_item": 2}
        breakdown = result.summary["qualitative_mismatch_breakdown"]
        assert breakdown["missing_item_by_category"] == [{"label": "permit", "count": 2}]
        assert breakdown["top_missing_requirements"] == [
            {"label": "permit | c", "count": 1},
            {"label": "permit | d", "count": 1},
        ]
        assert gate["status"] == "fail"
        assert gate["aligned_pct"] == 50.0
        assert gate["missing_item_pct"] == 50.0

    def test_qualitative_scope_variants_are_excluded_from_gate_math(self, temp_dir):
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "facility_type", "specific_subject"]
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        model_a = {
            "requirements": [
                {"category": "Permit", "facility_type": "All facilities", "specific_subject": "A", "details": "same"},
                {"category": "Permit", "facility_type": "All facilities", "specific_subject": "B", "details": "same"},
                {"category": "Permit", "facility_type": "All facilities", "specific_subject": "C", "details": "same"},
                {"category": "Permit", "facility_type": "All facilities", "specific_subject": "D", "details": "same"},
                {"category": "Other", "facility_type": "Pipeline", "specific_subject": "pipeline siting and configuration", "details": "Use shared rights-of-way where feasible."},
            ]
        }
        model_b = {
            "requirements": [
                {"category": "Permit", "facility_type": "All facilities", "specific_subject": "A", "details": "same"},
                {"category": "Permit", "facility_type": "All facilities", "specific_subject": "B", "details": "same"},
                {"category": "Permit", "facility_type": "All facilities", "specific_subject": "C", "details": "same"},
                {"category": "Permit", "facility_type": "All facilities", "specific_subject": "D", "details": "same"},
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Scope Variant Doc",
        )

        assert result.summary["review_category_counts"] == {"aligned": 4, "scope_variant": 1}
        breakdown = result.summary["qualitative_mismatch_breakdown"]
        assert breakdown["scope_variant_by_category"] == [{"label": "other", "count": 1}]
        assert breakdown["top_scope_variant_requirements"] == [
            {"label": "other | pipeline | pipeline siting and configuration", "count": 1}
        ]

        gate = result.summary["qualitative_advisory_gate"]
        assert gate["status"] == "pass"
        assert gate["aligned_pct"] == 100.0
        assert gate["missing_item_pct"] == 0.0
        assert gate["evaluated_comparisons"] == 4
        assert gate["excluded_scope_variants"] == 1

    def test_text_review_fallback_matches_identical_details_despite_key_differences(self, temp_dir):
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "facility_type", "specific_subject"]
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        shared_text = "Lights should be directed or shielded to confine direct rays to the Project site."
        model_a = {
            "requirements": [
                {
                    "category": "Lighting requirement",
                    "facility_type": "All facilities",
                    "specific_subject": None,
                    "details": shared_text,
                }
            ]
        }
        model_b = {
            "requirements": [
                {
                    "category": "Lighting requirement",
                    "facility_type": "Exploration/drilling",
                    "specific_subject": None,
                    "details": shared_text,
                }
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Fallback Doc",
        )

        details_comparison = next(
            (fc for fc in result.item_comparisons if fc.field_path == "details"),
            None,
        )
        assert details_comparison is not None
        assert details_comparison.agreement_score == "2/2"
        assert details_comparison.needs_review is False
        assert result.summary["review_category_counts"] == {"aligned": 1}

        gate = result.summary["qualitative_advisory_gate"]
        assert gate["status"] == "pass"
        assert gate["missing_item_pct"] == 0.0

    def test_text_review_collapses_same_model_duplicates_before_fallback_matching(self, temp_dir):
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "facility_type", "specific_subject"]
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        shared_text = "Shrubs, trees, and ground cover shall be planted and maintained."
        model_a = {
            "requirements": [
                {
                    "category": "Visual impact assessment",
                    "facility_type": "All facilities",
                    "specific_subject": None,
                    "details": shared_text,
                },
                {
                    "category": "Visual impact assessment",
                    "facility_type": "Power plant",
                    "specific_subject": None,
                    "details": shared_text,
                },
            ]
        }
        model_b = {
            "requirements": [
                {
                    "category": "Visual impact assessment",
                    "facility_type": "All facilities",
                    "specific_subject": "landscaping",
                    "details": shared_text,
                }
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Duplicate Collapse Doc",
        )

        details_comparisons = [
            fc for fc in result.item_comparisons if fc.field_path == "details"
        ]
        assert len(details_comparisons) == 1
        assert details_comparisons[0].agreement_score == "2/2"
        assert details_comparisons[0].needs_review is False
        assert result.summary["review_category_counts"] == {"aligned": 1}

    def test_text_review_absorbs_unique_subsumed_missing_item(self, temp_dir):
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "facility_type", "specific_subject"]
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        model_a = {
            "requirements": [
                {
                    "category": "Setback",
                    "facility_type": "Production/injection well",
                    "specific_subject": "from structures/residential zones",
                    "details": "Residence 300 feet. Any other permanent structure/development 300 feet.",
                }
            ]
        }
        model_b = {
            "requirements": [
                {
                    "category": "Setback",
                    "facility_type": "Production/injection well",
                    "specific_subject": "from structures/residential zones",
                    "details": "Residence 300 feet.",
                },
                {
                    "category": "Setback",
                    "facility_type": "Production/injection well",
                    "specific_subject": "from any other permanent structure/development",
                    "details": "Any other permanent structure/development 300 feet.",
                },
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Subsumed Item Doc",
        )

        assert result.summary["review_category_counts"] == {"text_difference": 1}
        assert result.summary["qualitative_mismatch_breakdown"]["missing_item_by_category"] == []

    def test_text_review_merges_duplicate_keys_within_same_model(self, temp_dir):
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "facility_type", "specific_subject"]
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        model_a = {
            "requirements": [
                {
                    "category": "Noise limit",
                    "facility_type": "Exploration/drilling",
                    "specific_subject": "drilling operations",
                    "details": "Each operator shall limit drilling noise to CNEL 65 dB(A).",
                },
                {
                    "category": "Noise limit",
                    "facility_type": "Exploration/drilling",
                    "specific_subject": "drilling operations",
                    "details": "Impulse noises such as sudden steam venting shall be controlled by mufflers.",
                },
            ]
        }
        model_b = {
            "requirements": [
                {
                    "category": "Noise limit",
                    "facility_type": "Exploration/drilling",
                    "specific_subject": "drilling operations",
                    "details": "Each operator shall limit drilling noise to CNEL 65 dB(A). Impulse noises such as sudden steam venting shall be controlled by mufflers.",
                }
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Duplicate Key Merge Doc",
        )

        assert result.summary["review_category_counts"] == {"aligned": 1}

    def test_text_review_absorbs_decommissioning_procedural_detail(self, temp_dir):
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "facility_type", "specific_subject"]
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        model_a = {
            "requirements": [
                {
                    "category": "Decommissioning",
                    "facility_type": "All facilities",
                    "specific_subject": "facility removal",
                    "details": "When the operation of the permitted Project has ceased, all facilities on the site shall be secured until an alternative use is found for the facilities, or they are dismantled and removed.",
                }
            ]
        }
        model_b = {
            "requirements": [
                {
                    "category": "Decommissioning",
                    "facility_type": "Exploration/drilling",
                    "specific_subject": "facility removal",
                    "details": "Drilling operations shall be diligently pursued until each well is completed or abandoned. All drilling equipment, including derrick, shall be removed from the premises as soon as practicable after completion of any well.",
                }
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Decommissioning Absorption Doc",
        )

        assert result.summary["review_category_counts"] == {"scope_variant": 1}

    def test_text_review_absorbs_parking_detail_into_roads_and_parking(self, temp_dir):
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "facility_type", "specific_subject"]
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        model_a = {
            "requirements": [
                {
                    "category": "Other",
                    "facility_type": "All facilities",
                    "specific_subject": "roads and parking",
                    "details": "All proposed on-site roads and parking areas shall be improved to County standards. On-site parking shall be provided for all employees, customers, or clients.",
                }
            ]
        }
        model_b = {
            "requirements": [
                {
                    "category": "Other",
                    "facility_type": "Exploration/drilling",
                    "specific_subject": "parking",
                    "details": "A minimum of five off-street parking spaces, to County standards, shall be provided for each well site.",
                }
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Parking Absorption Doc",
        )

        assert result.summary["review_category_counts"] == {"scope_variant": 1}

    def test_text_review_keeps_missing_item_when_subsumption_is_ambiguous(self, temp_dir):
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "facility_type", "specific_subject"]
        mock.get_context_objects.return_value = []
        mock.get_expected_requirements.return_value = []

        model_a = {
            "requirements": [
                {
                    "category": "Other",
                    "facility_type": "All facilities",
                    "specific_subject": "roads",
                    "details": "Road standards and parking requirements apply.",
                },
                {
                    "category": "Other",
                    "facility_type": "All facilities",
                    "specific_subject": "parking",
                    "details": "Parking requirements apply and roads must meet County standards.",
                },
            ]
        }
        model_b = {
            "requirements": [
                {
                    "category": "Other",
                    "facility_type": "All facilities",
                    "specific_subject": "roads and parking",
                    "details": "Road standards apply and parking requirements must be met.",
                }
            ]
        }

        path_a = temp_dir / "gpt-4o.json"
        path_b = temp_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "qualitative",
                "mode": "qualitative",
                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Ambiguous Subsumption Doc",
        )

        assert result.summary["review_category_counts"] == {"scope_variant": 1}

    def test_compare_numeric_values_agree(self, mock_schema_metadata, temp_dir):
        """Test comparison with numeric values that agree."""
        model_a_data = {
            "jurisdiction": {"state": "Colorado"},
            "requirements": [
                {"category": "Setback", "specific_subject": "property", "section": "1.1", 
                 "value": 100, "unit": "feet"},
            ]
        }
        model_b_data = {
            "jurisdiction": {"state": "Colorado"},
            "requirements": [
                {"category": "Setback", "specific_subject": "property", "section": "1.1", 
                 "value": 100, "unit": "feet"},
            ]
        }
        
        path_a = temp_dir / "model_a.json"
        path_b = temp_dir / "model_b.json"
        path_a.write_text(json.dumps(model_a_data))
        path_b.write_text(json.dumps(model_b_data))
        
        engine = ComparisonEngine(mock_schema_metadata)
        result = engine.compare_outputs(
            output_files={"model_a": path_a, "model_b": path_b},
            document_name="test_doc"
        )
        
        assert result.document_name == "test_doc"
        assert len(result.models) == 2
        # Numeric fields should agree
        assert result.summary["full_agreement_pct"] == 100.0

    def test_compare_numeric_values_disagree(self, mock_schema_metadata, temp_dir):
        """Test comparison with disagreeing numeric values."""
        model_a_data = {
            "jurisdiction": {"state": "Colorado"},
            "requirements": [
                {"category": "Setback", "specific_subject": "property", "section": "1.1", 
                 "value": 100, "unit": "feet"},
            ]
        }
        model_b_data = {
            "jurisdiction": {"state": "Colorado"},
            "requirements": [
                {"category": "Setback", "specific_subject": "property", "section": "1.1", 
                 "value": 150, "unit": "feet"},  # Different!
            ]
        }
        
        path_a = temp_dir / "model_a.json"
        path_b = temp_dir / "model_b.json"
        path_a.write_text(json.dumps(model_a_data))
        path_b.write_text(json.dumps(model_b_data))
        
        engine = ComparisonEngine(mock_schema_metadata)
        result = engine.compare_outputs(
            output_files={"model_a": path_a, "model_b": path_b},
            document_name="test_doc"
        )
        
        # Find the value field comparison
        value_comparison = next(
            (fc for fc in result.item_comparisons if fc.field_path == "value"),
            None
        )
        assert value_comparison is not None
        assert value_comparison.needs_review is True
        assert value_comparison.agreement_score == "1/2"

    def test_skips_text_only_fields(self, mock_schema_metadata, temp_dir):
        """Test that text-only fields are skipped."""
        model_a_data = {
            "jurisdiction": {"state": "Colorado"},
            "requirements": [
                {"category": "Setback", "specific_subject": "property", "section": "1.1", 
                 "description": "This is a long description", 
                 "notes": "Some notes here"},
            ]
        }
        model_b_data = {
            "jurisdiction": {"state": "Colorado"},
            "requirements": [
                {"category": "Setback", "specific_subject": "property", "section": "1.1", 
                 "description": "A completely different description",  # Would differ
                 "notes": "Different notes"},  # Would differ
            ]
        }
        
        path_a = temp_dir / "model_a.json"
        path_b = temp_dir / "model_b.json"
        path_a.write_text(json.dumps(model_a_data))
        path_b.write_text(json.dumps(model_b_data))
        
        engine = ComparisonEngine(mock_schema_metadata)
        result = engine.compare_outputs(
            output_files={"model_a": path_a, "model_b": path_b},
            document_name="test_doc"
        )
        
        # Text-only fields should be skipped (not in comparisons)
        field_paths = [fc.field_path for fc in result.item_comparisons]
        assert "description" not in field_paths
        assert "notes" not in field_paths
        assert "category" not in field_paths  # Also text-only
        
        # Should report skipped fields
        assert result.summary["skipped_non_numeric"] > 0

    def test_compare_outputs_with_missing_item(self, mock_schema_metadata, temp_dir):
        """Test comparison when item is missing from one model."""
        model_a_data = {
            "jurisdiction": {"state": "Colorado"},
            "requirements": [
                {"category": "Setback", "specific_subject": "property", "section": "1.1", "value": 100},
                {"category": "Permit", "specific_subject": "commercial", "section": "2.1", "value": "required"},
            ]
        }
        model_b_data = {
            "jurisdiction": {"state": "Colorado"},
            "requirements": [
                {"category": "Setback", "specific_subject": "property", "section": "1.1", "value": 100},
                # Missing the Permit item
            ]
        }
        
        path_a = temp_dir / "model_a.json"
        path_b = temp_dir / "model_b.json"
        path_a.write_text(json.dumps(model_a_data))
        path_b.write_text(json.dumps(model_b_data))
        
        engine = ComparisonEngine(mock_schema_metadata)
        result = engine.compare_outputs(
            output_files={"model_a": path_a, "model_b": path_b},
            document_name="test_doc"
        )
        
        # When an item is missing from one model, we now show actual field values
        # instead of _item_presence. The missing model will have None values.
        assert len(result.item_comparisons) >= 1
        
        # At least one comparison should need review (for the missing item)
        needs_review = [fc for fc in result.item_comparisons if fc.needs_review]
        assert len(needs_review) >= 1
        
        # Find the comparison for the missing permit item
        permit_comparisons = [fc for fc in result.item_comparisons if "permit" in fc.item_id.lower()]
        assert len(permit_comparisons) == 1
        assert "model_b" in permit_comparisons[0].notes

    def test_compare_outputs_three_models(self, mock_schema_metadata, temp_dir):
        """Test comparison with three models."""
        model_a_data = {"requirements": [
            {"category": "Setback", "specific_subject": "test", "section": "1.1", "value": 100}
        ]}
        model_b_data = {"requirements": [
            {"category": "Setback", "specific_subject": "test", "section": "1.1", "value": 100}
        ]}
        model_c_data = {"requirements": [
            {"category": "Setback", "specific_subject": "test", "section": "1.1", "value": 150}  # Different
        ]}
        
        for model, data in [("a", model_a_data), ("b", model_b_data), ("c", model_c_data)]:
            (temp_dir / f"model_{model}.json").write_text(json.dumps(data))
        
        mock_schema_metadata.get_context_objects.return_value = []
        engine = ComparisonEngine(mock_schema_metadata)
        result = engine.compare_outputs(
            output_files={
                "model_a": temp_dir / "model_a.json",
                "model_b": temp_dir / "model_b.json",
                "model_c": temp_dir / "model_c.json"
            },
            document_name="test_doc"
        )
        
        # Check value comparison (2/3 agree on 100)
        value_comparison = next(
            (fc for fc in result.item_comparisons if fc.field_path == "value"), None
        )
        assert value_comparison is not None
        assert value_comparison.agreement_score == "2/3"
        assert value_comparison.needs_review is True

    def test_compare_outputs_missing_file(self, mock_schema_metadata, temp_dir):
        """Test handling of missing output file."""
        path_a = temp_dir / "model_a.json"
        path_a.write_text(json.dumps({
            "requirements": [{"category": "test", "specific_subject": "x", "section": "1", "value": 100}]
        }))
        
        path_b = temp_dir / "nonexistent.json"  # Does not exist
        
        mock_schema_metadata.get_context_objects.return_value = []
        engine = ComparisonEngine(mock_schema_metadata)
        result = engine.compare_outputs(
            output_files={"model_a": path_a, "model_b": path_b},
            document_name="test_doc"
        )
        
        # Should have error or only one model
        assert len(result.models) == 1 or "error" in result.summary

    def test_compare_outputs_empty_arrays(self, mock_schema_metadata, temp_dir):
        """Test comparison with empty requirement arrays."""
        model_a_data = {"requirements": []}
        model_b_data = {"requirements": []}
        
        path_a = temp_dir / "model_a.json"
        path_b = temp_dir / "model_b.json"
        path_a.write_text(json.dumps(model_a_data))
        path_b.write_text(json.dumps(model_b_data))
        
        mock_schema_metadata.get_context_objects.return_value = []
        engine = ComparisonEngine(mock_schema_metadata)
        result = engine.compare_outputs(
            output_files={"model_a": path_a, "model_b": path_b},
            document_name="test_doc"
        )
        
        assert result.summary["total_comparisons"] == 0
        assert len(result.field_comparisons) == 0

    def test_get_flat_fields(self, mock_schema_metadata):
        """Test flattening nested dict to field paths."""
        engine = ComparisonEngine(mock_schema_metadata)
        
        nested = {
            "category": "Setback",
            "details": {
                "description": "test",
                "value": 50
            },
            "value": 100
        }
        
        fields = engine._get_flat_fields(nested)
        assert "category" in fields
        assert "details.description" in fields
        assert "details.value" in fields
        assert "value" in fields

    def test_count_agreement(self, mock_schema_metadata):
        """Test agreement counting."""
        engine = ComparisonEngine(mock_schema_metadata)
        
        # All agree
        assert engine._count_agreement({"a": 100, "b": 100, "c": 100}) == 3
        
        # Two agree
        assert engine._count_agreement({"a": 100, "b": 100, "c": 200}) == 2
        
        # All different
        assert engine._count_agreement({"a": 100, "b": 200, "c": 300}) == 1
        
        # With None values - 2 agree on 100
        assert engine._count_agreement({"a": 100, "b": None, "c": 100}) == 2
        
        # All None - this is full agreement (all empty)
        assert engine._count_agreement({"a": None, "b": None}) == 2

    def test_summary_includes_numeric_approach(self, mock_schema_metadata, temp_dir):
        """Test that summary indicates numeric-only approach."""
        model_a_data = {"requirements": [
            {"category": "A", "specific_subject": "x", "section": "1", "value": 100}
        ]}
        model_b_data = {"requirements": [
            {"category": "A", "specific_subject": "x", "section": "1", "value": 100}
        ]}
        
        path_a = temp_dir / "model_a.json"
        path_b = temp_dir / "model_b.json"
        path_a.write_text(json.dumps(model_a_data))
        path_b.write_text(json.dumps(model_b_data))
        
        mock_schema_metadata.get_context_objects.return_value = []
        engine = ComparisonEngine(mock_schema_metadata)
        result = engine.compare_outputs(
            output_files={"model_a": path_a, "model_b": path_b},
            document_name="test_doc"
        )
        
        # Check summary structure
        assert result.summary["comparison_approach"] == "numeric_only"
        assert result.summary["qaqc_config_source"] == "schema_metadata"
        assert result.summary["qaqc_lane"] is None
        assert "skipped_non_numeric" in result.summary
        assert "items_per_model" in result.summary

    def test_summary_includes_runtime_lane_metadata(self, mock_schema_metadata, temp_dir):
        """Runtime QA/QC lane metadata should flow into comparison summaries."""
        model_a_data = {"requirements": [
            {"category": "A", "specific_subject": "x", "section": "1", "value": 100}
        ]}
        model_b_data = {"requirements": [
            {"category": "A", "specific_subject": "x", "section": "1", "value": 100}
        ]}

        path_a = temp_dir / "model_a.json"
        path_b = temp_dir / "model_b.json"
        path_a.write_text(json.dumps(model_a_data))
        path_b.write_text(json.dumps(model_b_data))

        mock_schema_metadata.get_context_objects.return_value = []
        engine = ComparisonEngine(
            mock_schema_metadata,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "quantitative",
                "mode": "quantitative",
                "comparison_approach": "numeric_only",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["value"],
            },
        )
        result = engine.compare_outputs(
            output_files={"model_a": path_a, "model_b": path_b},
            document_name="test_doc"
        )

        assert result.summary["qaqc_config_source"] == "runtime_artifact"
        assert result.summary["qaqc_lane"] == "quantitative"
        assert result.summary["qaqc_mode"] == "quantitative"


class TestComparisonEngineIntegration:
    """Integration tests using realistic data structure."""

    @pytest.fixture
    def geothermal_schema_metadata(self):
        """Create mock schema metadata matching geothermal schema."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["jurisdiction.state", "jurisdiction.county"]
        mock.get_deduplication_key_fields.return_value = [
            "category", "specific_subject", "applies_to", "section"
        ]
        mock.get_context_objects.return_value = ["jurisdiction"]
        # QA/QC config from geothermal schema
        mock.get_qa_qc_match_fields.return_value = ["category", "applies_to"]
        mock.get_qa_qc_compare_fields.return_value = ["value", "unit"]
        return mock

    @pytest.fixture
    def temp_qa_qc_dir(self):
        """Create temp directory structure mimicking QA/QC output."""
        tmp = tempfile.mkdtemp()
        doc_dir = Path(tmp) / "qa_qc" / "Test County"
        doc_dir.mkdir(parents=True)
        yield doc_dir
        shutil.rmtree(tmp)

    def test_realistic_geothermal_comparison(self, geothermal_schema_metadata, temp_qa_qc_dir):
        """Test with realistic geothermal ordinance data."""
        # Model A output
        model_a = {
            "jurisdiction": {"state": "Colorado", "county": "Test County"},
            "requirements": [
                {
                    "category": "Setback",
                    "specific_subject": "from property boundary",
                    "applies_to": "All geothermal facilities",
                    "value": 100,
                    "unit": "feet",
                    "section": "10-404(2)(b)"
                },
                {
                    "category": "Permit required",
                    "specific_subject": "Commercial Use",
                    "applies_to": "All geothermal facilities",
                    "value": "Required",  # Text, will be skipped
                    "unit": None,
                    "section": "10-103(1)"
                }
            ]
        }
        
        # Model B output - value differs
        model_b = {
            "jurisdiction": {"state": "Colorado", "county": "Test County"},
            "requirements": [
                {
                    "category": "Setback",
                    "specific_subject": "from property boundary",
                    "applies_to": "All geothermal facilities",
                    "value": 100,  # Same
                    "unit": "ft",  # Different formatting - but both text
                    "section": "10-404(2)(b)"
                },
                {
                    "category": "Permit required",
                    "specific_subject": "Commercial Use",
                    "applies_to": "All geothermal facilities",
                    "value": "Required",
                    "unit": None,
                    "section": "10-103(1)"
                }
            ]
        }
        
        path_a = temp_qa_qc_dir / "gpt-4o.json"
        path_b = temp_qa_qc_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))
        
        engine = ComparisonEngine(geothermal_schema_metadata)
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Test County"
        )
        
        assert result.document_name == "Test County"
        assert "gpt-4o" in result.models
        assert "gpt-4.1" in result.models
        
        # Only NUMERIC 'value' fields are compared (text values like "Required" are skipped)
        value_comparisons = [fc for fc in result.item_comparisons if fc.field_path == "value"]
        # Only the numeric value (100) is compared, text value ("Required") is skipped
        assert len(value_comparisons) == 1  # Only numeric value fields compared
        # The numeric value should agree (100=100)
        assert all(vc.agreement_score == "2/2" for vc in value_comparisons)
        
        # Text fields (category, section, "Required" value, etc.) should be skipped
        assert result.summary["skipped_non_numeric"] > 0

    def test_tariff_nested_charge_projection_comparison(self, temp_qa_qc_dir):
        """Tariff QA/QC projection should flatten nested charges into compareable items."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "rate_schedules"
        mock.get_identifier_fields.return_value = ["utility_info.utility_name"]
        mock.get_context_objects.return_value = ["utility_info"]
        mock.get_expected_requirements.return_value = []
        mock.get_expected_count_range.return_value = (1, 100)

        model_a = {
            "utility_info": {"utility_name": "Metro Electric", "state": "CO"},
            "rate_schedules": [
                {
                    "rate_name": "Schedule R",
                    "is_rider": False,
                    "sector": "Residential",
                    "charges": [
                        {
                            "charge_type": "Energy charge",
                            "charge_description": "All kilowatt-hours",
                            "rate": 0.12,
                            "unit": "$/kWh",
                            "season": "Summer",
                            "time_period": "On-Peak",
                            "extracted_text": "Energy charge 0.12",
                        }
                    ],
                }
            ],
        }
        model_b = {
            "utility_info": {"utility_name": "Metro Electric", "state": "CO"},
            "rate_schedules": [
                {
                    "rate_name": "Schedule R",
                    "is_rider": False,
                    "sector": "Residential",
                    "charges": [
                        {
                            "charge_type": "Energy charge",
                            "charge_description": "All kilowatt-hours",
                            "rate": 0.15,
                            "unit": "$/kWh",
                            "season": "Summer",
                            "time_period": "On-Peak",
                            "extracted_text": "Energy charge 0.15",
                        }
                    ],
                }
            ],
        }

        path_a = temp_qa_qc_dir / "gpt-4o.json"
        path_b = temp_qa_qc_dir / "gpt-4.1.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                "source": "runtime_artifact",
                "lane_name": "quantitative",
                "mode": "quantitative",
                "comparison_approach": "numeric_only",
                "projection": {
                    "type": "nested_array_items",
                    "source_array": "rate_schedules",
                    "nested_array": "charges",
                    "parent_fields": ["rate_name", "is_rider", "sector"],
                },
                "match_fields": ["rate_name", "charge_type", "charge_description", "season", "time_period"],
                "compare_fields": ["rate", "unit"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Tariff Doc",
        )

        assert result.summary["items_per_model"] == {"gpt-4o": 1, "gpt-4.1": 1}
        rate_comparison = next(
            (fc for fc in result.item_comparisons if fc.field_path == "rate"),
            None,
        )
        assert rate_comparison is not None
        assert rate_comparison.agreement_score == "1/2"
        assert rate_comparison.needs_review is True
        assert "schedule r" in rate_comparison.item_id


# --- Phase 8: Tests for Potential Duplicate Detection and Completeness ---

class TestPotentialDuplicateDetection:
    """Tests for Phase 8 potential duplicate detection."""

    @pytest.fixture
    def mock_schema_metadata_with_expected(self):
        """Create a mock SchemaMetadata with expected requirements (compound key format)."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["source.state"]
        mock.get_context_objects.return_value = ["source"]
        mock.get_qa_qc_match_fields.return_value = ["requirement_type"]
        mock.get_qa_qc_compare_fields.return_value = ["value", "source_text"]
        mock.get_expected_requirements.return_value = [
            "setback__property_line_ft",
            "setback__residence_ft",
            "noise__at_property_line_dba",
        ]
        mock.get_expected_count_range.return_value = (3, 10)
        return mock

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for test files."""
        tmp = tempfile.mkdtemp()
        yield Path(tmp)
        shutil.rmtree(tmp)

    def test_detect_potential_duplicate_same_value_different_key(
        self, mock_schema_metadata_with_expected, temp_dir
    ):
        """Test detection when same value is extracted with different keys."""
        # Model A: time__reclamation_deadline_days = 60
        model_a = {
            "source": {"state": "CO"},
            "requirements": [
                {"requirement_type": "setback__property_line_ft", "value": 100, "source_text": "100 ft setback"},
                {"requirement_type": "time__reclamation_deadline_days", "value": 60, "source_text": "60 days reclamation"},
            ]
        }
        
        # Model B: time__permit_validity_days = 60 (SAME value, different key)
        model_b = {
            "source": {"state": "CO"},
            "requirements": [
                {"requirement_type": "setback__property_line_ft", "value": 100, "source_text": "100 ft setback"},
                {"requirement_type": "time__permit_validity_days", "value": 60, "source_text": "60 days permit"},
            ]
        }
        
        path_a = temp_dir / "model_a.json"
        path_b = temp_dir / "model_b.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))
        
        engine = ComparisonEngine(mock_schema_metadata_with_expected)
        result = engine.compare_outputs(
            output_files={"model_a": path_a, "model_b": path_b},
            document_name="Test Doc"
        )
        
        # Should detect 60 as potential duplicate (same value, different requirement_type)
        assert len(result.potential_duplicates) == 1
        dup = result.potential_duplicates[0]
        assert dup.value == 60
        assert len(dup.items) == 2  # Both models
        
    def test_no_duplicates_when_different_values(
        self, mock_schema_metadata_with_expected, temp_dir
    ):
        """Test no duplicates detected when values are different."""
        model_a = {
            "source": {"state": "CO"},
            "requirements": [
                {"requirement_type": "time__reclamation_deadline_days", "value": 60, "source_text": "60 days"},
            ]
        }
        
        model_b = {
            "source": {"state": "CO"},
            "requirements": [
                {"requirement_type": "time__permit_validity_days", "value": 30, "source_text": "30 days"},
            ]
        }
        
        path_a = temp_dir / "model_a.json"
        path_b = temp_dir / "model_b.json"
        path_a.write_text(json.dumps(model_a))
        path_b.write_text(json.dumps(model_b))
        
        engine = ComparisonEngine(mock_schema_metadata_with_expected)
        result = engine.compare_outputs(
            output_files={"model_a": path_a, "model_b": path_b},
            document_name="Test Doc"
        )
        
        # No duplicates - values are different
        assert len(result.potential_duplicates) == 0


class TestCompletenessCalculation:
    """Tests for Phase 8 completeness calculation (compound key format)."""

    @pytest.fixture
    def mock_schema_with_expected(self):
        """Create mock with expected requirements (compound key format)."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["source.state"]
        mock.get_context_objects.return_value = ["source"]
        mock.get_qa_qc_match_fields.return_value = ["requirement_type"]
        mock.get_qa_qc_compare_fields.return_value = ["value", "source_text"]
        mock.get_expected_requirements.return_value = [
            "setback__property_line_ft",
            "setback__residence_ft",
            "noise__at_property_line_dba",
        ]
        mock.get_expected_count_range.return_value = (3, 10)
        return mock

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for test files."""
        tmp = tempfile.mkdtemp()
        yield Path(tmp)
        shutil.rmtree(tmp)

    def test_completeness_all_expected_found(self, mock_schema_with_expected, temp_dir):
        """Test completeness when all expected requirements are found."""
        model_output = {
            "source": {"state": "CO"},
            "requirements": [
                {"requirement_type": "setback__property_line_ft", "value": 100, "source_text": "100 ft"},
                {"requirement_type": "setback__residence_ft", "value": 1320, "source_text": "1320 ft"},
                {"requirement_type": "noise__at_property_line_dba", "value": 65, "source_text": "65 dBA"},
            ]
        }
        
        path = temp_dir / "model.json"
        path.write_text(json.dumps(model_output))
        
        engine = ComparisonEngine(mock_schema_with_expected)
        result = engine.compare_outputs(
            output_files={"model_a": path, "model_b": path},  # Same file twice for simplicity
            document_name="Test Doc"
        )
        
        # Both models should have 100% completeness
        assert "model_a" in result.completeness
        comp = result.completeness["model_a"]
        assert comp.expected_found == 3
        assert comp.expected_total == 3
        assert comp.completeness_score == 1.0
        assert comp.missing_expected == []

    def test_completeness_partial_expected_found(self, mock_schema_with_expected, temp_dir):
        """Test completeness when only some expected requirements are found."""
        model_output = {
            "source": {"state": "CO"},
            "requirements": [
                {"requirement_type": "setback__property_line_ft", "value": 100, "source_text": "100 ft"},
                # Missing: setback__residence_ft and noise__at_property_line_dba
            ]
        }
        
        path = temp_dir / "model.json"
        path.write_text(json.dumps(model_output))
        
        engine = ComparisonEngine(mock_schema_with_expected)
        result = engine.compare_outputs(
            output_files={"model_a": path, "model_b": path},
            document_name="Test Doc"
        )
        
        comp = result.completeness["model_a"]
        assert comp.expected_found == 1
        assert comp.expected_total == 3
        assert abs(comp.completeness_score - 1/3) < 0.01
        assert len(comp.missing_expected) == 2

    def test_completeness_in_summary(self, mock_schema_with_expected, temp_dir):
        """Test that completeness metrics appear in summary."""
        model_output = {
            "source": {"state": "CO"},
            "requirements": [
                {"requirement_type": "setback__property_line_ft", "value": 100, "source_text": "100 ft"},
            ]
        }
        
        path = temp_dir / "model.json"
        path.write_text(json.dumps(model_output))
        
        engine = ComparisonEngine(mock_schema_with_expected)
        result = engine.compare_outputs(
            output_files={"model_a": path, "model_b": path},
            document_name="Test Doc"
        )
        
        assert "completeness_per_model" in result.summary
        assert "model_a" in result.summary["completeness_per_model"]
        
    def test_no_expected_requirements(self, temp_dir):
        """Test behavior when no expected requirements are configured."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["source.state"]
        mock.get_context_objects.return_value = []
        mock.get_qa_qc_match_fields.return_value = ["requirement_type"]
        mock.get_qa_qc_compare_fields.return_value = ["value", "source_text"]
        mock.get_expected_requirements.return_value = []  # No expected requirements
        mock.get_expected_count_range.return_value = (1, 100)
        
        model_output = {
            "source": {"state": "CO"},
            "requirements": [
                {"requirement_type": "setback__property_line_ft", "value": 100, "source_text": "100 ft"},
            ]
        }
        
        path = temp_dir / "model.json"
        path.write_text(json.dumps(model_output))
        
        engine = ComparisonEngine(mock)
        result = engine.compare_outputs(
            output_files={"model_a": path, "model_b": path},
            document_name="Test Doc"
        )
        
        # With no expected requirements, completeness should be 100%
        comp = result.completeness["model_a"]
        assert comp.completeness_score == 1.0
        assert comp.expected_total == 0
