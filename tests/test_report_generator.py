"""
Tests for QA/QC Report Generator.

Tests the report generation functionality for multi-model comparison results.
"""

import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from psweep.qa_qc.comparison_engine import ComparisonResult, FieldComparison
from psweep.qa_qc.report_generator import ReportGenerator


class TestReportGenerator:
    """Tests for ReportGenerator class."""

    @pytest.fixture
    def generator(self):
        """Create a ReportGenerator instance."""
        return ReportGenerator()

    @pytest.fixture
    def sample_comparison_result(self):
        """Create a sample ComparisonResult for testing."""
        context_comparisons = [
            FieldComparison(
                item_id="[jurisdiction]",
                field_path="county",
                model_values={"gpt-4.1": "Chaffee County", "gpt-5": "Chaffee County"},
                agreement_score="2/2",
                needs_review=False,
                notes=""
            ),
            FieldComparison(
                item_id="[jurisdiction]",
                field_path="state",
                model_values={"gpt-4.1": "Colorado", "gpt-5": "CO"},
                agreement_score="1/2",
                needs_review=True,
                notes="2 different values"
            ),
        ]
        
        item_comparisons = [
            FieldComparison(
                item_id="Permit required | Commercial Use",
                field_path="category",
                model_values={"gpt-4.1": "Permit required", "gpt-5": "Permit required"},
                agreement_score="2/2",
                needs_review=False,
                notes=""
            ),
            FieldComparison(
                item_id="Permit required | Commercial Use",
                field_path="value",
                model_values={"gpt-4.1": "Required", "gpt-5": "Mandatory"},
                agreement_score="1/2",
                needs_review=True,
                notes="2 different values"
            ),
            FieldComparison(
                item_id="Setback | Residential",
                field_path="_item_presence",
                model_values={"gpt-4.1": "PRESENT", "gpt-5": "MISSING"},
                agreement_score="1/2",
                needs_review=True,
                notes="Item missing from: gpt-5"
            ),
        ]
        
        return ComparisonResult(
            document_name="Test Document",
            models=["gpt-4.1", "gpt-5"],
            summary={
                "qaqc_lane": "qualitative",
                "qaqc_mode": "qualitative",
                "comparison_approach": "text_review",
                "review_category_counts": {"aligned": 1, "missing_item": 1, "scope_variant": 1, "text_difference": 1},
                "qualitative_mismatch_breakdown": {
                    "missing_item_by_category": [{"label": "Setback", "count": 1}],
                    "scope_variant_by_category": [{"label": "Other", "count": 1}],
                    "text_difference_by_category": [{"label": "Permit required", "count": 1}],
                    "top_missing_requirements": [{"label": "Setback | Residential", "count": 1}],
                    "top_scope_variant_requirements": [{"label": "Other | Pipeline | pipeline siting and configuration", "count": 1}],
                    "top_text_difference_requirements": [{"label": "Permit required | Commercial Use", "count": 1}],
                },
                "qualitative_advisory_gate": {
                    "mode": "advisory",
                    "status": "warn",
                    "aligned_pct": 33.3,
                    "missing_item_pct": 33.3,
                    "dominant_category": "aligned",
                    "evaluated_comparisons": 3,
                    "excluded_scope_variants": 1,
                    "recommended_action": "Inspect text differences before relying on this run.",
                },
                "total_items_per_model": {"gpt-4.1": 10, "gpt-5": 8},
                "total_comparisons": 5,
                "full_agreement_count": 2,
                "full_agreement_pct": 40.0,
                "needs_review_count": 3,
                "needs_review_pct": 60.0,
                "context_comparisons": 2,
                "context_agreement_pct": 50.0,
                "item_comparisons": 3,
                "item_agreement_pct": 33.3,
            },
            context_comparisons=context_comparisons,
            item_comparisons=item_comparisons,
        )

    def test_generate_report_creates_files(self, generator, sample_comparison_result):
        """Test that generate_report creates both Excel and CSV files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            
            excel_path, csv_path = generator.generate_report(
                sample_comparison_result, output_dir
            )
            
            assert excel_path.exists()
            assert csv_path.exists()
            assert excel_path.name == "comparison_report.xlsx"
            assert csv_path.name == "comparison_report.csv"
            assert (output_dir / "comparison_summary.json").exists()

    def test_generate_report_writes_machine_readable_summary(self, generator, sample_comparison_result):
        """Generate a stable JSON summary alongside report artifacts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)

            generator.generate_report(sample_comparison_result, output_dir)

            summary_payload = json.loads((output_dir / "comparison_summary.json").read_text(encoding="utf-8"))

            assert summary_payload["document_name"] == "Test Document"
            assert summary_payload["summary"]["qaqc_lane"] == "qualitative"
            assert summary_payload["summary"]["qualitative_advisory_gate"]["status"] == "warn"

    def test_generate_report_creates_output_dir(self, generator, sample_comparison_result):
        """Test that generate_report creates output directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "nested" / "path"
            
            excel_path, csv_path = generator.generate_report(
                sample_comparison_result, output_dir
            )
            
            assert output_dir.exists()
            assert excel_path.exists()

    def test_csv_contains_all_comparisons(self, generator, sample_comparison_result):
        """Test that CSV contains item-centric comparisons (one row per item)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            
            _, csv_path = generator.generate_report(
                sample_comparison_result, output_dir
            )
            
            df = pd.read_csv(csv_path)
            
            # Item-centric format: each item is one row (fewer rows than field-by-field)
            # Should have at least 1 row for items
            assert len(df) >= 1
            
            # Should have Status column (new format)
            assert "Status" in df.columns
            
            # Should have Requirement column (compound key format)
            assert "Requirement" in df.columns
            assert "QA/QC Lane" in df.columns
            assert "Comparison Approach" in df.columns
            assert "Review Category" in df.columns

    def test_csv_contains_agreement_info(self, generator, sample_comparison_result):
        """Test that CSV contains agreement info in new format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            
            _, csv_path = generator.generate_report(
                sample_comparison_result, output_dir
            )
            
            df = pd.read_csv(csv_path)
            
            # New format columns
            assert "Agreement" in df.columns
            assert "Notes" in df.columns
            assert "Status" in df.columns
            assert "QA/QC Lane" in df.columns
            assert "Comparison Approach" in df.columns
            assert "Review Category" in df.columns

            assert set(df["QA/QC Lane"].dropna()) == {"qualitative"}
            assert set(df["Comparison Approach"].dropna()) == {"text_review"}
            assert set(df["Review Category"].dropna()) <= {
                "aligned",
                "missing_item",
                "scope_variant",
                "text_difference",
                "value_difference",
            }
            
            # Check that Status has valid values
            valid_statuses = ["AGREE", "DIFFER", "PARTIAL"]
            # Status might also include "ONLY model_name" patterns
            for status in df["Status"].dropna():
                assert any(s in status for s in valid_statuses + ["ONLY"])


class TestBuildSummaryDf:
    """Tests for _build_summary_df method."""

    @pytest.fixture
    def generator(self):
        return ReportGenerator()

    def test_build_summary_df_has_required_metrics(self, generator):
        """Test that summary DataFrame has required metrics."""
        result = ComparisonResult(
            document_name="Test Doc",
            models=["model_a", "model_b"],
            summary={
                "total_items_per_model": {"model_a": 5, "model_b": 4},
                "total_comparisons": 20,
                "full_agreement_count": 15,
                "full_agreement_pct": 75.0,
                "needs_review_count": 5,
                "needs_review_pct": 25.0,
                "context_comparisons": 5,
                "context_agreement_pct": 80.0,
                "item_comparisons": 15,
                "item_agreement_pct": 73.3,
            },
            context_comparisons=[],
            item_comparisons=[],
        )
        
        df = generator._build_summary_df(result)
        
        # Check required columns
        assert "Metric" in df.columns
        assert "Value" in df.columns
        
        # Check required metrics
        metrics = df["Metric"].tolist()
        assert "Document" in metrics
        assert "Models" in metrics
        assert "QA/QC Lane" in metrics
        assert "QA/QC Mode" in metrics
        assert "Comparison Approach" in metrics
        assert "Total Comparisons" in metrics
        assert "Full Agreement %" in metrics

    def test_build_summary_df_includes_qaqc_metadata(self, generator):
        """Summary sheet should expose lane and comparison metadata for qualitative runs."""
        result = ComparisonResult(
            document_name="Qualitative Report",
            models=["model_a", "model_b"],
            summary={
                "qaqc_lane": "qualitative",
                "qaqc_mode": "qualitative",
                "comparison_approach": "text_review",
                "review_category_counts": {"text_difference": 1},
                "qualitative_mismatch_breakdown": {
                    "missing_item_by_category": [{"label": "setback", "count": 1}],
                    "scope_variant_by_category": [{"label": "other", "count": 1}],
                    "text_difference_by_category": [{"label": "permit required", "count": 1}],
                    "top_missing_requirements": [{"label": "setback | residential", "count": 1}],
                    "top_scope_variant_requirements": [{"label": "other | pipeline | pipeline siting and configuration", "count": 1}],
                    "top_text_difference_requirements": [{"label": "permit required | commercial use", "count": 1}],
                },
                "qualitative_advisory_gate": {
                    "mode": "advisory",
                    "status": "fail",
                    "aligned_pct": 0.0,
                    "missing_item_pct": 0.0,
                    "dominant_category": "text_difference",
                    "evaluated_comparisons": 1,
                    "excluded_scope_variants": 1,
                    "recommended_action": "Treat this run as not ready for qualitative benchmark gating.",
                },
                "total_items_per_model": {"model_a": 1, "model_b": 1},
                "total_comparisons": 1,
                "full_agreement_count": 0,
                "full_agreement_pct": 0.0,
                "needs_review_count": 1,
                "needs_review_pct": 100.0,
                "context_comparisons": 0,
                "context_agreement_pct": 0.0,
                "item_comparisons": 1,
                "item_agreement_pct": 0.0,
            },
            context_comparisons=[],
            item_comparisons=[],
        )

        df = generator._build_summary_df(result)
        values = dict(zip(df["Metric"], df["Value"]))
        assert values["QA/QC Lane"] == "qualitative"
        assert values["QA/QC Mode"] == "qualitative"
        assert values["Comparison Approach"] == "text_review"
        assert values["Review Category: text_difference"] == 1
        assert values["Qualitative Gate Mode"] == "advisory"
        assert values["Qualitative Gate Status"] == "fail"
        assert values["Qualitative Aligned %"] == "0.0%"
        assert values["Qualitative Dominant Category"] == "text_difference"
        assert values["Qualitative Evaluated Comparisons"] == 1
        assert values["Qualitative Excluded Scope Variants"] == 1
        assert values["Top Missing-Item Categories"] == "setback: 1"
        assert values["Top Scope-Variant Categories"] == "other: 1"
        assert values["Top Text-Difference Categories"] == "permit required: 1"


class TestQualitativeReportLabels:
    @pytest.fixture
    def generator(self):
        return ReportGenerator()

    def test_item_centric_df_includes_qualitative_labels(self, generator):
        result = ComparisonResult(
            document_name="Qualitative Doc",
            models=["gpt-4.1", "gpt-5"],
            summary={
                "qaqc_lane": "qualitative",
                "qaqc_mode": "qualitative",
                "comparison_approach": "text_review",
            },
            context_comparisons=[],
            item_comparisons=[
                FieldComparison(
                    item_id="Permit required | Commercial Use",
                    field_path="details",
                    model_values={
                        "gpt-4.1": "Permit required before operations begin",
                        "gpt-5": "Permit required before drilling begins",
                    },
                    agreement_score="1/2",
                    needs_review=True,
                    notes="2 different values",
                )
            ],
        )

        df = generator._build_item_centric_df(result)
        assert "QA/QC Lane" in df.columns
        assert "Comparison Approach" in df.columns
        assert "Review Category" in df.columns
        assert df["QA/QC Lane"].iloc[0] == "qualitative"
        assert df["Comparison Approach"].iloc[0] == "text_review"
        assert df["Review Category"].iloc[0] == "text_difference"

    def test_item_centric_df_marks_scope_variant_rows(self, generator):
        result = ComparisonResult(
            document_name="Qualitative Scope Variant Doc",
            models=["gpt-4.1", "gpt-5"],
            summary={
                "qaqc_lane": "qualitative",
                "qaqc_mode": "qualitative",
                "comparison_approach": "text_review",
            },
            context_comparisons=[],
            item_comparisons=[
                FieldComparison(
                    item_id="Other | Pipeline | pipeline siting and configuration",
                    field_path="_item_presence",
                    model_values={
                        "gpt-4.1": None,
                        "gpt-5": "PRESENT",
                    },
                    agreement_score="1/2",
                    needs_review=True,
                    notes="Item not extracted by: gpt-4.1",
                )
            ],
        )

        df = generator._build_item_centric_df(result)
        assert df["Review Category"].iloc[0] == "scope_variant"


class TestTruncateValue:
    """Tests for _truncate_value method."""

    @pytest.fixture
    def generator(self):
        return ReportGenerator()

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("short string", "short string"),
            (None, ""),
            (12345, "12345"),
        ],
    )
    def test_truncate_value_non_truncating_cases(self, generator, value, expected):
        assert generator._truncate_value(value) == expected

    def test_truncate_value_long_string(self, generator):
        long_string = "a" * 300
        result = generator._truncate_value(long_string)
        assert len(result) == 200
        assert result.endswith("...")


class TestGetRowColor:
    """Tests for _get_row_color method."""

    @pytest.fixture
    def generator(self):
        return ReportGenerator()

    @pytest.mark.parametrize(
        ("agreement_score", "notes", "num_models", "expected"),
        [
            ("2/2", "", 2, "full_agreement"),
            ("3/3", "", 3, "full_agreement"),
            ("2/3", "", 3, "partial_agreement"),
            ("1/3", "", 3, "disagreement"),
            ("1/2", "", 2, "disagreement"),
            ("1/2", "Item missing from: model_a", 2, "missing"),
            ("invalid", "", 2, None),
            ("", "", 2, None),
        ],
    )
    def test_get_row_color(self, generator, agreement_score, notes, num_models, expected):
        color = generator._get_row_color(agreement_score, notes, num_models)
        if expected is None:
            assert color == ""
            return
        assert color == generator.COLORS[expected]


class TestExcelGeneration:
    """Tests for Excel-specific generation."""

    @pytest.fixture
    def generator(self):
        return ReportGenerator()

    @pytest.fixture
    def sample_result(self):
        """Create a minimal sample result."""
        return ComparisonResult(
            document_name="Excel Test",
            models=["model_a", "model_b"],
            summary={
                "total_items_per_model": {"model_a": 2, "model_b": 2},
                "total_comparisons": 2,
                "full_agreement_count": 1,
                "full_agreement_pct": 50.0,
                "needs_review_count": 1,
                "needs_review_pct": 50.0,
                "context_comparisons": 1,
                "context_agreement_pct": 100.0,
                "item_comparisons": 1,
                "item_agreement_pct": 0.0,
            },
            context_comparisons=[
                FieldComparison(
                    item_id="[context]",
                    field_path="field1",
                    model_values={"model_a": "same", "model_b": "same"},
                    agreement_score="2/2",
                    needs_review=False,
                    notes=""
                )
            ],
            item_comparisons=[
                FieldComparison(
                    item_id="item1",
                    field_path="field2",
                    model_values={"model_a": "value_a", "model_b": "value_b"},
                    agreement_score="1/2",
                    needs_review=True,
                    notes="2 different values"
                )
            ],
        )

    def test_excel_has_two_sheets(self, generator, sample_result):
        """Test that Excel file has Summary and Item Comparison sheets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            
            excel_path, _ = generator.generate_report(sample_result, output_dir)
            
            # Read Excel to check sheets
            xlsx = pd.ExcelFile(excel_path)
            assert "Summary" in xlsx.sheet_names
            assert "Item Comparison" in xlsx.sheet_names
            # Note: Context Comparison removed in favor of item-centric format

    def test_excel_summary_sheet_content(self, generator, sample_result):
        """Test that Summary sheet has correct content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            
            excel_path, _ = generator.generate_report(sample_result, output_dir)
            
            df = pd.read_excel(excel_path, sheet_name="Summary")
            
            # Check document name is present
            assert "Excel Test" in df["Value"].values
            
            # Check models are present
            assert "model_a, model_b" in df["Value"].values


class TestEmptyComparisons:
    """Tests for edge cases with empty comparisons."""

    @pytest.fixture
    def generator(self):
        return ReportGenerator()

    def test_empty_context_comparisons(self, generator):
        """Test handling of empty context comparisons."""
        result = ComparisonResult(
            document_name="Empty Context",
            models=["model_a"],
            summary={
                "total_items_per_model": {},
                "total_comparisons": 0,
                "full_agreement_count": 0,
                "full_agreement_pct": 0.0,
                "needs_review_count": 0,
                "needs_review_pct": 0.0,
                "context_comparisons": 0,
                "context_agreement_pct": 0.0,
                "item_comparisons": 0,
                "item_agreement_pct": 0.0,
            },
            context_comparisons=[],
            item_comparisons=[],
        )
        
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            
            excel_path, csv_path = generator.generate_report(result, output_dir)
            
            assert excel_path.exists()
            assert csv_path.exists()
            
            # CSV should still have headers
            df = pd.read_csv(csv_path)
            assert len(df) == 0  # No data rows
