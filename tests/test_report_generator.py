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
                notes="Item missing from: gpt-5",
                present_models=["gpt-4.1"],
                missing_models=["gpt-5"],
            ),
        ]
        
        return ComparisonResult(
            document_name="Test Document",
            models=["gpt-4.1", "gpt-5"],
            summary={
                "qaqc_profile": "qualitative",
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
            assert summary_payload["summary"]["qaqc_profile"] == "qualitative"
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
        """CSV should expose reviewer queue columns."""
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
            
            assert "Divergence" in df.columns
            assert "Evidence" in df.columns
            assert "Why Flagged" in df.columns
            assert "Reviewer Verdict" in df.columns
            assert "Reviewer Notes" in df.columns
            assert "Judge Says" in df.columns
            # Lean queue: noisy diagnostics stay in All Items, not CSV queue.
            assert "Row ID" not in df.columns
            assert "Match Method" not in df.columns
            assert "Queue Signal" not in df.columns
            assert "Seen By" not in df.columns

    def test_csv_contains_agreement_info(self, generator, sample_comparison_result):
        """CSV queue should include only actionable rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            
            _, csv_path = generator.generate_report(
                sample_comparison_result, output_dir
            )
            
            df = pd.read_csv(csv_path)
            
            assert "Status" in df.columns
            assert "Divergence" in df.columns
            assert set(df["Divergence"].dropna()) <= {
                "presence_diff",
                "semantic_conflict",
                "field_conflict",
                "scope_variant",
                "judge_uncertain",
            }
            assert "Judge Says" in df.columns
            
            # Check that Status has valid values
            valid_statuses = ["AGREE", "DIFFER", "PARTIAL"]
            # Status might also include "ONLY model_name" patterns
            for status in df["Status"].dropna():
                assert any(s in status for s in valid_statuses + ["ONLY"])

    def test_queue_omits_measurement_shift_false_conflict(self, generator):
        """Normalized-equivalent measurement rows should not appear in reviewer queue."""
        result = ComparisonResult(
            document_name="Doc",
            models=["flash", "5-mini"],
            summary={"comparison_approach": "mixed"},
            item_comparisons=[
                FieldComparison(
                    item_id="setback | all facilities | fault trace",
                    field_path="value",
                    model_values={"flash": "50", "5-mini": "50 feet"},
                    agreement_score="2/2",
                    needs_review=False,
                    notes="normalized-equivalent measurement",
                    present_models=["flash", "5-mini"],
                    row_id="row-1",
                    match_method="semantic",
                ),
                FieldComparison(
                    item_id="setback | all facilities | fault trace",
                    field_path="units",
                    model_values={"flash": "feet", "5-mini": None},
                    agreement_score="2/2",
                    needs_review=False,
                    notes="normalized-equivalent measurement",
                    present_models=["flash", "5-mini"],
                    row_id="row-1",
                    match_method="semantic",
                ),
            ],
        )
        item_df = generator._build_item_centric_df(result)
        queue_df = generator._build_review_queue_df(item_df, include_missing=True)
        assert len(queue_df) == 0

    def test_item_centric_shows_diverging_field_values_when_primary_display_is_identical(
        self, generator
    ):
        """When display value is identical, use diverging non-measurement fields for clarity."""
        result = ComparisonResult(
            document_name="Doc",
            models=["flash", "5-mini"],
            summary={"comparison_approach": "mixed"},
            item_comparisons=[
                FieldComparison(
                    item_id="noise | all facilities geothermal | continuous level sound",
                    field_path="value",
                    model_values={"flash": 70, "5-mini": 70},
                    agreement_score="2/2",
                    needs_review=False,
                    present_models=["flash", "5-mini"],
                    row_id="row-2",
                    match_method="semantic",
                ),
                FieldComparison(
                    item_id="noise | all facilities geothermal | continuous level sound",
                    field_path="units",
                    model_values={"flash": "decibels", "5-mini": "decibels"},
                    agreement_score="2/2",
                    needs_review=False,
                    present_models=["flash", "5-mini"],
                    row_id="row-2",
                    match_method="semantic",
                ),
                FieldComparison(
                    item_id="noise | all facilities geothermal | continuous level sound",
                    field_path="obligation",
                    model_values={"flash": "shall", "5-mini": "must"},
                    agreement_score="1/2",
                    needs_review=True,
                    notes="2 different values",
                    present_models=["flash", "5-mini"],
                    row_id="row-2",
                    match_method="semantic",
                ),
            ],
        )
        df = generator._build_item_centric_df(result)
        assert len(df) == 1
        assert df.iloc[0]["flash"] == "obligation=shall"
        assert df.iloc[0]["5-mini"] == "obligation=must"

    def test_item_centric_shows_same_diverging_field_set_per_model(self, generator):
        """DIFFER rows should render the same diverging fields for each model."""
        result = ComparisonResult(
            document_name="Doc",
            models=["flash", "5-mini"],
            summary={"comparison_approach": "mixed"},
            item_comparisons=[
                FieldComparison(
                    item_id="hours | drilling | near residence",
                    field_path="value",
                    model_values={"flash": "7 a.m. to 7 p.m.", "5-mini": None},
                    agreement_score="1/2",
                    needs_review=True,
                    present_models=["flash", "5-mini"],
                    row_id="row-3",
                    match_method="semantic",
                ),
                FieldComparison(
                    item_id="hours | drilling | near residence",
                    field_path="units",
                    model_values={"flash": "hours", "5-mini": "HH:MM (24-hour)"},
                    agreement_score="1/2",
                    needs_review=True,
                    present_models=["flash", "5-mini"],
                    row_id="row-3",
                    match_method="semantic",
                ),
            ],
        )
        df = generator._build_item_centric_df(result)
        assert len(df) == 1
        assert df.iloc[0]["flash"] == "value=7 a.m. to 7 p.m.; units=hours"
        assert df.iloc[0]["5-mini"] == "value=∅; units=HH:MM (24-hour)"


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
        assert "QA/QC Profile" in metrics
        assert "QA/QC Mode" not in metrics  # removed: was redundant with qaqc_profile
        assert "Comparison Approach" in metrics
        assert "Total Comparisons" in metrics
        assert "Full Agreement %" in metrics

    def test_build_summary_df_includes_qaqc_metadata(self, generator):
        """Summary sheet should expose lane and comparison metadata for qualitative runs."""
        result = ComparisonResult(
            document_name="Qualitative Report",
            models=["model_a", "model_b"],
            summary={
                "qaqc_profile": "qualitative",
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
        assert values["QA/QC Profile"] == "qualitative"
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
                "qaqc_profile": "qualitative",
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
        assert "Divergence" in df.columns
        assert df["Divergence"].iloc[0] == "semantic_conflict"

    def test_item_centric_df_marks_scope_variant_rows(self, generator):
        result = ComparisonResult(
            document_name="Qualitative Scope Variant Doc",
            models=["gpt-4.1", "gpt-5"],
            summary={
                "qaqc_profile": "qualitative",
                "comparison_approach": "text_review",
                "qualitative_mismatch_breakdown": {
                    "top_scope_variant_requirements": [
                        {"label": "Other | Pipeline | pipeline siting and configuration", "count": 1}
                    ]
                },
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
        assert df["Divergence"].iloc[0] == "scope_variant"


class TestReviewQueueFallback:
    @pytest.fixture
    def generator(self):
        return ReportGenerator()

    def test_review_queue_does_not_fallback_to_missing_when_disabled(self, generator):
        df = pd.DataFrame(
            [
                {"Divergence": "missing", "Status": "ONLY a"},
                {"Divergence": "missing", "Status": "ONLY b"},
            ]
        )
        queue = generator._build_review_queue_df(df, include_missing=False)
        assert len(queue) == 0

    def test_item_centric_presence_uses_engine_presence_not_display(self, generator):
        result = ComparisonResult(
            document_name="Presence Regression",
            models=["model_a", "model_b"],
            summary={"comparison_approach": "mixed"},
            context_comparisons=[],
            item_comparisons=[
                FieldComparison(
                    item_id="Setback | Facility | Hospital",
                    field_path="obligation",
                    model_values={
                        "model_a": "prohibited",
                        "model_b": "prohibited",
                    },
                    agreement_score="2/2",
                    needs_review=False,
                    notes="",
                    present_models=["model_a", "model_b"],
                    missing_models=[],
                )
            ],
        )
        df = generator._build_item_centric_df(result)
        # Seen By removed; verify Status is populated and Why Flagged is clean
        assert df.loc[0, "Status"] in {"AGREE", "DIFFER", "PARTIAL"} or df.loc[0, "Status"].startswith("ONLY")
        assert "Missing in:" not in str(df.loc[0, "Why Flagged"])


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


class TestDivergenceFillColor:
    """Tests for row divergence color mapping."""

    @pytest.fixture
    def generator(self):
        return ReportGenerator()

    @pytest.mark.parametrize(
        ("divergence", "expected"),
        [
            ("aligned", "full_agreement"),
            ("semantic_conflict", "partial_agreement"),
            ("field_conflict", "disagreement"),
            ("presence_diff", "missing"),
            ("scope_variant", "scope_variant"),
            ("judge_uncertain", "judge_uncertain"),
            ("unknown", None),
        ],
    )
    def test_divergence_fill_color(self, generator, divergence, expected):
        color = generator._divergence_fill_color(divergence)
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

    def test_excel_has_reviewer_first_sheets(self, generator, sample_result):
        """Workbook should expose dashboard + review queue architecture."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            
            excel_path, _ = generator.generate_report(sample_result, output_dir)
            
            # Read Excel to check sheets
            xlsx = pd.ExcelFile(excel_path)
            assert "Dashboard" in xlsx.sheet_names
            assert "Review Queue" in xlsx.sheet_names
            assert "All Items" in xlsx.sheet_names
            assert "Model Reliability" in xlsx.sheet_names
            assert "Run Manifest" in xlsx.sheet_names

    def test_excel_dashboard_sheet_content(self, generator, sample_result):
        """Dashboard should include document and compared models."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            
            excel_path, _ = generator.generate_report(sample_result, output_dir)
            
            df = pd.read_excel(excel_path, sheet_name="Dashboard")
            
            assert "Excel Test" in df["Value"].values
            assert "model_a, model_b" in df["Value"].values

    def test_dashboard_legend_only_includes_present_divergence_types(self, generator):
        result = ComparisonResult(
            document_name="Legend Test",
            models=["model_a", "model_b"],
            summary={"comparison_approach": "mixed"},
            item_comparisons=[
                FieldComparison(
                    item_id="noise | operator",
                    field_path="value",
                    model_values={"model_a": "50", "model_b": "55"},
                    agreement_score="1/2",
                    needs_review=True,
                    notes="2 different values",
                    row_id="row-1",
                )
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            excel_path, _ = generator.generate_report(result, output_dir)
            df = pd.read_excel(excel_path, sheet_name="Dashboard")
            legend_metrics = set(df[df["Section"] == "Legend"]["Metric"].tolist())
            assert "field_conflict" in legend_metrics
            assert "semantic_conflict" not in legend_metrics
            assert "scope_variant" not in legend_metrics


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
