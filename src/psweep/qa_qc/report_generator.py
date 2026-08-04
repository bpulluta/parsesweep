"""
Report Generator for QA/QC Multi-Model Validation.

Generates Excel/CSV reports with color-coded comparison results
for human review.

Usage:
    from psweep.qa_qc.report_generator import ReportGenerator
    from psweep.qa_qc.comparison_engine import ComparisonResult

    generator = ReportGenerator()
    excel_path, csv_path = generator.generate_report(
        comparison_result=result,
        output_dir=Path("processed/qa_qc/austin_energy")
    )

Output Files:
    processed/qa_qc/{doc_name}/
        comparison_report.xlsx  # Color-coded Excel
        comparison_report.csv   # Plain CSV

Color Coding:
    - Green: Full agreement (N/N)
    - Yellow: Partial agreement (>50%)
    - Red: Low agreement (≤50%) or disagreement
    - Gray: Item missing from one or more models
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from openpyxl import load_workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .comparison_engine import ComparisonResult, FieldComparison

logger = logging.getLogger(__name__)

# Maximum length for cell values before truncation
MAX_VALUE_LENGTH = 200


class ReportGenerator:
    """
    Generate comparison reports in Excel/CSV format.

    Creates actionable reports for human review of multi-model QA/QC results.
    Excel reports include color-coded rows based on agreement level.
    """

    # Color scheme for Excel formatting
    COLORS = {
        "full_agreement": "C6EFCE",  # Light green
        "partial_agreement": "FFEB9C",  # Light yellow
        "disagreement": "FFC7CE",  # Light red
        "missing": "D9D9D9",  # Gray
        "judge_uncertain": "D9E1F2",  # Light blue
        "scope_variant": "E2EFDA",  # Light muted green
    }
    REVIEWER_VERDICTS = [
        "accept-model-a",
        "accept-model-b",
        "accept-model-c",
    ]

    def generate_report(
        self,
        comparison_result: ComparisonResult,
        output_dir: Path,
        run_metadata: Optional[Dict[str, Any]] = None,
        include_csv: bool = True,
        include_missing_in_queue: bool = False,
        include_low_signal_presence_in_queue: bool = False,
    ) -> Tuple[Path, Path]:
        """
        Generate Excel and CSV reports.

        Args:
            comparison_result: ComparisonResult from comparison engine
            output_dir: Directory to save reports

        Returns:
            Tuple of (excel_path, csv_path)
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        excel_path = output_dir / "comparison_report.xlsx"
        csv_path = output_dir / "comparison_report.csv"
        summary_path = output_dir / "comparison_summary.json"

        # Generate Excel with formatting
        self._generate_excel(
            comparison_result,
            excel_path,
            run_metadata=run_metadata,
            include_missing_in_queue=include_missing_in_queue,
            include_low_signal_presence_in_queue=include_low_signal_presence_in_queue,
        )
        logger.info(f"Generated Excel report: {excel_path}")

        if include_csv:
            self._generate_item_centric_csv(
                comparison_result,
                csv_path,
                include_missing_in_queue=include_missing_in_queue,
                include_low_signal_presence_in_queue=include_low_signal_presence_in_queue,
            )
            logger.info(f"Generated CSV report: {csv_path}")
        elif csv_path.exists():
            csv_path.unlink()

        self._write_summary_json(comparison_result, summary_path)
        logger.info(f"Generated summary report: {summary_path}")

        return excel_path, csv_path

    def _write_summary_json(
        self, result: ComparisonResult, output_path: Path
    ) -> None:
        """Persist a machine-readable comparison summary for benchmark gating."""
        payload = {
            "document_name": result.document_name,
            "models": result.models,
            "summary": result.summary,
        }
        output_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _generate_excel(
        self,
        result: ComparisonResult,
        output_path: Path,
        run_metadata: Optional[Dict[str, Any]] = None,
        include_missing_in_queue: bool = False,
        include_low_signal_presence_in_queue: bool = False,
    ) -> None:
        """Generate a reviewer-first workbook with explicit queue, diagnostics, and manifest."""
        all_items_df = self._build_item_centric_df(result)
        review_queue_df = self._build_review_queue_df(
            all_items_df,
            include_missing=include_missing_in_queue,
            include_low_signal_presence=include_low_signal_presence_in_queue,
        )
        dashboard_df = self._build_dashboard_df(
            result=result,
            all_items_df=all_items_df,
            review_queue_df=review_queue_df,
            run_metadata=run_metadata,
        )
        reliability_df = self._build_model_reliability_df(
            result=result,
            all_items_df=all_items_df,
            run_metadata=run_metadata,
        )
        manifest_df = self._build_run_manifest_df(
            result=result,
            run_metadata=run_metadata,
        )

        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            dashboard_df.to_excel(writer, sheet_name="Dashboard", index=False)
            review_queue_df.to_excel(writer, sheet_name="Review Queue", index=False)
            all_items_df.to_excel(writer, sheet_name="All Items", index=False)
            reliability_df.to_excel(writer, sheet_name="Model Reliability", index=False)
            manifest_df.to_excel(writer, sheet_name="Run Manifest", index=False)

        # Apply formatting and append the reviewer-authoritative Final Verdicts sheet
        self._apply_workbook_formatting(
            output_path, result=result, all_items_df=all_items_df
        )

    def _build_summary_df(self, result: ComparisonResult) -> pd.DataFrame:
        """Build summary metrics DataFrame."""
        summary = result.summary

        rows = [
            {"Metric": "Document", "Value": result.document_name},
            {"Metric": "Models", "Value": ", ".join(result.models)},
            {
                "Metric": "QA/QC Profile",
                "Value": summary.get("qaqc_profile") or "unknown",
            },
            {
                "Metric": "Comparison Approach",
                "Value": summary.get("comparison_approach") or "mixed",
            },
        ]

        # Add items per model
        items_per_model = summary.get("items_per_model", {})
        for model, count in items_per_model.items():
            rows.append({"Metric": f"Total Items ({model})", "Value": count})

        rows.extend(
            [
                {
                    "Metric": "Item Rows (full table)",
                    "Value": len(self._build_item_centric_df(result)),
                },
                {
                    "Metric": "Total Comparisons",
                    "Value": summary.get("total_comparisons", 0),
                },
                {
                    "Metric": "Full Agreement Count",
                    "Value": summary.get("full_agreement_count", 0),
                },
                {
                    "Metric": "Full Agreement %",
                    "Value": f"{summary.get('full_agreement_pct', 0):.1f}%",
                },
                {
                    "Metric": "Needs Review Count",
                    "Value": summary.get("needs_review_count", 0),
                },
                {
                    "Metric": "Needs Review %",
                    "Value": f"{summary.get('needs_review_pct', 0):.1f}%",
                },
                {"Metric": "", "Value": ""},  # Blank row
                {
                    "Metric": "Context Comparisons",
                    "Value": summary.get("context_comparisons", 0),
                },
                {
                    "Metric": "Context Agreement %",
                    "Value": f"{summary.get('context_agreement_pct', 0):.1f}%",
                },
                {
                    "Metric": "Item Comparisons",
                    "Value": summary.get("item_comparisons", 0),
                },
                {
                    "Metric": "Item Agreement %",
                    "Value": f"{summary.get('item_agreement_pct', 0):.1f}%",
                },
            ]
        )

        review_category_counts = summary.get("review_category_counts") or {}
        if review_category_counts:
            rows.extend(
                [
                    {"Metric": "", "Value": ""},
                    {"Metric": "--- Review Categories ---", "Value": ""},
                ]
            )
            for category_name, count in sorted(review_category_counts.items()):
                rows.append(
                    {
                        "Metric": f"Review Category: {category_name}",
                        "Value": count,
                    }
                )

        qualitative_gate = summary.get("qualitative_advisory_gate") or {}
        if qualitative_gate:
            rows.extend(
                [
                    {"Metric": "", "Value": ""},
                    {
                        "Metric": "--- Qualitative Advisory Gate ---",
                        "Value": "",
                    },
                    {
                        "Metric": "Qualitative Gate Mode",
                        "Value": qualitative_gate.get("mode", "advisory"),
                    },
                    {
                        "Metric": "Qualitative Gate Status",
                        "Value": qualitative_gate.get(
                            "status", "not_applicable"
                        ),
                    },
                    {
                        "Metric": "Qualitative Aligned %",
                        "Value": f"{qualitative_gate.get('aligned_pct', 0):.1f}%",
                    },
                    {
                        "Metric": "Qualitative Missing Item %",
                        "Value": f"{qualitative_gate.get('missing_item_pct', 0):.1f}%",
                    },
                    {
                        "Metric": "Qualitative Dominant Category",
                        "Value": qualitative_gate.get(
                            "dominant_category", "none"
                        ),
                    },
                    {
                        "Metric": "Qualitative Evaluated Comparisons",
                        "Value": qualitative_gate.get(
                            "evaluated_comparisons", 0
                        ),
                    },
                    {
                        "Metric": "Qualitative Excluded Scope Variants",
                        "Value": qualitative_gate.get(
                            "excluded_scope_variants", 0
                        ),
                    },
                    {
                        "Metric": "Qualitative Recommended Action",
                        "Value": qualitative_gate.get(
                            "recommended_action", ""
                        ),
                    },
                ]
            )

        qualitative_breakdown = (
            summary.get("qualitative_mismatch_breakdown") or {}
        )
        if qualitative_breakdown:
            rows.extend(
                [
                    {"Metric": "", "Value": ""},
                    {
                        "Metric": "--- Qualitative Mismatch Breakdown ---",
                        "Value": "",
                    },
                ]
            )
            self._append_breakdown_rows(
                rows,
                "Top Missing-Item Categories",
                qualitative_breakdown.get("missing_item_by_category") or [],
            )
            self._append_breakdown_rows(
                rows,
                "Top Scope-Variant Categories",
                qualitative_breakdown.get("scope_variant_by_category") or [],
            )
            self._append_breakdown_rows(
                rows,
                "Top Text-Difference Categories",
                qualitative_breakdown.get("text_difference_by_category") or [],
            )
            self._append_breakdown_rows(
                rows,
                "Top Missing Requirements",
                qualitative_breakdown.get("top_missing_requirements") or [],
            )
            self._append_breakdown_rows(
                rows,
                "Top Scope-Variant Requirements",
                qualitative_breakdown.get("top_scope_variant_requirements")
                or [],
            )
            self._append_breakdown_rows(
                rows,
                "Top Text-Difference Requirements",
                qualitative_breakdown.get("top_text_difference_requirements")
                or [],
            )

        judge = summary.get("judge") or {}
        if judge.get("enabled"):
            rows.extend(
                [
                    {"Metric": "", "Value": ""},
                    {"Metric": "--- LLM Judge ---", "Value": ""},
                    {"Metric": "Judge Model", "Value": judge.get("model") or "unknown"},
                    {
                        "Metric": "Judge Calls Attempted",
                        "Value": int(judge.get("attempted_calls", 0)),
                    },
                    {
                        "Metric": "Judge Calls Successful",
                        "Value": int(judge.get("successful_calls", 0)),
                    },
                    {
                        "Metric": "Judge Resolved Matches",
                        "Value": int(judge.get("resolved_matches", 0)),
                    },
                    {
                        "Metric": "Judge Input Tokens",
                        "Value": int(judge.get("input_tokens", 0)),
                    },
                    {
                        "Metric": "Judge Output Tokens",
                        "Value": int(judge.get("output_tokens", 0)),
                    },
                    {
                        "Metric": "Judge Cost (USD)",
                        "Value": f"${float(judge.get('total_cost_usd', 0.0)):.4f}",
                    },
                ]
            )

        # Add potential duplicates count
        potential_duplicates_count = summary.get(
            "potential_duplicates_count", 0
        )
        if potential_duplicates_count > 0:
            rows.extend(
                [
                    {"Metric": "", "Value": ""},  # Blank row
                    {"Metric": "--- Potential Duplicates ---", "Value": ""},
                    {
                        "Metric": "Potential Duplicates",
                        "Value": potential_duplicates_count,
                    },
                ]
            )

        # Add completeness per model
        completeness_per_model = summary.get("completeness_per_model", {})
        if completeness_per_model:
            rows.extend(
                [
                    {"Metric": "", "Value": ""},  # Blank row
                    {"Metric": "--- Completeness Metrics ---", "Value": ""},
                ]
            )
            for model, metrics in completeness_per_model.items():
                expected_total = metrics.get("expected_total", 0)
                if expected_total > 0:
                    rows.append(
                        {
                            "Metric": f"Completeness ({model})",
                            "Value": f"{metrics.get('expected_found', 0)}/{expected_total} ({metrics.get('completeness_score', 0):.1f}%)",
                        }
                    )

        df = pd.DataFrame(rows)
        for col in list(df.columns):
            if col.endswith(" Source"):
                values = set(
                    str(v).strip() for v in df[col].fillna("").tolist()
                )
                if values <= {"", "-"}:
                    df = df.drop(columns=[col])
        if "Agreement" in df.columns:
            values = set(str(v).strip() for v in df["Agreement"].fillna("").tolist())
            if values <= {"", "-"}:
                df = df.drop(columns=["Agreement"])
        return df

    def _append_breakdown_rows(
        self,
        rows: List[Dict[str, Any]],
        metric_name: str,
        entries: List[Dict[str, Any]],
    ) -> None:
        """Append a compact qualitative mismatch breakdown row when entries exist."""
        if not entries:
            return

        value = ", ".join(
            f"{entry.get('label')}: {entry.get('count')}" for entry in entries
        )
        rows.append({"Metric": metric_name, "Value": value})

    def _truncate_value(self, value: Any) -> str:
        """Truncate long values for readability."""
        if value is None:
            return ""

        str_value = str(value)
        if len(str_value) > MAX_VALUE_LENGTH:
            return str_value[: MAX_VALUE_LENGTH - 3] + "..."
        return str_value

    def _auto_size_columns(self, ws) -> None:
        """Auto-size columns based on content."""
        sample_rows = min(ws.max_row, 250)
        for column_cells in ws.columns:
            max_length = 0
            column_letter = get_column_letter(column_cells[0].column)

            for cell in list(column_cells)[:sample_rows]:
                try:
                    cell_length = len(str(cell.value)) if cell.value else 0
                    max_length = max(max_length, cell_length)
                except (TypeError, AttributeError):
                    pass

            # Set width with min/max bounds
            adjusted_width = min(max(max_length + 2, 10), 50)
            ws.column_dimensions[column_letter].width = adjusted_width

    def _apply_sheet_column_layout(self, ws) -> None:
        """Apply per-header width caps to keep sheets readable."""
        width_map = {
            "#": 5,
            "Status": 12,
            "Category": 24,
            "Subject": 44,
            "Diverging Field(s)": 24,
            "Seen By": 26,
            "Why Flagged": 52,
            "Evidence": 60,
            "Judge": 46,
            "Likely Correct": 20,
            "Reviewer Verdict": 20,
            "Reviewer Notes": 42,
            "Issue Type": 18,
            "Divergence": 16,
            "Queue Signal": 12,
            "Match Method": 14,
            "Row ID": 12,
            "Requirement": 58,
            "Metric": 34,
            "Value": 56,
            "Section": 18,
            "Model": 26,
            "Failure Reason": 52,
            "Key": 42,
        }
        headers = [str(cell.value or "") for cell in ws[1]]
        for idx, header in enumerate(headers, 1):
            width = width_map.get(header)
            if width is None:
                continue
            ws.column_dimensions[get_column_letter(idx)].width = width

    def _build_item_centric_df(self, result: ComparisonResult) -> pd.DataFrame:
        """Build one row per aligned record with reviewer-first columns."""
        if not result.item_comparisons:
            return pd.DataFrame(
                columns=[
                    "Status",
                    "Divergence",
                    "Category",
                    "Subject",
                    "Diverging Field(s)",
                    "Seen By",
                    "Judge",
                ]
            )

        items_data: Dict[str, List[FieldComparison]] = {}
        for comparison in result.item_comparisons:
            group_id = comparison.row_id or comparison.item_id
            items_data.setdefault(group_id, []).append(comparison)

        model_labels = self._build_model_labels(result.models)
        comparison_approach = result.summary.get("comparison_approach") or "mixed"
        scope_variant_items = self._extract_scope_variant_items(result.summary)
        rows: List[Dict[str, Any]] = []

        for group_id, field_comparisons in sorted(items_data.items()):
            item_id = field_comparisons[0].item_id
            req_parts = [part.strip() for part in str(item_id).split("|")]
            while len(req_parts) < 3:
                req_parts.append("")
            category, applies_to, specific_subject = req_parts[:3]
            subject = " \u203a ".join(
                [segment for segment in [applies_to, specific_subject] if segment]
            )

            model_fields: Dict[str, Dict[str, Any]] = {model: {} for model in result.models}
            for field_comp in field_comparisons:
                field_name = field_comp.field_path.split(".")[-1].lower()
                for model, value in field_comp.model_values.items():
                    model_fields.setdefault(model, {})
                    model_fields[model][field_name] = value

            model_displays: Dict[str, str] = {}
            present_model_set = set()
            for field_comp in field_comparisons:
                present_model_set.update(field_comp.present_models or [])
            for model in result.models:
                display = self._select_model_display_value(
                    model_fields.get(model, {}),
                    comparison_approach=comparison_approach,
                )
                model_displays[model] = display

            if present_model_set:
                models_present = [m for m in result.models if m in present_model_set]
                models_missing = [m for m in result.models if m not in present_model_set]
            else:
                model_presence = {
                    model: bool(display and display != "\u2205")
                    for model, display in model_displays.items()
                }
                models_present = [m for m, present in model_presence.items() if present]
                models_missing = [m for m, present in model_presence.items() if not present]
            if not models_present:
                continue
            seen_by = ", ".join(model_labels[m] for m in models_present) if models_present else "-"

            any_review = any(fc.needs_review for fc in field_comparisons)
            missing_present = len(models_missing) > 0
            if missing_present and len(models_present) == 1:
                status = f"ONLY {model_labels[models_present[0]]}"
            elif missing_present and any_review:
                status = "PARTIAL"
            elif any_review:
                status = "DIFFER"
            else:
                status = "AGREE"

            divergence = self._determine_divergence(
                status=status,
                comparison_approach=comparison_approach,
                item_id=item_id,
                scope_variant_items=scope_variant_items,
                comparisons=field_comparisons,
            )
            issue_type = self._determine_issue_type(
                divergence=divergence,
                status=status,
                comparisons=field_comparisons,
            )
            judge_status = self._extract_judge_status(field_comparisons)
            likely_correct = self._extract_likely_correct_model(
                field_comparisons=field_comparisons,
                model_labels=model_labels,
            )
            evidence = self._build_model_evidence(
                field_comparisons=field_comparisons,
                model_order=result.models,
                model_labels=model_labels,
                divergence=divergence,
            )
            diverging_fields_list = self._collect_diverging_fields(field_comparisons)
            diverging_fields = ", ".join(diverging_fields_list) if diverging_fields_list else "-"
            if status == "DIFFER" and diverging_fields_list:
                for model in result.models:
                    model_displays[model] = self._select_model_display_value(
                        model_fields.get(model, {}),
                        comparison_approach=comparison_approach,
                        preferred_fields=diverging_fields_list,
                    )
            why_flagged = self._build_flag_reason(
                status=status,
                models_missing=models_missing,
                model_labels=model_labels,
                divergence=divergence,
                issue_type=issue_type,
                comparisons=field_comparisons,
                model_order=result.models,
            )

            row: Dict[str, Any] = {
                "Status": status,
                "Issue Type": issue_type,
                "Divergence": divergence,
                "Queue Signal": self._queue_signal(
                    status=status,
                    issue_type=issue_type,
                    divergence=divergence,
                    comparisons=field_comparisons,
                    model_order=result.models,
                ),
                "Category": category or "",
                "Subject": subject,
                "Seen By": seen_by,
                "Diverging Field(s)": diverging_fields,
            }
            for model in result.models:
                row[model_labels[model]] = model_displays.get(model, "\u2205")
            row["Evidence"] = evidence
            row["Judge Says"] = self._build_judge_says(judge_status, likely_correct)
            row["Why Flagged"] = why_flagged
            row["Reviewer Verdict"] = ""
            row["Reviewer Notes"] = ""
            row["Row ID"] = group_id
            row["Match Method"] = field_comparisons[0].match_method or "exact"
            row["Requirement"] = str(item_id)
            rows.append(row)

        rows.sort(
            key=lambda row: (
                self._divergence_order(row.get("Divergence", "")),
                row.get("Issue Type", ""),
                row.get("Category", ""),
                row.get("Subject", ""),
                row.get("Requirement", ""),
            )
        )
        for idx, row in enumerate(rows, start=1):
            row["#"] = idx

        column_order = [
            "#",
            "Status",
            "Category",
            "Subject",
            *[model_labels[m] for m in result.models],
            "Diverging Field(s)",
            "Evidence",
            "Judge Says",
            "Why Flagged",
            "Reviewer Verdict",
            "Reviewer Notes",
            "Issue Type",
            "Divergence",
            "Queue Signal",
            "Match Method",
            "Row ID",
            "Requirement",
        ]
        if not rows:
            return pd.DataFrame(columns=column_order)
        return pd.DataFrame(rows)[column_order]

    def _build_model_labels(self, models: List[str]) -> Dict[str, str]:
        """Create collision-safe model labels while preserving full model names."""
        labels: Dict[str, str] = {}
        used: set[str] = set()
        for model in models:
            candidate = model
            label = candidate
            suffix = 2
            while label in used:
                label = f"{candidate} ({suffix})"
                suffix += 1
            used.add(label)
            labels[model] = label
        return labels

    def _extract_scope_variant_items(self, summary: Dict[str, Any]) -> set[str]:
        """Extract scope-variant requirement labels from summary breakdown."""
        breakdown = summary.get("qualitative_mismatch_breakdown") or {}
        items = breakdown.get("top_scope_variant_requirements") or []
        return {
            str(entry.get("label")).strip()
            for entry in items
            if entry.get("label")
        }

    def _select_model_display_value(
        self,
        fields: Dict[str, Any],
        *,
        comparison_approach: str,
        preferred_fields: Optional[List[str]] = None,
    ) -> str:
        """Pick a reviewer-facing value for one model on one item."""
        if comparison_approach == "text_review":
            candidates = [
                fields.get("requirement_description"),
                fields.get("value"),
                fields.get("summary"),
                fields.get("condition"),
                fields.get("source_verbatim"),
                fields.get("source_text"),
            ]
            for value in candidates:
                if value not in (None, ""):
                    return self._truncate_value(value)
            for field_name, field_value in fields.items():
                if field_name != "source_text" and field_value not in (None, ""):
                    return self._truncate_value(field_value)
            return "\u2205"

        if preferred_fields:
            rendered_fields: List[str] = []
            max_fields = 3
            for index, field_name in enumerate(preferred_fields):
                if index >= max_fields:
                    break
                field_key = str(field_name).lower()
                field_value = fields.get(field_key)
                display_value = "∅" if field_value in (None, "") else field_value
                rendered_fields.append(
                    self._truncate_value(f"{field_name}={display_value}")
                )
            if rendered_fields:
                if len(preferred_fields) > max_fields:
                    rendered_fields.append("…")
                return "; ".join(rendered_fields)

        value = fields.get("value")
        unit = fields.get("unit") or fields.get("units")
        if value not in (None, "") and unit not in (None, ""):
            return self._truncate_value(f"{value} {unit}")
        if value not in (None, ""):
            return self._truncate_value(value)
        for fallback_field in (
            "value_interpretation",
            "obligation",
            "condition",
            "summary",
            "source_text",
        ):
            fallback = fields.get(fallback_field)
            if fallback not in (None, ""):
                return self._truncate_value(fallback)
        if unit not in (None, ""):
            return self._truncate_value(unit)
        return "\u2205"

    def _collect_diverging_fields(
        self, comparisons: List[FieldComparison]
    ) -> List[str]:
        fields = [
            comparison.field_path.split(".")[-1]
            for comparison in comparisons
            if comparison.needs_review
        ]
        unique_fields: List[str] = []
        for field in fields:
            if field not in unique_fields:
                unique_fields.append(field)
        return unique_fields

    def _determine_divergence(
        self,
        *,
        status: str,
        comparison_approach: str,
        item_id: str,
        scope_variant_items: set[str],
        comparisons: List[FieldComparison],
    ) -> str:
        """Map a row to a triage divergence type."""
        if status == "AGREE":
            return "aligned"
        if status.startswith("ONLY") or status == "PARTIAL":
            if comparison_approach == "text_review" and item_id in scope_variant_items:
                return "scope_variant"
            return "presence_diff"
        if any("judge_error=" in (comparison.notes or "") for comparison in comparisons):
            return "judge_uncertain"
        if comparison_approach == "text_review":
            return "semantic_conflict"
        return "field_conflict"

    def _determine_issue_type(
        self,
        *,
        divergence: str,
        status: str,
        comparisons: List[FieldComparison],
    ) -> str:
        if divergence == "aligned":
            return "ALIGNED"
        if divergence == "presence_diff":
            methods = {c.match_method for c in comparisons if c.match_method}
            if "scope_split" in methods:
                return "MATCH_AMBIGUITY"
            if "unmatched" in methods:
                return "PRESENCE_CANDIDATE"
            if "fuzzy" in methods:
                return "MATCH_AMBIGUITY"
            return "PRESENCE_CANDIDATE"
        if divergence == "judge_uncertain":
            return "JUDGE_INCONCLUSIVE"
        if divergence == "semantic_conflict":
            return "SEMANTIC_CONFLICT"
        if divergence == "field_conflict":
            return "FIELD_CONFLICT"
        if status == "DIFFER":
            return "FIELD_CONFLICT"
        return "ALIGNED"

    def _extract_judge_status(self, comparisons: List[FieldComparison]) -> str:
        """Derive per-item judge status from field comparison notes."""
        active = [comparison for comparison in comparisons if comparison.needs_review]
        scan = active or comparisons
        for comparison in comparisons:
            if comparison.paired_by_judge:
                confidence = comparison.pairing_confidence or "unknown"
                reason = (comparison.pairing_reason or "").strip()
                if reason:
                    return f"paired ({confidence}): {self._truncate_value(reason)}"
                return f"paired ({confidence})"
        for comparison in scan:
            notes = comparison.notes or ""
            if notes.startswith("LLM row judge matched"):
                return notes.replace("LLM row judge matched", "row-matched", 1)
        for comparison in scan:
            notes = comparison.notes or ""
            if notes.startswith("LLM judge matched"):
                return notes.replace("LLM judge matched", "matched", 1)
        for comparison in scan:
            notes = comparison.notes or ""
            if "LLM judge mismatch" in notes:
                return notes.replace("LLM judge mismatch", "mismatch", 1)
        for comparison in scan:
            notes = comparison.notes or ""
            if "LLM judge inconclusive" in notes:
                return notes.replace("LLM judge inconclusive", "inconclusive", 1)
        for comparison in scan:
            notes = comparison.notes or ""
            if "judge_error=" in notes:
                return "error"
        return ""

    def _extract_likely_correct_model(
        self,
        *,
        field_comparisons: List[FieldComparison],
        model_labels: Dict[str, str],
    ) -> str:
        """Return the first high-confidence judge winner label for the row."""
        active = [
            comparison for comparison in field_comparisons if comparison.needs_review
        ]
        scan = active or field_comparisons
        for comparison in scan:
            candidate = (comparison.judge_likely_correct_model or "").strip()
            if not candidate:
                continue
            return model_labels.get(candidate, candidate)
        return ""

    def _build_judge_says(self, judge_status: str, likely_correct: str) -> str:
        """Combine judge status + likely-correct pick into one concise reviewer column."""
        if likely_correct:
            reason = (judge_status or "").strip()
            if len(reason) > 120:
                reason = reason[:120] + "…"
            return f"{likely_correct}" + (f" — {reason}" if reason else "")
        if not judge_status:
            return ""
        lower = judge_status.lower()
        if "inconclusive" in lower:
            return "Inconclusive"
        if "error" in lower:
            return "Judge error"
        trimmed = judge_status.strip()
        return trimmed[:120] + ("…" if len(trimmed) > 120 else "")

    def _build_model_evidence(
        self,
        *,
        field_comparisons: List[FieldComparison],
        model_order: List[str],
        model_labels: Dict[str, str],
        divergence: str,
    ) -> str:
        """Build compact side-by-side evidence from source_verbatim/summary."""
        if divergence not in {"field_conflict", "semantic_conflict", "judge_uncertain"}:
            return ""

        verbatim: Dict[str, str] = {}
        summary: Dict[str, str] = {}
        for comparison in field_comparisons:
            for model, text in (comparison.model_verbatim or {}).items():
                if text and model not in verbatim:
                    verbatim[model] = text
            for model, text in (comparison.model_summary or {}).items():
                if text and model not in summary:
                    summary[model] = text

        snippets: List[str] = []
        for model in model_order:
            quote = verbatim.get(model) or summary.get(model)
            if not quote:
                continue
            snippets.append(
                f"{model_labels.get(model, model)}: {self._truncate_value(quote)}"
            )
        return " | ".join(snippets)

    def _build_flag_reason(
        self,
        *,
        status: str,
        models_missing: List[str],
        model_labels: Dict[str, str],
        divergence: str,
        issue_type: str,
        comparisons: List[FieldComparison],
        model_order: List[str],
    ) -> str:
        """Build concise reviewer-facing reason."""
        if divergence == "judge_uncertain":
            return "Judge uncertainty/error; human review required"
        if issue_type == "MATCH_AMBIGUITY":
            return "Potential scope split/merge across models"
        if models_missing:
            if len(models_missing) >= len(model_labels):
                return "Presence mismatch unresolved; inspect pairing"
            missing_names = [model_labels.get(model, model) for model in models_missing]
            return f"Missing in: {', '.join(sorted(missing_names))}"
        if status == "DIFFER":
            for comparison in comparisons:
                if not comparison.needs_review:
                    continue
                field_name = comparison.field_path.split(".")[-1]
                rendered: List[str] = []
                for model in model_order:
                    if model in comparison.model_values:
                        value = comparison.model_values.get(model)
                        rendered.append(
                            f"{model_labels.get(model, model)}={self._truncate_value(value if value not in (None, '') else '∅')}"
                        )
                if rendered:
                    return f"{field_name}: " + " vs ".join(rendered)
            return "Model values differ"
        return ""

    def _build_diverging_fields(
        self, comparisons: List[FieldComparison]
    ) -> str:
        fields = self._collect_diverging_fields(comparisons)
        if not fields:
            return "-"
        return ", ".join(fields)

    def _divergence_order(self, divergence: str) -> int:
        order = {
            "judge_uncertain": 0,
            "field_conflict": 1,
            "semantic_conflict": 2,
            "presence_diff": 3,
            "scope_variant": 4,
            "aligned": 5,
        }
        return order.get(str(divergence), 9)

    def _build_review_queue_df(
        self,
        all_items_df: pd.DataFrame,
        *,
        include_missing: bool,
        include_low_signal_presence: bool = False,
    ) -> pd.DataFrame:
        """Return reviewer queue rows only, preserving sort order.

        Queue is intentionally compact for human triage. Full diagnostic columns
        remain available in the "All Items" sheet.
        """
        if all_items_df.empty:
            return all_items_df.copy()
        allowed = {"field_conflict", "semantic_conflict", "judge_uncertain"}
        if include_missing:
            allowed.add("presence_diff")
        queue = all_items_df[
            all_items_df["Divergence"].astype(str).isin(sorted(allowed))
        ].copy()
        if include_missing and not include_low_signal_presence:
            queue = queue[
                ~(
                    queue["Divergence"].astype(str).eq("presence_diff")
                    & queue["Queue Signal"].astype(str).eq("low")
                )
            ].copy()
        queue["#"] = range(1, len(queue) + 1)

        metadata_columns = {
            "#",
            "Status",
            "Category",
            "Subject",
            "Diverging Field(s)",
            "Why Flagged",
            "Evidence",
            "Judge Says",
            "Reviewer Verdict",
            "Reviewer Notes",
            "Issue Type",
            "Divergence",
            "Queue Signal",
            "Match Method",
            "Row ID",
            "Requirement",
        }
        model_columns = [
            col for col in queue.columns if col not in metadata_columns
        ]
        lean_columns = [
            "#",
            "Status",
            "Category",
            "Subject",
            *model_columns,
            "Diverging Field(s)",
            "Why Flagged",
            "Evidence",
            "Judge Says",
            "Reviewer Verdict",
            "Reviewer Notes",
            "Divergence",  # kept at far right for conditional formatting
        ]
        return queue[[col for col in lean_columns if col in queue.columns]]

    def _queue_signal(
        self,
        *,
        status: str,
        issue_type: str,
        divergence: str,
        comparisons: List[FieldComparison],
        model_order: List[str],
    ) -> str:
        """Classify queue signal strength for one-sided presence rows."""
        if divergence != "presence_diff" or not status.startswith("ONLY"):
            return "high"
        if issue_type != "PRESENCE_CANDIDATE":
            return "high"

        diverging_fields = {
            comparison.field_path.split(".")[-1].lower()
            for comparison in comparisons
            if comparison.needs_review
        }
        if not diverging_fields:
            return "low"
        if diverging_fields != {"obligation"}:
            return "high"

        obligation_values = []
        for comparison in comparisons:
            if comparison.field_path.split(".")[-1].lower() != "obligation":
                continue
            for model in model_order:
                value = comparison.model_values.get(model)
                if value in (None, ""):
                    continue
                obligation_values.append(str(value).strip().lower())
        if not obligation_values:
            return "low"

        if all(len(value) <= 24 and " " not in value for value in obligation_values):
            return "low"
        return "high"

    def _build_dashboard_df(
        self,
        *,
        result: ComparisonResult,
        all_items_df: pd.DataFrame,
        review_queue_df: pd.DataFrame,
        run_metadata: Optional[Dict[str, Any]],
    ) -> pd.DataFrame:
        """Build a compact start-here dashboard."""
        summary = result.summary or {}
        judge = summary.get("judge") or {}
        gate = summary.get("qualitative_advisory_gate") or {}
        metadata_summary = (run_metadata or {}).get("summary") or {}

        rows = [
            {"Section": "Start Here", "Metric": "Document", "Value": result.document_name},
            {"Section": "Start Here", "Metric": "Models Compared", "Value": ", ".join(result.models)},
            {
                "Section": "Start Here",
                "Metric": "Rows Requiring Review (record-level queue)",
                "Value": len(review_queue_df),
            },
            {"Section": "Start Here", "Metric": "All Item Rows", "Value": len(all_items_df)},
            {
                "Section": "Start Here",
                "Metric": "Fields Requiring Review (field-level)",
                "Value": int(summary.get("needs_review_count", 0)),
            },
            {"Section": "QA/QC", "Metric": "Profile", "Value": summary.get("qaqc_profile") or "unknown"},
            {"Section": "QA/QC", "Metric": "Comparison Approach", "Value": summary.get("comparison_approach") or "mixed"},
            {"Section": "QA/QC", "Metric": "Full Agreement %", "Value": f"{float(summary.get('full_agreement_pct', 0.0)):.1f}%"},
            {"Section": "QA/QC", "Metric": "Needs Review %", "Value": f"{float(summary.get('needs_review_pct', 0.0)):.1f}%"},
        ]
        if gate:
            rows.extend(
                [
                    {"Section": "Qualitative Gate", "Metric": "Status", "Value": str(gate.get("status", "not_applicable")).upper()},
                    {"Section": "Qualitative Gate", "Metric": "Aligned %", "Value": f"{float(gate.get('aligned_pct') or 0.0):.1f}%"},
                    {"Section": "Qualitative Gate", "Metric": "Missing Item %", "Value": f"{float(gate.get('missing_item_pct') or 0.0):.1f}%"},
                    {"Section": "Qualitative Gate", "Metric": "Recommended Action", "Value": gate.get("recommended_action", "")},
                ]
            )
        anchor = summary.get("anchor_metrics")
        if anchor:
            one_sided = anchor.get("one_sided_counts") or {}
            precision = anchor.get("precision_proxy")
            rows.extend([
                {"Section": "Matching", "Metric": "Matching Strategy", "Value": "verbatim-anchored (char n-gram)"},
                {"Section": "Matching", "Metric": "Requirement Pairs Matched", "Value": int(anchor.get("matched_requirement_pairs", 0))},
                {"Section": "Matching", "Metric": "Anchor Match Rate (proxy)", "Value": f"{float(anchor.get('anchor_match_rate_proxy', 0.0)):.1%}"},
                {"Section": "Matching", "Metric": "Field-Level Precision (proxy, matched pairs)", "Value": f"{float(precision):.1%}" if precision is not None else "N/A"},
            ])
            for model, count in sorted(one_sided.items()):
                rows.append({"Section": "Matching", "Metric": f"One-Sided Rows: {model}", "Value": int(count)})
            rows.append({"Section": "Matching", "Metric": "Note", "Value": anchor.get("note", "")})
        if judge.get("enabled"):
            rows.extend(
                [
                    {"Section": "Judge", "Metric": "Judge Model", "Value": judge.get("model") or "unknown"},
                    {"Section": "Judge", "Metric": "Judge Cache Hits", "Value": int(judge.get("cache_hits", 0))},
                    {"Section": "Judge", "Metric": "Judge Calls Attempted", "Value": int(judge.get("attempted_calls", 0))},
                    {"Section": "Judge", "Metric": "Judge Calls Successful", "Value": int(judge.get("successful_calls", 0))},
                    {"Section": "Judge", "Metric": "Judge Resolved Matches", "Value": int(judge.get("resolved_matches", 0))},
                    {"Section": "Judge", "Metric": "Judge Calls Skipped (budget)", "Value": int(judge.get("skipped_budget", 0))},
                ]
            )
        if metadata_summary:
            rows.extend(
                [
                    {"Section": "Run Diagnostics", "Metric": "Requested Models", "Value": int(metadata_summary.get("total_models", len(result.models)))},
                    {"Section": "Run Diagnostics", "Metric": "Successful Models", "Value": int(metadata_summary.get("successful", len(result.models)))},
                    {"Section": "Run Diagnostics", "Metric": "Failed Models", "Value": int(metadata_summary.get("failed", 0))},
                    {"Section": "Run Diagnostics", "Metric": "Run Status", "Value": run_metadata.get("status", "unknown")},
                ]
            )
        rows.extend(self._build_divergence_legend_rows(all_items_df))
        return pd.DataFrame(rows)

    def _build_model_reliability_df(
        self,
        *,
        result: ComparisonResult,
        all_items_df: pd.DataFrame,
        run_metadata: Optional[Dict[str, Any]],
    ) -> pd.DataFrame:
        """Build per-model reliability summary, including failed models from metadata."""
        rows: List[Dict[str, Any]] = []
        metadata_results = (run_metadata or {}).get("results") or {}
        seen_map: Dict[str, int] = {}
        for model in result.models:
            label = self._build_model_labels([model])[model]
            if label in all_items_df.columns:
                seen_map[model] = int((all_items_df[label].astype(str) != "\u2205").sum())
            else:
                seen_map[model] = 0
        for model in sorted(set(list(result.models) + list(metadata_results.keys()))):
            model_meta = metadata_results.get(model) or {}
            success = bool(model_meta.get("success", model in result.models))
            failure_reason = ""
            if not success:
                failure_reason = str(model_meta.get("error") or model_meta.get("error_details", {}).get("message") or "")
            rows.append(
                {
                    "Model": model,
                    "Status": "ok" if success else "failed",
                    "Items Seen in Comparison": seen_map.get(model, 0),
                    "Reused Output": bool(model_meta.get("reused", False)),
                    "Input Tokens": int(model_meta.get("input_tokens", 0) or 0),
                    "Output Tokens": int(model_meta.get("output_tokens", 0) or 0),
                    "Cost (USD)": float(model_meta.get("cost", 0.0) or 0.0),
                    "Failure Reason": failure_reason,
                }
            )
        return pd.DataFrame(rows)

    def _build_divergence_legend_rows(self, all_items_df: pd.DataFrame) -> List[Dict[str, Any]]:
        """Build legend rows only for divergence values present in this report."""
        legend_copy = {
            "aligned": "Fully aligned",
            "field_conflict": "Values conflict",
            "judge_uncertain": "Judge inconclusive/error",
            "presence_diff": "Only some models found this row",
            "semantic_conflict": "Text/semantic values differ",
            "scope_variant": "Auxiliary scope variance",
        }
        preferred_order = [
            "field_conflict",
            "judge_uncertain",
            "presence_diff",
            "semantic_conflict",
            "scope_variant",
            "aligned",
        ]
        if "Divergence" not in all_items_df.columns:
            return []
        seen = {
            str(v).strip()
            for v in all_items_df["Divergence"].dropna().tolist()
            if str(v).strip()
        }
        rows: List[Dict[str, Any]] = []
        for key in preferred_order:
            if key not in seen:
                continue
            rows.append({"Section": "Legend", "Metric": key, "Value": legend_copy[key]})
        return rows

    def _build_run_manifest_df(
        self,
        *,
        result: ComparisonResult,
        run_metadata: Optional[Dict[str, Any]],
    ) -> pd.DataFrame:
        """Build reproducibility/provenance rows for diagnostics."""
        rows = [
            {"Key": "document_name", "Value": result.document_name},
            {"Key": "models_compared", "Value": ", ".join(result.models)},
            {"Key": "qaqc_profile", "Value": result.summary.get("qaqc_profile") or "unknown"},
            {"Key": "comparison_approach", "Value": result.summary.get("comparison_approach") or "mixed"},
        ]
        judge = result.summary.get("judge") or {}
        if judge.get("enabled"):
            rows.extend(
                [
                    {"Key": "judge_model", "Value": judge.get("model") or "unknown"},
                    {"Key": "judge_attempted_calls", "Value": int(judge.get("attempted_calls", 0))},
                    {"Key": "judge_successful_calls", "Value": int(judge.get("successful_calls", 0))},
                    {"Key": "judge_total_cost_usd", "Value": float(judge.get("total_cost_usd", 0.0))},
                ]
            )
        def _append_flat(prefix: str, value: Any) -> None:
            if isinstance(value, dict):
                for child_key, child_value in sorted(value.items()):
                    _append_flat(f"{prefix}.{child_key}", child_value)
                return
            if isinstance(value, list):
                rows.append(
                    {
                        "Key": prefix,
                        "Value": json.dumps(value, sort_keys=True),
                    }
                )
                return
            rows.append({"Key": prefix, "Value": value})

        for key, value in sorted((run_metadata or {}).items()):
            if key in {"results", "errors", "model_errors"}:
                continue
            _append_flat(f"metadata.{key}", value)
        return pd.DataFrame(rows)

    def _plain_text(self, value: Any) -> Any:
        """Normalize a pandas cell into a workbook-safe value (NaN/None -> '')."""
        if value is None:
            return ""
        if isinstance(value, float) and pd.isna(value):
            return ""
        return value

    def _model_win_formula(
        self, status_ref: str, verdict_ref: str, model_label_list: List[str]
    ) -> str:
        """Build the per-row Model Win formula for N models (row-relative refs)."""
        inner = (
            f'IF(OR({verdict_ref}="reject-all",{verdict_ref}="needs-escalation"),'
            f'"Unresolved","Pending")'
        )
        for label in reversed(model_label_list):
            inner = f'IF({verdict_ref}="accept-{label}","{label}",{inner})'
        inner = f'IF({verdict_ref}="accept-both","Both",{inner})'
        return f'=IF({status_ref}="AGREE","Both",{inner})'

    def _build_final_verdicts_data(
        self, result: ComparisonResult, all_items_df: pd.DataFrame
    ) -> List[Dict[str, Any]]:
        """Build one authoritative detail row per item, preserving sort order."""
        model_labels = self._build_model_labels(result.models)
        model_label_list = [model_labels[m] for m in result.models]
        rows: List[Dict[str, Any]] = []
        for _, item in all_items_df.iterrows():
            status = self._plain_text(item.get("Status"))
            judge_decision = self._plain_text(item.get("Judge Says"))
            model_values = {
                label: self._plain_text(item.get(label))
                for label in model_label_list
            }
            if status == "AGREE" and model_label_list:
                correct_value = model_values.get(model_label_list[0], "")
            else:
                correct_value = ""
            row: Dict[str, Any] = {
                "#": self._plain_text(item.get("#")),
                "Status": status,
                "Category": self._plain_text(item.get("Category")),
                "Subject": self._plain_text(item.get("Subject")),
            }
            row.update(model_values)
            row["Diverging Field(s)"] = self._plain_text(
                item.get("Diverging Field(s)")
            )
            row["Judge Decision"] = judge_decision
            row["Reviewer Verdict"] = ""
            row["Model Win"] = ""
            row["Correct Value"] = correct_value
            row["Correct Answer"] = ""
            row["Evidence"] = self._plain_text(item.get("Evidence"))
            row["Row ID"] = self._plain_text(item.get("Row ID"))
            rows.append(row)
        return rows

    def _write_final_verdicts_sheet(
        self, wb, result: ComparisonResult, all_items_df: pd.DataFrame
    ) -> None:
        """Write the authoritative Final Verdicts sheet directly via openpyxl."""
        model_labels = self._build_model_labels(result.models)
        model_label_list = [model_labels[m] for m in result.models]
        data_rows = self._build_final_verdicts_data(result, all_items_df)

        detail_columns = [
            "#",
            "Status",
            "Category",
            "Subject",
            *model_label_list,
            "Diverging Field(s)",
            "Judge Decision",
            "Reviewer Verdict",
            "Model Win",
            "Correct Value",
            "Correct Answer",
            "Evidence",
            "Row ID",
        ]

        if "Final Verdicts" in wb.sheetnames:
            del wb["Final Verdicts"]
        ws = wb.create_sheet("Final Verdicts")

        # --- Scorecard block (rows expand with model count to stay scalable) ---
        ws.cell(1, 1, "Final Verdicts \u2014 Model Accuracy Scorecard")
        ws.cell(3, 1, "Metric")
        ws.cell(3, 2, "Value")
        ws.cell(4, 1, "Document")
        ws.cell(4, 2, result.document_name)
        ws.cell(5, 1, "Models Compared")
        ws.cell(5, 2, ", ".join(result.models))
        ws.cell(6, 1, "Total Items")
        ws.cell(6, 2, len(all_items_df))

        ws.cell(7, 1, "Auto-Agreed (AGREE rows)")
        auto_agreed_row = 7
        ws.cell(8, 1, "Judge-Decided (judge resolved, DIFFER rows)")
        judge_decided_row = 8
        ws.cell(9, 1, "Reviewer-Decided (human resolved)")
        reviewer_decided_row = 9

        per_model_win_rows: Dict[str, int] = {}
        cursor = 10
        for label in model_label_list:
            ws.cell(cursor, 1, f"{label} Wins")
            per_model_win_rows[label] = cursor
            cursor += 1
        ws.cell(cursor, 1, "Escalations (needs-escalation)")
        escalation_row = cursor
        cursor += 1
        ws.cell(cursor, 1, "Unresolved / Pending")
        unresolved_row = cursor
        cursor += 1
        cursor += 1  # blank spacer row before the detail table

        # --- Detail table ---
        detail_header_row = cursor
        detail_data_start_row = detail_header_row + 1
        for col_idx, header in enumerate(detail_columns, start=1):
            ws.cell(detail_header_row, col_idx, header)
        for offset, row in enumerate(data_rows):
            r = detail_data_start_row + offset
            for col_idx, header in enumerate(detail_columns, start=1):
                ws.cell(r, col_idx, row.get(header, ""))
        detail_data_end_row = detail_header_row + len(data_rows)

        status_col_idx = detail_columns.index("Status") + 1
        judge_col_idx = detail_columns.index("Judge Decision") + 1
        verdict_col_idx = detail_columns.index("Reviewer Verdict") + 1
        model_win_col_idx = detail_columns.index("Model Win") + 1
        status_letter = get_column_letter(status_col_idx)
        judge_letter = get_column_letter(judge_col_idx)
        verdict_letter = get_column_letter(verdict_col_idx)

        placeholder_rows = [
            auto_agreed_row,
            judge_decided_row,
            reviewer_decided_row,
            *per_model_win_rows.values(),
            escalation_row,
            unresolved_row,
        ]
        if not data_rows:
            for row_num in placeholder_rows:
                ws.cell(row_num, 2, 0)
            return

        status_range = (
            f"${status_letter}${detail_data_start_row}:"
            f"${status_letter}${detail_data_end_row}"
        )
        judge_range = (
            f"${judge_letter}${detail_data_start_row}:"
            f"${judge_letter}${detail_data_end_row}"
        )
        verdict_range = (
            f"${verdict_letter}${detail_data_start_row}:"
            f"${verdict_letter}${detail_data_end_row}"
        )

        ws.cell(auto_agreed_row, 2, f'=COUNTIF({status_range},"AGREE")')
        ws.cell(
            judge_decided_row,
            2,
            f'=COUNTIFS({status_range},"DIFFER",{judge_range},"<>")',
        )
        accept_terms = [
            f'COUNTIF({verdict_range},"accept-{label}")'
            for label in model_label_list
        ]
        accept_terms.append(f'COUNTIF({verdict_range},"accept-both")')
        ws.cell(reviewer_decided_row, 2, "=" + "+".join(accept_terms))
        for label in model_label_list:
            ws.cell(
                per_model_win_rows[label],
                2,
                f'=COUNTIF({verdict_range},"accept-{label}")',
            )
        ws.cell(escalation_row, 2, f'=COUNTIF({verdict_range},"needs-escalation")')
        ws.cell(unresolved_row, 2, f"=SUMPRODUCT((LEN({verdict_range})=0)*1)")

        for r in range(detail_data_start_row, detail_data_end_row + 1):
            ws.cell(
                r,
                model_win_col_idx,
                self._model_win_formula(
                    f"${status_letter}{r}", f"${verdict_letter}{r}", model_label_list
                ),
            )

    def _format_final_verdicts_sheet(self, ws) -> None:
        """Style the Final Verdicts scorecard and authoritative detail table."""
        detail_header_row = None
        for r in range(1, ws.max_row + 1):
            row_values = [ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
            if "Reviewer Verdict" in row_values:
                detail_header_row = r
                break
        if detail_header_row is None:
            return

        max_col = ws.max_column
        last_letter = get_column_letter(max_col)
        headers = {
            ws.cell(detail_header_row, c).value: c
            for c in range(1, max_col + 1)
            if ws.cell(detail_header_row, c).value
        }

        blue = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        gray = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
        light = PatternFill(start_color="F5F5F5", end_color="F5F5F5", fill_type="solid")
        white_bold = Font(bold=True, color="FFFFFF")
        bold = Font(bold=True)

        # Title row.
        ws.merge_cells("A1:D1")
        title_cell = ws.cell(1, 1)
        title_cell.fill = blue
        title_cell.font = white_bold
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 28

        # Scorecard header + alternating data rows.
        scorecard_header_row = None
        for r in range(1, detail_header_row):
            if ws.cell(r, 1).value == "Metric":
                scorecard_header_row = r
                break
        if scorecard_header_row is not None:
            for c in (1, 2):
                cell = ws.cell(scorecard_header_row, c)
                cell.fill = gray
                cell.font = bold
            for idx, r in enumerate(
                range(scorecard_header_row + 1, detail_header_row - 1)
            ):
                for c in (1, 2):
                    cell = ws.cell(r, c)
                    if idx % 2 == 0:
                        cell.fill = light
                    cell.alignment = Alignment(wrap_text=True, vertical="top")

        # Blank spacer rows.
        ws.row_dimensions[2].height = 10
        ws.row_dimensions[detail_header_row - 1].height = 10

        # Detail header row.
        for c in range(1, max_col + 1):
            cell = ws.cell(detail_header_row, c)
            cell.fill = blue
            cell.font = white_bold
            cell.alignment = Alignment(
                horizontal="center", vertical="center", wrap_text=True
            )
        ws.row_dimensions[detail_header_row].height = 24

        data_start = detail_header_row + 1
        data_end = ws.max_row
        subject_col = headers.get("Subject", 4)
        diverging_col = headers.get("Diverging Field(s)")
        verdict_col = headers.get("Reviewer Verdict")
        model_win_col = headers.get("Model Win")

        ws.freeze_panes = f"{get_column_letter(subject_col + 1)}{data_start}"
        ws.auto_filter.ref = f"A{detail_header_row}:{last_letter}{data_end}"

        if data_end >= data_start:
            model_cols = (
                list(range(subject_col + 1, diverging_col)) if diverging_col else []
            )
            wrap_cols = set(model_cols)
            for name in ("Subject", "Evidence", "Correct Answer", "Correct Value"):
                if name in headers:
                    wrap_cols.add(headers[name])
            for c in wrap_cols:
                for r in range(data_start, data_end + 1):
                    ws.cell(r, c).alignment = Alignment(
                        wrap_text=True, vertical="top"
                    )
            for r in range(data_start, data_end + 1):
                ws.row_dimensions[r].height = 40

            if verdict_col and diverging_col:
                labels = [
                    ws.cell(detail_header_row, c).value
                    for c in range(subject_col + 1, diverging_col)
                ]
                verdict_options = [f"accept-{label}" for label in labels if label]
                verdict_options += ["accept-both", "reject-all", "needs-escalation"]
                column_letter = get_column_letter(verdict_col)
                validation = DataValidation(
                    type="list",
                    formula1=f'"{",".join(verdict_options)}"',
                    allow_blank=True,
                )
                validation.error = (
                    "Select a valid reviewer verdict from the dropdown."
                )
                validation.errorTitle = "Invalid Verdict"
                ws.add_data_validation(validation)
                validation.add(
                    f"{column_letter}{data_start}:{column_letter}{data_end}"
                )

            if model_win_col:
                win_letter = get_column_letter(model_win_col)
                win_range = f"{win_letter}{data_start}:{win_letter}{data_end}"
                first_ref = f"${win_letter}{data_start}"
                green = PatternFill(
                    start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"
                )
                red = PatternFill(
                    start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"
                )
                pending = PatternFill(
                    start_color="D9D9D9", end_color="D9D9D9", fill_type="solid"
                )
                yellow = PatternFill(
                    start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"
                )
                ws.conditional_formatting.add(
                    win_range, FormulaRule(formula=[f'{first_ref}="Both"'], fill=green)
                )
                ws.conditional_formatting.add(
                    win_range,
                    FormulaRule(formula=[f'{first_ref}="Unresolved"'], fill=red),
                )
                ws.conditional_formatting.add(
                    win_range,
                    FormulaRule(formula=[f'{first_ref}="Pending"'], fill=pending),
                )
                ws.conditional_formatting.add(
                    win_range,
                    FormulaRule(
                        formula=[
                            f'AND(LEN({first_ref})>0,{first_ref}<>"Both",'
                            f'{first_ref}<>"Unresolved",{first_ref}<>"Pending")'
                        ],
                        fill=yellow,
                    ),
                )

        width_map = {
            "Status": 12,
            "Category": 20,
            "Subject": 28,
            "Diverging Field(s)": 18,
            "Judge Decision": 20,
            "Reviewer Verdict": 22,
            "Model Win": 16,
            "Correct Value": 22,
            "Correct Answer": 24,
            "Evidence": 38,
            "Row ID": 10,
        }
        if diverging_col:
            for c in range(subject_col + 1, diverging_col):
                ws.column_dimensions[get_column_letter(c)].width = 28
        for name, width in width_map.items():
            if name in headers:
                ws.column_dimensions[get_column_letter(headers[name])].width = width
        ws.sheet_view.zoomScale = 115

    def _apply_workbook_formatting(
        self,
        excel_path: Path,
        result: Optional[ComparisonResult] = None,
        all_items_df: Optional[pd.DataFrame] = None,
    ) -> None:
        """Apply formatting, filters, validation, and sheet defaults."""
        wb = load_workbook(excel_path)
        for name in wb.sheetnames:
            ws = wb[name]
            self._format_generic_sheet(ws)
            if name == "Dashboard":
                self._format_dashboard_sheet(ws)
            if name in {"Review Queue", "All Items"}:
                self._format_review_sheet(ws)
        if result is not None and all_items_df is not None:
            self._write_final_verdicts_sheet(wb, result, all_items_df)
            self._format_final_verdicts_sheet(wb["Final Verdicts"])
        if "Review Queue" in wb.sheetnames:
            wb.active = wb.sheetnames.index("Review Queue")
        wb.save(excel_path)

    def _format_dashboard_sheet(self, ws) -> None:
        """Highlight legend rows and keep dashboard readable."""
        if ws.max_row < 2:
            return
        section_col = None
        metric_col = None
        for idx, cell in enumerate(ws[1], 1):
            if cell.value == "Section":
                section_col = idx
            elif cell.value == "Metric":
                metric_col = idx
        if section_col is None or metric_col is None:
            return
        for row_idx in range(2, ws.max_row + 1):
            section = str(ws.cell(row_idx, section_col).value or "")
            metric = str(ws.cell(row_idx, metric_col).value or "")
            if section != "Legend":
                continue
            color = self._divergence_fill_color(metric)
            if not color:
                continue
            fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
            for col_idx in range(1, ws.max_column + 1):
                ws.cell(row_idx, col_idx).fill = fill

    def _format_generic_sheet(self, ws) -> None:
        if ws.max_row < 1:
            return
        header_fill = PatternFill(
            start_color="4472C4", end_color="4472C4", fill_type="solid"
        )
        header_font = Font(bold=True, color="FFFFFF")
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        self._auto_size_columns(ws)
        self._apply_sheet_column_layout(ws)
        ws.row_dimensions[1].height = 24
        ws.sheet_view.zoomScale = 115

    def _format_review_sheet(self, ws) -> None:
        if ws.max_row < 2:
            return
        divergence_col = None
        verdict_col = None
        header_values = [str(cell.value or "") for cell in ws[1]]
        divergence_col = None
        verdict_col = None
        subject_col_idx = None
        for idx, cell in enumerate(ws[1], 1):
            value = str(cell.value or "")
            if value == "Divergence":
                divergence_col = idx
            elif value == "Reviewer Verdict":
                verdict_col = idx
            elif value == "Subject":
                subject_col_idx = idx
        # Freeze panes after Subject so identity columns stay visible while scrolling right.
        freeze_after = subject_col_idx or 4
        ws.freeze_panes = f"{get_column_letter(min(freeze_after + 1, ws.max_column))}2"
        ws.auto_filter.ref = ws.dimensions
        if divergence_col is not None:
            data_range = f"A2:{get_column_letter(ws.max_column)}{ws.max_row}"
            divergence_letter = get_column_letter(divergence_col)
            for divergence_name in [
                "aligned",
                "field_conflict",
                "semantic_conflict",
                "presence_diff",
                "scope_variant",
                "judge_uncertain",
            ]:
                color = self._divergence_fill_color(divergence_name)
                if not color:
                    continue
                fill = PatternFill(start_color=color, end_color=color, fill_type="solid")
                ws.conditional_formatting.add(
                    data_range,
                    FormulaRule(
                        formula=[f'${divergence_letter}2="{divergence_name}"'],
                        fill=fill,
                    ),
                )
        _non_model_headers = {
            "#",
            "Status",
            "Category",
            "Subject",
            "Diverging Field(s)",
            "Why Flagged",
            "Evidence",
            "Judge Says",
            "Reviewer Verdict",
            "Reviewer Notes",
            "Issue Type",
            "Divergence",
            "Queue Signal",
            "Match Method",
            "Row ID",
            "Requirement",
            "",
        }
        model_headers = [h for h in header_values if h not in _non_model_headers]
        if verdict_col is not None:
            column_letter = get_column_letter(verdict_col)
            dynamic_verdicts = [f"accept-{h}" for h in model_headers if h]
            verdict_options = dynamic_verdicts or list(self.REVIEWER_VERDICTS)
            verdict_options = verdict_options + ["accept-both", "reject-all", "needs-escalation"]
            validation = DataValidation(
                type="list",
                formula1=f'"{",".join(verdict_options)}"',
                allow_blank=True,
            )
            validation.error = "Select a valid reviewer verdict from the dropdown."
            validation.errorTitle = "Invalid Verdict"
            ws.add_data_validation(validation)
            validation.add(f"{column_letter}2:{column_letter}{max(ws.max_row, 2)}")
        wrap_headers = {
            "Subject",
            "Why Flagged",
            "Reviewer Notes",
            "Judge Says",
            "Evidence",
            *model_headers,
        }
        for idx, cell in enumerate(ws[1], 1):
            if str(cell.value or "") in wrap_headers:
                letter = get_column_letter(idx)
                for row_idx in range(2, ws.max_row + 1):
                    ws[f"{letter}{row_idx}"].alignment = Alignment(wrap_text=True, vertical="top")
        for row_idx in range(2, ws.max_row + 1):
            ws.row_dimensions[row_idx].height = 44

    def _divergence_fill_color(self, divergence: str) -> str:
        mapping = {
            "aligned": self.COLORS["full_agreement"],
            "field_conflict": self.COLORS["disagreement"],
            "semantic_conflict": self.COLORS["partial_agreement"],
            "presence_diff": self.COLORS["missing"],
            "scope_variant": self.COLORS["scope_variant"],
            "judge_uncertain": self.COLORS["judge_uncertain"],
        }
        return mapping.get(str(divergence), "")

    def _generate_item_centric_csv(
        self,
        result: ComparisonResult,
        output_path: Path,
        include_missing_in_queue: bool,
        include_low_signal_presence_in_queue: bool = False,
    ) -> None:
        """Generate queue-first CSV used for review triage."""
        all_items = self._build_item_centric_df(result)
        queue = self._build_review_queue_df(
            all_items,
            include_missing=include_missing_in_queue,
            include_low_signal_presence=include_low_signal_presence_in_queue,
        )
        queue.to_csv(output_path, index=False)

    # --- Completeness Validation Methods ---

    def _build_potential_duplicates_df(
        self, result: ComparisonResult
    ) -> pd.DataFrame:
        """
        Build DataFrame for potential duplicates.

        Shows items that may be the same data extracted with different requirement_type.
        Example: gpt-5 has time__reclamation_deadline_days=60
                 gpt-4.1 has time__permit_validity_days=60
        """
        if not result.potential_duplicates:
            return pd.DataFrame()

        rows = []
        for dup in result.potential_duplicates:
            # Build a row showing the value and which models extracted it with what keys
            value_display = (
                f"{dup.value} {dup.unit}"
                if dup.unit != "N/A"
                else str(dup.value)
            )

            # Get requirement_type per model
            model_keys = {}
            for model, item in dup.items:
                req_type = item.get("requirement_type", "unknown")
                model_keys[model] = req_type

            row = {
                "Value": value_display,
                "Reason": dup.reason,
            }

            # Add requirement_type per model
            for model, key in model_keys.items():
                row[f"{model} Requirement"] = key

            rows.append(row)

        return pd.DataFrame(rows)

    def _build_expected_vs_found_df(
        self, result: ComparisonResult
    ) -> pd.DataFrame:
        """
        Build DataFrame showing expected vs found requirements.

        Shows which expected requirements were found by each model.
        """
        if not result.completeness:
            return pd.DataFrame()

        # Collect all expected requirements
        all_expected = set()
        all_found = {}  # {model: set of found keys}

        for model, comp_result in result.completeness.items():
            if comp_result.expected_total == 0:
                # No expected requirements configured
                return pd.DataFrame()

            all_found[model] = set()
            # The missing_expected are those NOT found (now just strings)
            missing_set = set()
            for missing in comp_result.missing_expected:
                missing_set.add(missing)
                all_expected.add(missing)

        # We need to get ALL expected from schema - but we only have missing
        # Let's rebuild expected from completeness data
        models = list(result.completeness.keys())

        # Build expected set from missing + found count
        # Since we know expected_found + len(missing_expected) = expected_total
        # We can use item_comparisons to find what each model extracted

        # Actually, simpler approach: group item comparisons by key and check presence
        item_keys_per_model = {}
        for model in models:
            item_keys_per_model[model] = set()

        for fc in result.item_comparisons:
            for model, value in fc.model_values.items():
                if value is not None and value != "":
                    item_keys_per_model.get(model, set()).add(fc.item_id)

        # Get expected from missing_expected in completeness results
        expected_reqs = set()
        for model, comp_result in result.completeness.items():
            for missing in comp_result.missing_expected:
                expected_reqs.add(missing)

        # Add found expected (those in item_comparisons that match expected format)
        # For now, just show the missing requirements across all models
        if not expected_reqs:
            # No missing requirements means all were found - show all as found
            # But we need to get the actual expected list from somewhere
            # For simplicity, return empty if no missing
            return pd.DataFrame()

        rows = []
        for expected_key in sorted(expected_reqs):
            row = {
                "Requirement": expected_key,
            }

            # Check which models are missing this
            models_missing = []
            for model, comp_result in result.completeness.items():
                if expected_key in comp_result.missing_expected:
                    row[model] = "❌ MISSING"
                    models_missing.append(model)
                else:
                    row[model] = "✓ Found"

            if len(models_missing) == len(models):
                row["Status"] = "NOT FOUND"
            elif len(models_missing) > 0:
                row["Status"] = "PARTIAL"
            else:
                row["Status"] = "FOUND"

            rows.append(row)

        return pd.DataFrame(rows)
