"""
Tests for QA/QC Comparison Engine.

Tests the ComparisonEngine class which compares outputs from multiple AI models
to identify discrepancies using numeric, categorical, and text comparison approaches.
"""

import copy
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


def _build_schema_metadata_mock(
    *,
    main_data_array: str = "requirements",
    identifier_fields: list[str] | None = None,
    deduplication_key_fields: list[str] | None = None,
    context_objects: list[str] | None = None,
) -> MagicMock:
    """Build a minimal schema_metadata mock (extraction contract only).

    QA/QC runtime config (match_fields, compare_fields, expected_requirements)
    lives in run config YAML — pass qa_qc_config directly to ComparisonEngine.
    """
    mock = MagicMock()
    mock.get_main_data_array.return_value = main_data_array
    mock.get_identifier_fields.return_value = identifier_fields or []
    mock.get_deduplication_key_fields.return_value = deduplication_key_fields or []
    mock.get_context_objects.return_value = context_objects or []
    return mock


def _write_output_files(base_dir: Path, payloads: dict[str, dict]) -> dict[str, Path]:
    output_files: dict[str, Path] = {}
    for model_name, payload in payloads.items():
        path = base_dir / f"{model_name}.json"
        path.write_text(json.dumps(payload))
        output_files[model_name] = path
    return output_files


class TestFieldComparison:
    """Tests for FieldComparison dataclass."""

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


