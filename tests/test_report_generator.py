"""
Tests for QA/QC Report Generator.

Tests the report generation functionality for multi-model comparison results.
"""

import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from streamline_extract.qa_qc.comparison_engine import ComparisonResult, FieldComparison
from streamline_extract.qa_qc.report_generator import ReportGenerator


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
        assert "Total Comparisons" in metrics
        assert "Full Agreement %" in metrics


class TestBuildComparisonDf:
    """Tests for _build_comparison_df method."""

    @pytest.fixture
    def generator(self):
        return ReportGenerator()

    def test_build_comparison_df_empty_list(self, generator):
        """Test that empty comparison list returns empty DataFrame."""
        df = generator._build_comparison_df([], ["model_a"])
        assert df.empty

    def test_build_comparison_df_has_model_columns(self, generator):
        """Test that comparison DataFrame has model columns."""
        comparisons = [
            FieldComparison(
                item_id="test",
                field_path="field",
                model_values={"model_a": "value_a", "model_b": "value_b"},
                agreement_score="1/2",
                needs_review=True,
                notes="test"
            )
        ]
        
        df = generator._build_comparison_df(comparisons, ["model_a", "model_b"])
        
        assert "Model: model_a" in df.columns
        assert "Model: model_b" in df.columns
        assert df["Model: model_a"].iloc[0] == "value_a"
        assert df["Model: model_b"].iloc[0] == "value_b"


class TestTruncateValue:
    """Tests for _truncate_value method."""

    @pytest.fixture
    def generator(self):
        return ReportGenerator()

    def test_truncate_value_short_string(self, generator):
        """Test that short strings are not truncated."""
        result = generator._truncate_value("short string")
        assert result == "short string"

    def test_truncate_value_long_string(self, generator):
        """Test that long strings are truncated."""
        long_string = "a" * 300
        result = generator._truncate_value(long_string)
        assert len(result) == 200
        assert result.endswith("...")

    def test_truncate_value_none(self, generator):
        """Test that None returns empty string."""
        result = generator._truncate_value(None)
        assert result == ""

    def test_truncate_value_number(self, generator):
        """Test that numbers are converted to strings."""
        result = generator._truncate_value(12345)
        assert result == "12345"


class TestGetRowColor:
    """Tests for _get_row_color method."""

    @pytest.fixture
    def generator(self):
        return ReportGenerator()

    def test_full_agreement_green(self, generator):
        """Test that full agreement returns green."""
        color = generator._get_row_color("2/2", "", 2)
        assert color == generator.COLORS["full_agreement"]
        
        color = generator._get_row_color("3/3", "", 3)
        assert color == generator.COLORS["full_agreement"]

    def test_partial_agreement_yellow(self, generator):
        """Test that partial agreement (>50%) returns yellow."""
        color = generator._get_row_color("2/3", "", 3)
        assert color == generator.COLORS["partial_agreement"]

    def test_low_agreement_red(self, generator):
        """Test that low agreement (≤50%) returns red."""
        color = generator._get_row_color("1/3", "", 3)
        assert color == generator.COLORS["disagreement"]
        
        color = generator._get_row_color("1/2", "", 2)
        assert color == generator.COLORS["disagreement"]

    def test_missing_gray(self, generator):
        """Test that missing items return gray."""
        color = generator._get_row_color("1/2", "Item missing from: model_a", 2)
        assert color == generator.COLORS["missing"]

    def test_invalid_agreement_no_color(self, generator):
        """Test that invalid agreement returns empty string."""
        color = generator._get_row_color("invalid", "", 2)
        assert color == ""
        
        color = generator._get_row_color("", "", 2)
        assert color == ""


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