class TestComparisonEngine:
    """Tests for ComparisonEngine class - Numeric Only comparison."""

    @pytest.fixture
    def mock_schema_metadata(self):
        """Minimal schema_metadata mock — extraction contract only, no QA/QC fields."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["jurisdiction.state"]
        mock.get_deduplication_key_fields.return_value = [
            "category", "specific_subject", "section"
        ]
        mock.get_context_objects.return_value = ["jurisdiction"]
        return mock

    # Standard qa_qc_config for tests that just need a working engine.
    _BASE_QA_QC_CONFIG = {
        "comparison_approach": "numeric_only",
        "match_fields": ["category", "specific_subject"],
        "compare_fields": ["value", "unit"],
    }

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for test files."""
        tmp = tempfile.mkdtemp()
        yield Path(tmp)
        shutil.rmtree(tmp)

    def test_init_requires_qaqc_config(self, mock_schema_metadata):
        """Engine initializes correctly when qa_qc_config is provided."""
        engine = ComparisonEngine(mock_schema_metadata, qa_qc_config=self._BASE_QA_QC_CONFIG)
        assert engine.main_data_array == "requirements"
        assert "category" in engine.match_fields
        assert "specific_subject" in engine.match_fields
        assert "value" in engine.compare_fields
        assert "unit" in engine.compare_fields

    def test_runtime_qaqc_config_sets_match_and_compare_fields(self):
        """qa_qc_config drives match_fields and compare_fields — schema has no say."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "items"
        mock.get_identifier_fields.return_value = []
        mock.get_context_objects.return_value = []

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                                "comparison_approach": "numeric_only",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["value", "unit"],
            },
        )

        assert engine.match_fields == ["category", "facility_type", "specific_subject"]
        assert engine.compare_fields == {"value", "unit"}

    def test_semantic_alignment_score_treats_time_formats_as_equivalent(self):
        """Mixed-lane semantic matching should treat 07:00 and 7 a.m. as equivalent time."""
        schema = _build_schema_metadata_mock(
            deduplication_key_fields=["feature", "applies_to", "specific_subject"]
        )
        engine = ComparisonEngine(
            schema,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["feature", "applies_to", "specific_subject"],
                "compare_fields": ["value", "units", "value_interpretation", "obligation"],
                "enable_text_fallback_matching": True,
            },
        )
        item_a = {
            "feature": "drilling start time",
            "applies_to": "geothermal well",
            "specific_subject": "drilling preparation site",
            "value": "07:00",
            "units": "HH:MM (24-hour)",
        }
        item_b = {
            "feature": "drilling start time",
            "applies_to": "300 feet from residences",
            "specific_subject": "drilling preparation site",
            "value": "7 a.m.",
            "units": "a.m./p.m.",
        }
        assert engine._values_semantically_equivalent(
            item_a["value"], item_b["value"]
        )
        assert engine._semantic_alignment_score(item_a, item_b) >= 0.38

    def test_values_semantically_equivalent_treats_time_ranges_as_equal(self):
        """Time ranges in 12h and 24h forms should compare as equivalent."""
        schema = _build_schema_metadata_mock(
            deduplication_key_fields=["feature", "applies_to", "specific_subject"]
        )
        engine = ComparisonEngine(
            schema,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["feature", "applies_to", "specific_subject"],
                "compare_fields": ["value", "units"],
            },
        )
        assert engine._values_semantically_equivalent("7 a.m. to 7 p.m.", "07:00-19:00")
        assert not engine._values_semantically_equivalent("7 a.m. to 6 p.m.", "07:00-19:00")

    def test_count_agreement_handles_temporal_field_names(self):
        """Time-like field names should compare by normalized time values."""
        schema = _build_schema_metadata_mock()
        engine = ComparisonEngine(
            schema,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["feature"],
                "compare_fields": ["start_time"],
            },
        )
        agreement = engine._count_agreement_for_field(
            field_name="start_time",
            normalized_values={"a": "7 a.m.", "b": "07:00"},
        )
        assert agreement == 2

    def test_time_unit_label_detection_is_not_substring_based(self):
        """Non-time units containing 'hour' substring should not be treated as time labels."""
        schema = _build_schema_metadata_mock()
        engine = ComparisonEngine(
            schema,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["feature"],
                "compare_fields": ["value", "units"],
            },
        )
        assert engine._is_time_unit_label("HH:MM (24-hour)")
        assert not engine._is_time_unit_label("kilowatt-hours")

    def test_format_item_id_for_anchor_keys_uses_match_field_labels(self):
        """Synthetic anchor keys should render user-meaningful requirement labels."""
        schema = _build_schema_metadata_mock(
            deduplication_key_fields=["feature", "applies_to", "specific_subject"]
        )
        engine = ComparisonEngine(
            schema,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["feature", "applies_to", "specific_subject"],
                "compare_fields": ["value", "units"],
            },
        )
        item = {
            "feature": "working hours",
            "applies_to": "drilling operations",
            "specific_subject": "within 300 feet of residences",
        }
        label = engine._format_item_id(("anchor_auto", 14), {"m1": item, "m2": None})
        assert label == "working hours | drilling operations | within 300 feet of residences"

    def test_resolve_qaqc_runtime_config_uses_runtime_artifact_lane(self):
        """Runtime QA/QC config resolution reads from simplified runtime qaqc block."""
        config = resolve_qaqc_runtime_config(
            MagicMock(),
            runtime_artifact={
                "resolved": {
                    "pack": {
                        "qaqc": {
                            "comparison_approach": "numeric_only",
                            "record_matching": {
                                "key_fields": ["referenceNumber", "make", "model"]
                            },
                            "comparison": {
                                "primary_fields": [
                                    "ratedCapacityKW",
                                    "operatingHoursPerUnitLimit",
                                ],
                                "unit_equivalence_groups": [
                                    ["hours", "hrs", "hr"]
                                ],
                            },
                        }
                    }
                }
            },
        )

        assert config["match_fields"] == ["referenceNumber", "make", "model"]
        assert config["compare_fields"] == ["ratedCapacityKW", "operatingHoursPerUnitLimit"]
        assert config["collapse_percent_context"] is False
        assert config["unit_equivalence_groups"] == [["hours", "hrs", "hr"]]
        assert config["projection"] is None

    def test_resolve_qaqc_runtime_config_raises_without_config(self):
        """resolve_qaqc_runtime_config raises ValueError when no config is provided."""
        with pytest.raises(ValueError, match="No QA/QC configuration found"):
            resolve_qaqc_runtime_config(MagicMock())

    def test_resolve_qaqc_runtime_config_raises_when_missing_key_fields(self):
        """resolve_qaqc_runtime_config raises ValueError when config has no record_matching.key_fields."""
        with pytest.raises(ValueError, match="record_matching.key_fields"):
            resolve_qaqc_runtime_config(
                MagicMock(),
                runtime_qaqc={
                    "comparison_approach": "mixed",
                    "comparison": {"primary_fields": ["value"]},
                },
            )

    def test_resolve_qaqc_runtime_config_includes_projection(self):
        """Runtime QA/QC config preserves top-level projection metadata."""
        config = resolve_qaqc_runtime_config(
            MagicMock(),
            runtime_artifact={
                "resolved": {
                    "pack": {
                        "qaqc": {
                            "comparison_approach": "numeric_only",
                            "projection": {
                                "type": "nested_array_items",
                                "source_array": "rate_schedules",
                                "nested_array": "charges",
                                "parent_fields": ["rate_name"],
                            },
                            "record_matching": {
                                "key_fields": ["rate_name", "charge_type"]
                            },
                            "comparison": {"primary_fields": ["rate", "unit"]},
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

    def test_resolve_qaqc_runtime_config_rejects_legacy_lane_shape(self):
        """Lane-based QA/QC config is rejected to keep runtime surface minimal."""
        with pytest.raises(ValueError, match="Deprecated QA/QC lane-style config"):
            resolve_qaqc_runtime_config(
                MagicMock(),
                runtime_qaqc={
                    "default_lane": "quantitative",
                    "lanes": {"quantitative": {"enabled": True}},
                },
            )

    def test_resolve_qaqc_runtime_config_rejects_invalid_judge_max_calls(self):
        with pytest.raises(ValueError, match="max_calls_per_document"):
            resolve_qaqc_runtime_config(
                MagicMock(),
                runtime_qaqc={
                    "comparison_approach": "mixed",
                    "record_matching": {"key_fields": ["feature"]},
                    "comparison": {"primary_fields": ["value"]},
                    "judge": {"enabled": True, "max_calls_per_document": 0},
                },
            )

    def test_resolve_qaqc_runtime_config_parses_report_options(self):
        config = resolve_qaqc_runtime_config(
            MagicMock(),
            runtime_qaqc={
                "comparison_approach": "mixed",
                "record_matching": {"key_fields": ["feature"]},
                "comparison": {"primary_fields": ["value"]},
                "report": {
                    "include_csv": True,
                    "include_missing_in_queue": False,
                    "include_low_signal_presence_in_queue": True,
                    "evidence_detail": "compact",
                    "evidence_max_chars": 350,
                    "identity_columns": "expanded",
                    "include_value_unit_column": False,
                },
            },
        )
        assert config["report"]["include_csv"] is True
        assert config["report"]["evidence_detail"] == "compact"
        assert config["report"]["evidence_max_chars"] == 350
        assert config["report"]["identity_columns"] == "expanded"
        assert config["report"]["include_value_unit_column"] is False

    def test_resolve_qaqc_runtime_config_wires_scope_variant_keys(self):
        config = resolve_qaqc_runtime_config(
            MagicMock(),
            runtime_qaqc={
                "comparison_approach": "mixed",
                "record_matching": {
                    "key_fields": ["feature"],
                    "scope_variant_keys": [["noise", "operator"], ["setback", "exception"]],
                },
                "comparison": {"primary_fields": ["value"]},
            },
        )
        assert config["scope_variant_keys"] == [
            ["noise", "operator"],
            ["setback", "exception"],
        ]

    def test_resolve_qaqc_runtime_config_parses_collapse_percent_context(self):
        config = resolve_qaqc_runtime_config(
            MagicMock(),
            runtime_qaqc={
                "comparison_approach": "mixed",
                "record_matching": {"key_fields": ["feature"]},
                "comparison": {
                    "primary_fields": ["value", "units"],
                    "collapse_percent_context": True,
                },
            },
        )
        assert config["collapse_percent_context"] is True

    def test_resolve_qaqc_runtime_config_rejects_non_bool_collapse_percent_context(self):
        with pytest.raises(ValueError, match="collapse_percent_context"):
            resolve_qaqc_runtime_config(
                MagicMock(),
                runtime_qaqc={
                    "comparison_approach": "mixed",
                    "record_matching": {"key_fields": ["feature"]},
                    "comparison": {
                        "primary_fields": ["value", "units"],
                        "collapse_percent_context": "yes",
                    },
                },
            )

    def test_compare_outputs_text_review_compares_configured_text_fields(self, temp_dir):
        """Qualitative text-review lanes should compare configured text fields instead of skipping them."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = []
        mock.get_context_objects.return_value = []

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
                                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
                # scope_variant_keys: domain-specific pairs that differ between models
                # by design and should be excluded from qualitative gate failure math.
                # Format: [[category, subject], ...] (normalized/lowercase match).
                "scope_variant_keys": [["other", "pipeline siting and configuration"]],
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
        """Without scope_variant_keys configured, unmatched items are missing_item.

        Previously, the engine had hardcoded geothermal-domain patterns that
        classified certain decommissioning items as scope_variant. These patterns
        have been removed — scope variants are now config-driven via
        scope_variant_keys in the lane config.
        """
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "facility_type", "specific_subject"]
        mock.get_context_objects.return_value = []

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
                                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
                # No scope_variant_keys — items that don't match are missing_item.
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Decommissioning Absorption Doc",
        )

        # Without hardcoded domain rules, unmatched items are classified as missing_item.
        assert result.summary["review_category_counts"].get("missing_item", 0) >= 1


    def test_text_review_absorbs_parking_detail_into_roads_and_parking(self, temp_dir):
        """Without scope_variant_keys configured, unmatched items are missing_item.

        Previously, the engine had hardcoded geothermal-domain patterns that
        absorbed parking sub-items under a broader roads-and-parking item.
        These patterns have been removed — absorption is now driven only by
        generic text/token overlap heuristics, not domain-specific terms.
        """
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "facility_type", "specific_subject"]
        mock.get_context_objects.return_value = []

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
                                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
                # No scope_variant_keys — items that don't match are missing_item.
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Parking Absorption Doc",
        )

        # Without hardcoded domain rules, unmatched items are classified as missing_item.
        assert result.summary["review_category_counts"].get("missing_item", 0) >= 1

    def test_text_review_keeps_missing_item_when_subsumption_is_ambiguous(self, temp_dir):
        """Ambiguous multi-item vs single-item groupings remain as missing_item.

        Previously, hardcoded hierarchical rules classified these as scope_variant.
        Without those rules, generic text-overlap absorption may or may not match,
        but the outcome is at minimum missing_item, not scope_variant.
        """
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = ["category", "facility_type", "specific_subject"]
        mock.get_context_objects.return_value = []

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
                                "comparison_approach": "text_review",
                "match_fields": ["category", "facility_type", "specific_subject"],
                "compare_fields": ["details"],
            },
        )
        result = engine.compare_outputs(
            output_files={"gpt-4o": path_a, "gpt-4.1": path_b},
            document_name="Qualitative Ambiguous Subsumption Doc",
        )

        # Ambiguous groupings without scope_variant_keys are not classified as scope_variant.
        assert "scope_variant" not in result.summary["review_category_counts"]


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
        
        engine = ComparisonEngine(mock_schema_metadata, qa_qc_config=self._BASE_QA_QC_CONFIG)
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
        
        engine = ComparisonEngine(mock_schema_metadata, qa_qc_config=self._BASE_QA_QC_CONFIG)
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
        
        engine = ComparisonEngine(mock_schema_metadata, qa_qc_config=self._BASE_QA_QC_CONFIG)
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

    def test_compare_outputs_mixed_compares_enum_fields(self, mock_schema_metadata, temp_dir):
        """mixed approach must compare categorical/enum fields — not silently skip them."""
        model_a_data = {
            "jurisdiction": {"state": "Colorado"},
            "requirements": [
                {
                    "category": "Setback", "specific_subject": "property", "section": "1.1",
                    "value": 100, "unit": "feet", "obligation": "shall",
                    "value_interpretation": "minimum",
                },
            ],
        }
        model_b_data = {
            "jurisdiction": {"state": "Colorado"},
            "requirements": [
                {
                    "category": "Setback", "specific_subject": "property", "section": "1.1",
                    "value": 100, "unit": "feet", "obligation": "should",  # differs
                    "value_interpretation": "maximum",  # differs
                },
            ],
        }
        engine = ComparisonEngine(
            schema_metadata=mock_schema_metadata,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["value", "unit", "obligation", "value_interpretation"],
            },
        )
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a_data, "model_b": model_b_data}
            ),
            document_name="test_doc",
        )
        compared_field_paths = [fc.field_path for fc in result.item_comparisons]
        # obligation and value_interpretation must appear in results (not silently dropped)
        assert any("obligation" in fp for fp in compared_field_paths), (
            "obligation must be compared in mixed mode — was silently dropped"
        )
        assert any("value_interpretation" in fp for fp in compared_field_paths), (
            "value_interpretation must be compared in mixed mode — was silently dropped"
        )
        # The mismatching fields must require review
        obligation_fc = next(fc for fc in result.item_comparisons if "obligation" in fc.field_path)
        assert obligation_fc.needs_review, "obligation='shall' vs 'should' must differ"

    def test_compare_outputs_mixed_agrees_on_all_fields(self, mock_schema_metadata, temp_dir):
        """mixed approach should NOT flag disagreement when all fields match."""
        model_a_data = {
            "jurisdiction": {"state": "Colorado"},
            "requirements": [
                {
                    "category": "Setback", "specific_subject": "property", "section": "1.1",
                    "value": 100, "unit": "feet", "obligation": "shall",
                    "value_interpretation": "minimum",
                },
            ],
        }
        engine = ComparisonEngine(
            schema_metadata=mock_schema_metadata,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["value", "unit", "obligation", "value_interpretation"],
            },
        )
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a_data, "model_b": copy.deepcopy(model_a_data)}
            ),
            document_name="test_doc",
        )
        compared_field_paths = [fc.field_path for fc in result.item_comparisons]
        assert any("obligation" in fp for fp in compared_field_paths)
        assert any("value_interpretation" in fp for fp in compared_field_paths)
        # No field should need review
        reviewing = [fc for fc in result.item_comparisons if fc.needs_review]
        assert len(reviewing) == 0, f"All fields match, nothing should need review; got: {reviewing}"

    def test_mixed_judge_skips_numeric_mismatches_by_default(self, mock_schema_metadata, temp_dir):
        """Cost optimization: mixed judge with apply_on=non_numeric must skip numeric disagreements."""
        model_a_data = {
            "requirements": [
                {
                    "category": "Setback",
                    "specific_subject": "property",
                    "section": "1.1",
                    "value": 100,
                    "unit": "feet",
                }
            ]
        }
        model_b_data = {
            "requirements": [
                {
                    "category": "Setback",
                    "specific_subject": "property",
                    "section": "1.1",
                    "value": 150,
                    "unit": "feet",
                }
            ]
        }
        engine = ComparisonEngine(
            schema_metadata=mock_schema_metadata,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["value", "unit"],
                "judge": {
                    "enabled": True,
                    "apply_on": "non_numeric",
                    "min_confidence": "high",
                },
                "judge_runtime": {"model": "judge-model"},
            },
        )
        judge_mock = MagicMock(
            return_value={
                "equivalent": True,
                "confidence": "high",
                "reason": "same meaning",
            }
        )
        engine._judge_text_equivalence = judge_mock
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a_data, "model_b": model_b_data}
            ),
            document_name="mixed_numeric_skip",
        )
        assert judge_mock.call_count == 0
        value_fc = next(fc for fc in result.item_comparisons if fc.field_path == "value")
        assert value_fc.needs_review is True

    def test_mixed_judge_can_resolve_non_numeric_mismatch(self, mock_schema_metadata, temp_dir):
        """Mixed lane judge should run for non-numeric mismatches when enabled."""
        model_a_data = {
            "requirements": [
                {
                    "category": "Setback",
                    "specific_subject": "property",
                    "section": "1.1",
                    "obligation": "shall",
                }
            ]
        }
        model_b_data = {
            "requirements": [
                {
                    "category": "Setback",
                    "specific_subject": "property",
                    "section": "1.1",
                    "obligation": "must",
                }
            ]
        }
        engine = ComparisonEngine(
            schema_metadata=mock_schema_metadata,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["obligation"],
                "judge": {
                    "enabled": True,
                    "apply_on": "non_numeric",
                    "min_confidence": "high",
                },
                "judge_runtime": {"model": "judge-model"},
            },
        )
        judge_mock = MagicMock(
            return_value={
                "equivalent": True,
                "confidence": "high",
                "reason": "same obligation strength",
            }
        )
        engine._judge_text_equivalence = judge_mock
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a_data, "model_b": model_b_data}
            ),
            document_name="mixed_non_numeric_judged",
        )
        assert judge_mock.call_count >= 1
        obligation_fc = next(fc for fc in result.item_comparisons if "obligation" in fc.field_path)
        assert obligation_fc.needs_review is False
        assert "LLM judge matched" in obligation_fc.notes

    def test_judge_winner_is_recorded_for_high_confidence_mismatch(self, mock_schema_metadata, temp_dir):
        model_a_data = {
            "requirements": [
                {
                    "category": "Noise",
                    "specific_subject": "operator",
                    "obligation": "must",
                    "source_verbatim": "Operator must maintain records.",
                }
            ]
        }
        model_b_data = {
            "requirements": [
                {
                    "category": "Noise",
                    "specific_subject": "operator",
                    "obligation": "should",
                    "source_verbatim": "Operator should maintain records.",
                }
            ]
        }
        engine = ComparisonEngine(
            schema_metadata=mock_schema_metadata,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["obligation"],
                "judge": {"enabled": True, "apply_on": "non_numeric", "min_confidence": "high"},
                "judge_runtime": {"model": "judge-model"},
            },
        )
        engine._judge_text_equivalence = MagicMock(
            return_value={
                "equivalent": False,
                "confidence": "high",
                "reason": "Model A aligns with verbatim language.",
                "likely_correct_model": "model_a",
            }
        )
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a_data, "model_b": model_b_data}
            ),
            document_name="judge_winner_doc",
        )
        obligation_fc = next(fc for fc in result.item_comparisons if "obligation" in fc.field_path)
        assert obligation_fc.needs_review is True
        assert obligation_fc.judge_likely_correct_model == "model_a"
        assert obligation_fc.judge_confidence == "high"

    def test_judge_budget_caps_calls(self, mock_schema_metadata, temp_dir):
        model_a_data = {
            "requirements": [
                {"category": "A", "specific_subject": "x", "obligation": "must"},
                {"category": "B", "specific_subject": "x", "obligation": "must"},
                {"category": "C", "specific_subject": "x", "obligation": "must"},
            ]
        }
        model_b_data = {
            "requirements": [
                {"category": "A", "specific_subject": "x", "obligation": "should"},
                {"category": "B", "specific_subject": "x", "obligation": "should"},
                {"category": "C", "specific_subject": "x", "obligation": "should"},
            ]
        }
        engine = ComparisonEngine(
            schema_metadata=mock_schema_metadata,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["obligation"],
                "judge": {
                    "enabled": True,
                    "apply_on": "non_numeric",
                    "min_confidence": "high",
                    "max_calls_per_document": 1,
                },
                "judge_runtime": {"model": "judge-model"},
            },
        )
        # Use real budget logic but fake the model call payload.
        engine._judge_client = MagicMock()
        engine._judge_client.extract.return_value = {
            "data": {
                "equivalent": False,
                "confidence": "high",
                "reason": "Different obligation strength.",
                "likely_correct_model": "unclear",
            },
            "input_tokens": 1,
            "output_tokens": 1,
            "cost": 0.0,
        }

        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a_data, "model_b": model_b_data}
            ),
            document_name="judge_budget_doc",
        )
        assert result.summary["judge"]["attempted_calls"] == 1
        assert result.summary["judge"]["skipped_budget"] >= 1

    def test_judge_cache_reuses_decisions_across_reruns(self, mock_schema_metadata, temp_dir):
        model_a_data = {
            "requirements": [
                {"category": "A", "specific_subject": "x", "obligation": "required"}
            ]
        }
        model_b_data = {
            "requirements": [
                {"category": "A", "specific_subject": "x", "obligation": "allowed"}
            ]
        }
        engine = ComparisonEngine(
            schema_metadata=mock_schema_metadata,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["obligation"],
                "judge": {
                    "enabled": True,
                    "apply_on": "non_numeric",
                    "min_confidence": "high",
                    "max_calls_per_document": 10,
                    "reuse_cached_decisions": True,
                },
                "judge_runtime": {"model": "judge-model"},
            },
        )
        engine.set_judge_cache_path(temp_dir / "judge_cache.json")
        engine._judge_client = MagicMock()
        engine._judge_client.extract.return_value = {
            "data": {
                "equivalent": False,
                "confidence": "high",
                "reason": "Different obligation strength.",
                "likely_correct_model": "model_a",
            },
            "input_tokens": 11,
            "output_tokens": 4,
            "cost": 0.01,
        }

        first = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a_data, "model_b": model_b_data}
            ),
            document_name="judge_cache_doc",
        )
        assert first.summary["judge"]["attempted_calls"] == 1
        assert first.summary["judge"]["cache_hits"] == 0
        assert engine._judge_client.extract.call_count == 1

        engine._judge_client.extract.reset_mock()
        second = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a_data, "model_b": model_b_data}
            ),
            document_name="judge_cache_doc",
        )
        assert second.summary["judge"]["attempted_calls"] == 0
        assert second.summary["judge"]["cache_hits"] >= 1
        assert engine._judge_client.extract.call_count == 0

    def test_mixed_text_fallback_matching_can_align_different_keys(self, temp_dir):
        """Mixed lanes can optionally use text fallback matching to reduce false missing rows."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = []
        mock.get_context_objects.return_value = []

        shared_detail = "Setback shall be 100 feet from the property line."
        model_a = {
            "requirements": [
                {
                    "category": "Setback",
                    "specific_subject": "property line",
                    "details": shared_detail,
                }
            ]
        }
        model_b = {
            "requirements": [
                {
                    "category": "Property lines distance",
                    "specific_subject": "from lot line",
                    "details": shared_detail,
                }
            ]
        }
        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["details"],
                "enable_text_fallback_matching": True,
            },
        )
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a, "model_b": model_b}
            ),
            document_name="mixed_fallback_doc",
        )

        missing_rows = [
            fc for fc in result.item_comparisons if "not extracted" in (fc.notes or "").lower()
        ]
        assert not missing_rows

    @pytest.mark.parametrize(
        ("case_id", "model_a", "model_b"),
        [
            (
                "measurement_shift_split_vs_inline",
                {"value": "50", "units": "feet"},
                {"value": "50 feet", "units": None},
            ),
            (
                "unit_synonyms_hours_vs_hrs",
                {"value": 24, "units": "hours"},
                {"value": 24, "units": "hrs"},
            ),
            (
                "time_formats_24h_vs_ampm",
                {"value": "07:00", "units": "HH:MM (24-hour)"},
                {"value": "7 a.m.", "units": "a.m./p.m."},
            ),
            (
                "time_ranges_ampm_vs_24h",
                {"value": "7 a.m. to 7 p.m.", "units": "hours"},
                {"value": "07:00-19:00", "units": "HH:MM (24-hour)"},
            ),
            (
                "time_ranges_with_and_phrasing",
                {"value": "7 a.m. and 7 p.m.", "units": "hours"},
                {"value": "07:00-19:00", "units": "HH:MM (24-hour)"},
            ),
        ],
    )
    def test_mixed_equivalent_value_unit_not_flagged(
        self, temp_dir, case_id, model_a, model_b
    ):
        """Semantically equivalent value/unit encodings must not be queued as conflicts.

        Covers split-vs-inline measurements, unit synonyms, and equivalent time
        formats/ranges across the two compared models under the mixed lane.
        """
        base = {"category": "Working hours", "specific_subject": "site preparation"}
        engine = ComparisonEngine(
            _build_schema_metadata_mock(),
            qa_qc_config={
                "comparison_approach": "mixed",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["value", "units"],
                "unit_equivalence_groups": [["hours", "hrs", "hr"]],
            },
        )
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir,
                {
                    "model_a": {"requirements": [{**base, **model_a}]},
                    "model_b": {"requirements": [{**base, **model_b}]},
                },
            ),
            document_name=f"mixed_{case_id}",
        )

        value_fc = next(fc for fc in result.item_comparisons if fc.field_path == "value")
        units_fc = next(fc for fc in result.item_comparisons if fc.field_path == "units")
        assert value_fc.needs_review is False
        assert units_fc.needs_review is False

    def test_mixed_judge_pair_matching_can_reduce_missing_rows(self, temp_dir):
        """Judge-assisted pair matching should align one-sided rows before field compare."""
        mock = MagicMock()
        mock.get_main_data_array.return_value = "requirements"
        mock.get_identifier_fields.return_value = []
        mock.get_context_objects.return_value = []

        model_a = {
            "requirements": [
                {
                    "category": "Noise",
                    "specific_subject": "drilling noise limit",
                    "value": 65,
                    "units": "dBA",
                    "requirement_description": "Noise shall not exceed 65 dBA near residences.",
                }
            ]
        }
        model_b = {
            "requirements": [
                {
                    "category": "Acoustic limit",
                    "specific_subject": "project noise cap",
                    "value": 65,
                    "units": "CNEL dB(A)",
                    "requirement_description": "Project noise is limited to 65 dB(A) near homes.",
                }
            ]
        }
        engine = ComparisonEngine(
            mock,
            qa_qc_config={
                                "comparison_approach": "mixed",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["value", "units"],
                "enable_text_fallback_matching": True,
                "enable_judge_pair_matching": True,
                "text_fallback_fields": [
                    "requirement_description",
                    "value",
                    "units",
                ],
                "judge": {
                    "enabled": True,
                    "apply_on": "all",
                    "min_confidence": "high",
                },
                "judge_runtime": {"model": "judge-model"},
            },
        )
        engine._judge_text_equivalence = MagicMock(
            return_value={
                "equivalent": True,
                "confidence": "high",
                "reason": "same requirement",
            }
        )
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a, "model_b": model_b}
            ),
            document_name="mixed_judge_pair_doc",
        )
        missing_rows = [
            fc for fc in result.item_comparisons if "not extracted" in (fc.notes or "").lower()
        ]
        assert not missing_rows

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
        
        engine = ComparisonEngine(mock_schema_metadata, qa_qc_config=self._BASE_QA_QC_CONFIG)
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
        engine = ComparisonEngine(mock_schema_metadata, qa_qc_config=self._BASE_QA_QC_CONFIG)
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
        engine = ComparisonEngine(mock_schema_metadata, qa_qc_config=self._BASE_QA_QC_CONFIG)
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
        
        mock_schema_metadata.get_context_objects.return_value = []
        engine = ComparisonEngine(mock_schema_metadata, qa_qc_config=self._BASE_QA_QC_CONFIG)
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a_data, "model_b": model_b_data}
            ),
            document_name="test_doc"
        )
        
        assert result.summary["total_comparisons"] == 0
        assert len(result.field_comparisons) == 0

    def test_get_flat_fields(self, mock_schema_metadata):
        """Test flattening nested dict to field paths."""
        engine = ComparisonEngine(mock_schema_metadata, qa_qc_config=self._BASE_QA_QC_CONFIG)
        
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
        engine = ComparisonEngine(mock_schema_metadata, qa_qc_config=self._BASE_QA_QC_CONFIG)
        
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
        engine = ComparisonEngine(mock_schema_metadata, qa_qc_config=self._BASE_QA_QC_CONFIG)
        result = engine.compare_outputs(
            output_files={"model_a": path_a, "model_b": path_b},
            document_name="test_doc"
        )
        
        # Check summary structure
        assert result.summary["comparison_approach"] == "numeric_only"
        assert "skipped_non_numeric" in result.summary
        assert "items_per_model" in result.summary

    def test_summary_includes_runtime_profile_metadata(self, mock_schema_metadata, temp_dir):
        """Runtime QA/QC profile metadata should flow into comparison summaries."""
        model_a_data = {"requirements": [
            {"category": "A", "specific_subject": "x", "section": "1", "value": 100}
        ]}
        model_b_data = {"requirements": [
            {"category": "A", "specific_subject": "x", "section": "1", "value": 100}
        ]}

        mock_schema_metadata.get_context_objects.return_value = []
        engine = ComparisonEngine(
            mock_schema_metadata,
            qa_qc_config={
                                "comparison_approach": "numeric_only",
                "match_fields": ["category", "specific_subject"],
                "compare_fields": ["value"],
            },
        )
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a_data, "model_b": model_b_data}
            ),
            document_name="test_doc"
        )

        assert "qaqc_mode" not in result.summary  # removed: was redundant with qaqc_profile


class TestComparisonEngineIntegration:
    """Integration tests using realistic data structure."""

    @pytest.fixture
    def geothermal_schema_metadata(self):
        """Create mock schema metadata matching geothermal schema (extraction contract only)."""
        return _build_schema_metadata_mock(
            main_data_array="requirements",
            identifier_fields=["jurisdiction.state", "jurisdiction.county"],
            deduplication_key_fields=[
                "category",
                "specific_subject",
                "applies_to",
                "section",
            ],
            context_objects=["jurisdiction"],
        )

    # QA/QC config for geothermal tests — lives in run config, not schema.
    _GEOTHERMAL_QA_QC_CONFIG = {
        "comparison_approach": "numeric_only",
        "match_fields": ["category", "applies_to"],
        "compare_fields": ["value", "unit"],
    }

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
        
        engine = ComparisonEngine(geothermal_schema_metadata, qa_qc_config=self._GEOTHERMAL_QA_QC_CONFIG)
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_qa_qc_dir, {"gpt-4o": model_a, "gpt-4.1": model_b}
            ),
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
        mock = _build_schema_metadata_mock(
            main_data_array="rate_schedules",
            identifier_fields=["utility_info.utility_name"],
            context_objects=["utility_info"],
        )

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

        engine = ComparisonEngine(
            mock,
            qa_qc_config={
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
            output_files=_write_output_files(
                temp_qa_qc_dir, {"gpt-4o": model_a, "gpt-4.1": model_b}
            ),
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
        """Create a mock SchemaMetadata (extraction contract only; no QA/QC in schema)."""
        return _build_schema_metadata_mock(
            main_data_array="requirements",
            identifier_fields=["source.state"],
            context_objects=["source"],
        )

    # QA/QC config for duplicate-detection tests — lives in run config.
    _DUP_QA_QC_CONFIG = {
        "comparison_approach": "numeric_only",
        "match_fields": ["requirement_type"],
        "compare_fields": ["value", "source_text"],
    }

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
        
        engine = ComparisonEngine(mock_schema_metadata_with_expected, qa_qc_config=self._DUP_QA_QC_CONFIG)
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a, "model_b": model_b}
            ),
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
        
        engine = ComparisonEngine(mock_schema_metadata_with_expected, qa_qc_config=self._DUP_QA_QC_CONFIG)
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_a, "model_b": model_b}
            ),
            document_name="Test Doc"
        )
        
        # No duplicates - values are different
        assert len(result.potential_duplicates) == 0


class TestCompletenessCalculation:
    """Tests for Phase 8 completeness calculation (compound key format)."""

    @pytest.fixture
    def mock_schema_with_expected(self):
        """Create mock (extraction contract only; expected_requirements in qa_qc_config)."""
        return _build_schema_metadata_mock(
            main_data_array="requirements",
            identifier_fields=["source.state"],
            context_objects=["source"],
        )

    # QA/QC config for completeness tests — expected_requirements live here, not in schema.
    _COMPLETENESS_QA_QC_CONFIG = {
        "comparison_approach": "numeric_only",
        "match_fields": ["requirement_type"],
        "compare_fields": ["value", "source_text"],
        "expected_requirements": [
            "setback__property_line_ft",
            "setback__residence_ft",
            "noise__at_property_line_dba",
        ],
    }

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
        
        engine = ComparisonEngine(mock_schema_with_expected, qa_qc_config=self._COMPLETENESS_QA_QC_CONFIG)
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_output, "model_b": model_output}
            ),
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
        
        engine = ComparisonEngine(mock_schema_with_expected, qa_qc_config=self._COMPLETENESS_QA_QC_CONFIG)
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_output, "model_b": model_output}
            ),
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
        
        engine = ComparisonEngine(mock_schema_with_expected, qa_qc_config=self._COMPLETENESS_QA_QC_CONFIG)
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_output, "model_b": model_output}
            ),
            document_name="Test Doc"
        )
        
        assert "completeness_per_model" in result.summary
        assert "model_a" in result.summary["completeness_per_model"]
        
    def test_no_expected_requirements(self, temp_dir):
        """Test behavior when no expected requirements are configured."""
        mock = _build_schema_metadata_mock(
            main_data_array="requirements",
            identifier_fields=["source.state"],
            context_objects=[],
        )
        no_expected_config = {
            **self._COMPLETENESS_QA_QC_CONFIG,
            "expected_requirements": [],
        }

        model_output = {
            "source": {"state": "CO"},
            "requirements": [
                {"requirement_type": "setback__property_line_ft", "value": 100, "source_text": "100 ft"},
            ]
        }

        engine = ComparisonEngine(mock, qa_qc_config=no_expected_config)
        result = engine.compare_outputs(
            output_files=_write_output_files(
                temp_dir, {"model_a": model_output, "model_b": model_output}
            ),
            document_name="Test Doc"
        )
        
        # With no expected requirements, completeness should be 100%
        comp = result.completeness["model_a"]
        assert comp.completeness_score == 1.0
        assert comp.expected_total == 0


# ── Tests for _extract_item_arrays and _project_item_array ───────────────────


class TestExtractItemArrays:
    """Tests for ComparisonEngine._extract_item_arrays.

    The existing test suite uses MagicMock for schema_metadata, which means
    extract_main_data_array() returns a MagicMock (not a list) and the
    fast-path branch (isinstance(items, list)) never fires. These tests use a
    mock that returns a real list so the fast-path is actually exercised.
    """

    _BASE_QA_QC_CONFIG = {
        "comparison_approach": "numeric_only",
        "match_fields": ["feature"],
        "compare_fields": ["value"],
    }

    def _make_metadata(self, main_array: str = "requirements") -> MagicMock:
        mock = MagicMock()
        mock.get_main_data_array.return_value = main_array
        mock.get_identifier_fields.return_value = []
        mock.get_context_objects.return_value = []
        return mock

    def test_fast_path_used_when_extract_method_returns_list(self):
        """When extract_main_data_array returns a list, the fast-path branch fires."""
        items = [{"feature": "noise", "value": 55}, {"feature": "setback", "value": 300}]
        meta = self._make_metadata()
        meta.extract_main_data_array.return_value = items

        engine = ComparisonEngine(meta, qa_qc_config=self._BASE_QA_QC_CONFIG)
        result = engine._extract_item_arrays({"model_a": {"requirements": items}})
        assert result["model_a"] == items

    def test_fallback_when_extract_method_not_callable(self):
        """When schema_metadata has no extract_main_data_array attr, fallback to dict key."""
        meta = self._make_metadata()
        del meta.extract_main_data_array  # simulate absent method

        items = [{"feature": "noise", "value": 55}]
        engine = ComparisonEngine(meta, qa_qc_config=self._BASE_QA_QC_CONFIG)
        result = engine._extract_item_arrays({"model_a": {"requirements": items}})
        assert result["model_a"] == items

    def test_fallback_when_extract_method_returns_non_list(self):
        """When extract_main_data_array returns a non-list, fall back to dict key lookup."""
        items = [{"feature": "fencing", "value": 6}]
        meta = self._make_metadata()
        meta.extract_main_data_array.return_value = "not-a-list"

        engine = ComparisonEngine(meta, qa_qc_config=self._BASE_QA_QC_CONFIG)
        result = engine._extract_item_arrays({"model_a": {"requirements": items}})
        # falls back to output.get(main_data_array)
        assert result["model_a"] == items

    def test_missing_key_returns_empty_list(self):
        meta = self._make_metadata()
        meta.extract_main_data_array.return_value = []

        engine = ComparisonEngine(meta, qa_qc_config=self._BASE_QA_QC_CONFIG)
        result = engine._extract_item_arrays({"model_a": {}})
        assert result["model_a"] == []


class TestProjectItemArray:
    """Tests for ComparisonEngine._project_item_array dict→list normalization.

    The projection path is active when qa_qc_config contains a 'projection'
    block. The dict→list branch normalizes object-maps emitted by some
    LLM providers (keyed "0", "1", ...) into ordered lists.
    """

    _PROJECTION_QA_QC_CONFIG = {
        "comparison_approach": "numeric_only",
        "match_fields": ["charge_type"],
        "compare_fields": ["value"],
        "projection": {
            "type": "nested_array_items",
            "source_array": "rate_schedules",
            "nested_array": "charges",
            "parent_fields": ["rate_name"],
        },
    }

    def _make_engine(self, main_array: str = "rate_schedules") -> ComparisonEngine:
        mock = MagicMock()
        mock.get_main_data_array.return_value = main_array
        mock.get_identifier_fields.return_value = []
        mock.get_context_objects.return_value = []
        return ComparisonEngine(mock, qa_qc_config=self._PROJECTION_QA_QC_CONFIG)

    def test_list_source_projected_correctly(self):
        """Normal list source projects parent fields into each child row."""
        engine = self._make_engine()
        output = {
            "rate_schedules": [
                {
                    "rate_name": "Schedule A",
                    "charges": [
                        {"charge_type": "customer", "value": 10},
                        {"charge_type": "energy",   "value": 0.05},
                    ],
                }
            ]
        }
        rows = engine._project_item_array(output)
        assert len(rows) == 2
        assert all(r["rate_name"] == "Schedule A" for r in rows)
        assert rows[0]["charge_type"] == "customer"
        assert rows[1]["charge_type"] == "energy"

    def test_dict_map_source_normalized_to_list(self):
        """Object-map sources (string-keyed "0","1",...) are sorted and normalized."""
        engine = self._make_engine()
        output = {
            "rate_schedules": {
                "1": {"rate_name": "Schedule B", "charges": [{"charge_type": "demand", "value": 5}]},
                "0": {"rate_name": "Schedule A", "charges": [{"charge_type": "energy", "value": 0.1}]},
            }
        }
        rows = engine._project_item_array(output)
        # "0" sorts before "1"
        assert len(rows) == 2
        assert rows[0]["rate_name"] == "Schedule A"
        assert rows[1]["rate_name"] == "Schedule B"

    def test_dict_map_non_numeric_keys_returns_empty(self):
        """Dict with non-numeric keys (not an object-map) returns empty list safely."""
        engine = self._make_engine()
        output = {
            "rate_schedules": {
                "residential": {"rate_name": "R1", "charges": []},
                "commercial":  {"rate_name": "C1", "charges": []},
            }
        }
        rows = engine._project_item_array(output)
        assert rows == []

    def test_unsupported_projection_type_returns_empty(self):
        """Unknown projection type logs a warning and returns empty list."""
        config = {**self._PROJECTION_QA_QC_CONFIG, "projection": {"type": "unknown_type"}}
        mock = MagicMock()
        mock.get_main_data_array.return_value = "rate_schedules"
        mock.get_identifier_fields.return_value = []
        mock.get_context_objects.return_value = []
        engine = ComparisonEngine(mock, qa_qc_config=config)
        assert engine._project_item_array({"rate_schedules": []}) == []

    def test_missing_nested_array_key_skips_parent(self):
        """Parents without the nested array key are silently skipped."""
        engine = self._make_engine()
        output = {
            "rate_schedules": [
                {"rate_name": "No charges parent"},  # no "charges" key
            ]
        }
        rows = engine._project_item_array(output)
        assert rows == []
