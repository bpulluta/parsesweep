"""
Report Generator for QA/QC Multi-Model Validation.

Generates Excel/CSV reports with color-coded comparison results
for human review.

This module is part of Phase 4 of the QA/QC Multi-Model Implementation Plan.

Usage:
    from streamline_extract.qa_qc.report_generator import ReportGenerator
    from streamline_extract.qa_qc.comparison_engine import ComparisonResult
    
    generator = ReportGenerator()
    excel_path, csv_path = generator.generate_report(
        comparison_result=result,
        output_dir=Path("processed/qa_qc/austin_energy")
    )

Output Files:
    processed/qa_qc/{doc_name}/
        comparison_report.xlsx  # Color-coded Excel with 3 sheets
        comparison_report.csv   # Plain CSV with all comparisons

Excel Sheets:
    1. Summary - Key metrics (agreement %, items per model, etc.)
    2. Context Comparison - Document-level fields (jurisdiction, etc.)
    3. Item Comparison - Main data array items

Color Coding:
    - Green: Full agreement (N/N)
    - Yellow: Partial agreement (>50%)
    - Red: Low agreement (≤50%) or disagreement
    - Gray: Item missing from one or more models

Status: Phase 4 - COMPLETED
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
from openpyxl import load_workbook
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
    
    Status: Phase 4 - COMPLETED (v2: Item-centric format)
    """

    # Color scheme for Excel formatting
    COLORS = {
        "full_agreement": "C6EFCE",     # Light green
        "partial_agreement": "FFEB9C",  # Light yellow
        "disagreement": "FFC7CE",       # Light red
        "missing": "D9D9D9",            # Gray
    }

    def generate_report(
        self,
        comparison_result: ComparisonResult,
        output_dir: Path,
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
        
        # Generate Excel with formatting
        self._generate_excel(comparison_result, excel_path)
        logger.info(f"Generated Excel report: {excel_path}")
        
        # Generate CSV - now item-centric format
        self._generate_item_centric_csv(comparison_result, csv_path)
        logger.info(f"Generated CSV report: {csv_path}")
        
        return excel_path, csv_path

    def _generate_excel(self, result: ComparisonResult, output_path: Path) -> None:
        """
        Generate Excel with multiple sheets and formatting.
        
        Creates up to 4 sheets:
        1. Summary - Key metrics
        2. Item Comparison - Item-centric comparison (one row per item)
        3. Potential Duplicates - Items that may be same data with different keys (Phase 8)
        4. Expected vs Found - Completeness analysis per expected requirement (Phase 8)
        """
        # Build DataFrames
        summary_df = self._build_summary_df(result)
        item_df = self._build_item_centric_df(result)
        duplicates_df = self._build_potential_duplicates_df(result)
        expected_df = self._build_expected_vs_found_df(result)
        
        # Write to Excel
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            summary_df.to_excel(writer, sheet_name="Summary", index=False)
            
            if not item_df.empty:
                item_df.to_excel(writer, sheet_name="Item Comparison", index=False)
            
            if not duplicates_df.empty:
                duplicates_df.to_excel(writer, sheet_name="Potential Duplicates", index=False)
            
            if not expected_df.empty:
                expected_df.to_excel(writer, sheet_name="Expected vs Found", index=False)
        
        # Apply formatting
        self._apply_formatting_item_centric(output_path, len(result.models))

    def _build_summary_df(self, result: ComparisonResult) -> pd.DataFrame:
        """Build summary metrics DataFrame."""
        summary = result.summary
        
        rows = [
            {"Metric": "Document", "Value": result.document_name},
            {"Metric": "Models", "Value": ", ".join(result.models)},
        ]
        
        # Add items per model
        items_per_model = summary.get("items_per_model", {})
        for model, count in items_per_model.items():
            rows.append({"Metric": f"Total Items ({model})", "Value": count})
        
        rows.extend([
            {"Metric": "Total Comparisons", "Value": summary.get("total_comparisons", 0)},
            {"Metric": "Full Agreement Count", "Value": summary.get("full_agreement_count", 0)},
            {"Metric": "Full Agreement %", "Value": f"{summary.get('full_agreement_pct', 0):.1f}%"},
            {"Metric": "Needs Review Count", "Value": summary.get("needs_review_count", 0)},
            {"Metric": "Needs Review %", "Value": f"{summary.get('needs_review_pct', 0):.1f}%"},
            {"Metric": "", "Value": ""},  # Blank row
            {"Metric": "Context Comparisons", "Value": summary.get("context_comparisons", 0)},
            {"Metric": "Context Agreement %", "Value": f"{summary.get('context_agreement_pct', 0):.1f}%"},
            {"Metric": "Item Comparisons", "Value": summary.get("item_comparisons", 0)},
            {"Metric": "Item Agreement %", "Value": f"{summary.get('item_agreement_pct', 0):.1f}%"},
        ])
        
        # Phase 8: Add potential duplicates count
        potential_duplicates_count = summary.get("potential_duplicates_count", 0)
        if potential_duplicates_count > 0:
            rows.extend([
                {"Metric": "", "Value": ""},  # Blank row
                {"Metric": "--- Potential Duplicates ---", "Value": ""},
                {"Metric": "Potential Duplicates", "Value": potential_duplicates_count},
            ])
        
        # Phase 8: Add completeness per model
        completeness_per_model = summary.get("completeness_per_model", {})
        if completeness_per_model:
            rows.extend([
                {"Metric": "", "Value": ""},  # Blank row
                {"Metric": "--- Completeness Metrics ---", "Value": ""},
            ])
            for model, metrics in completeness_per_model.items():
                expected_total = metrics.get("expected_total", 0)
                if expected_total > 0:
                    rows.append({
                        "Metric": f"Completeness ({model})",
                        "Value": f"{metrics.get('expected_found', 0)}/{expected_total} ({metrics.get('completeness_score', 0):.1f}%)"
                    })
        
        return pd.DataFrame(rows)

    def _build_comparison_df(
        self, 
        comparisons: List[FieldComparison], 
        models: List[str]
    ) -> pd.DataFrame:
        """
        Build comparison DataFrame from FieldComparison list.
        
        Args:
            comparisons: List of FieldComparison objects
            models: List of model names for column ordering
            
        Returns:
            DataFrame with columns: Item ID, Field Path, Model values..., Agreement, Needs Review, Notes
        """
        if not comparisons:
            return pd.DataFrame()
        
        rows = []
        for fc in comparisons:
            row = {
                "Item ID": self._truncate_value(fc.item_id),
                "Field Path": fc.field_path,
            }
            
            # Add model values in consistent order
            for model in models:
                value = fc.model_values.get(model, "N/A")
                row[f"Model: {model}"] = self._truncate_value(value)
            
            row["Agreement"] = fc.agreement_score
            row["Needs Review"] = "YES" if fc.needs_review else "NO"
            row["Notes"] = fc.notes or ""
            
            rows.append(row)
        
        return pd.DataFrame(rows)

    def _truncate_value(self, value: Any) -> str:
        """Truncate long values for readability."""
        if value is None:
            return ""
        
        str_value = str(value)
        if len(str_value) > MAX_VALUE_LENGTH:
            return str_value[:MAX_VALUE_LENGTH - 3] + "..."
        return str_value

    def _apply_formatting(self, excel_path: Path, num_models: int) -> None:
        """
        Apply color coding and formatting to Excel.
        
        Colors rows based on agreement level:
        - Green: Full agreement (N/N)
        - Yellow: Partial agreement (>50%)
        - Red: Low agreement (≤50%)
        - Gray: Missing items
        """
        wb = load_workbook(excel_path)
        
        # Format Summary sheet
        if "Summary" in wb.sheetnames:
            self._format_summary_sheet(wb["Summary"])
        
        # Format comparison sheets
        for sheet_name in ["Context Comparison", "Item Comparison"]:
            if sheet_name in wb.sheetnames:
                self._format_comparison_sheet(wb[sheet_name], num_models)
        
        wb.save(excel_path)

    def _format_summary_sheet(self, ws) -> None:
        """Format the Summary sheet."""
        # Bold headers
        for cell in ws[1]:
            cell.font = Font(bold=True)
        
        # Bold metric names
        for row in ws.iter_rows(min_row=2):
            row[0].font = Font(bold=True)
        
        # Auto-size columns
        self._auto_size_columns(ws)

    def _format_comparison_sheet(self, ws, num_models: int) -> None:
        """Format a comparison sheet with color coding."""
        if ws.max_row < 2:
            return
        
        # Bold headers
        header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF")
        
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        
        # Find Agreement and Notes columns
        agreement_col = None
        notes_col = None
        for idx, cell in enumerate(ws[1], 1):
            if cell.value == "Agreement":
                agreement_col = idx
            elif cell.value == "Notes":
                notes_col = idx
        
        if not agreement_col:
            self._auto_size_columns(ws)
            return
        
        # Apply conditional formatting row by row
        for row_idx in range(2, ws.max_row + 1):
            agreement_cell = ws.cell(row_idx, agreement_col)
            agreement_value = str(agreement_cell.value) if agreement_cell.value else ""
            
            # Check for missing items
            notes_value = ""
            if notes_col:
                notes_cell = ws.cell(row_idx, notes_col)
                notes_value = str(notes_cell.value).lower() if notes_cell.value else ""
            
            # Determine color
            fill_color = self._get_row_color(agreement_value, notes_value, num_models)
            
            if fill_color:
                fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
                for col_idx in range(1, ws.max_column + 1):
                    ws.cell(row_idx, col_idx).fill = fill
        
        # Auto-size columns
        self._auto_size_columns(ws)

    def _get_row_color(self, agreement: str, notes: str, num_models: int) -> str:
        """
        Determine row color based on agreement level.
        
        Args:
            agreement: Agreement score string (e.g., "2/3")
            notes: Notes string (for detecting missing items)
            num_models: Total number of models
            
        Returns:
            Color hex code or empty string for no fill
        """
        # Check for missing items
        if "missing" in notes.lower():
            return self.COLORS["missing"]
        
        # Parse agreement score
        if "/" not in agreement:
            return ""
        
        try:
            parts = agreement.split("/")
            agreed = int(parts[0])
            total = int(parts[1])
        except (ValueError, IndexError):
            return ""
        
        # Determine color based on agreement ratio
        if agreed == total:
            return self.COLORS["full_agreement"]
        elif agreed > total / 2:
            return self.COLORS["partial_agreement"]
        else:
            return self.COLORS["disagreement"]

    def _auto_size_columns(self, ws) -> None:
        """Auto-size columns based on content."""
        for column_cells in ws.columns:
            max_length = 0
            column_letter = get_column_letter(column_cells[0].column)
            
            for cell in column_cells:
                try:
                    cell_length = len(str(cell.value)) if cell.value else 0
                    max_length = max(max_length, cell_length)
                except (TypeError, AttributeError):
                    pass
            
            # Set width with min/max bounds
            adjusted_width = min(max(max_length + 2, 10), 50)
            ws.column_dimensions[column_letter].width = adjusted_width

    def _build_item_centric_df(self, result: ComparisonResult) -> pd.DataFrame:
        """
        Build item-centric DataFrame - one row per item, not per field.
        
        Combines value+unit into a single display (e.g., "1320 feet").
        Includes source_text columns for each model.
        Uses single "Requirement" column for compound keys.
        Used by both Excel and CSV generation.
        """
        if not result.item_comparisons:
            return pd.DataFrame(columns=["Status", "Requirement", "Agreement", "Notes"])
        
        # Group comparisons by item_id
        items_data: Dict[str, Dict[str, Any]] = {}
        for fc in result.item_comparisons:
            item_id = fc.item_id
            if item_id not in items_data:
                items_data[item_id] = {
                    "field_comparisons": [],
                    "models_present": set(),
                    "models_missing": set(),
                }
            items_data[item_id]["field_comparisons"].append(fc)
            
            # Track which models have/don't have this item
            for model, value in fc.model_values.items():
                if value is not None and value != "":
                    items_data[item_id]["models_present"].add(model)
                else:
                    items_data[item_id]["models_missing"].add(model)
        
        # Build item-centric rows
        rows = []
        models = result.models
        
        # Shorten model names for display (e.g., "compassop-gpt-5" -> "gpt-5")
        short_names = {}
        for m in models:
            parts = m.split("-")
            if len(parts) >= 2:
                short_names[m] = "-".join(parts[-2:]) if "gpt" in m.lower() else parts[-1]
            else:
                short_names[m] = m
        
        for item_id, data in sorted(items_data.items()):
            # item_id is the compound key (e.g., "setback__property_line_ft")
            requirement = item_id
            
            # Get field values for each model
            model_fields: Dict[str, Dict[str, Any]] = {m: {} for m in models}
            for fc in data["field_comparisons"]:
                field_name = fc.field_path.split(".")[-1].lower()
                for model, value in fc.model_values.items():
                    model_fields[model][field_name] = value
            
            # Build combined value display for each model (value only, unit is in requirement type)
            model_displays = {}
            model_sources = {}
            for model in models:
                fields = model_fields[model]
                value = fields.get("value", "")
                unit = fields.get("unit", "")
                source_text = fields.get("source_text", "")
                
                if value is None or value == "":
                    model_displays[model] = "-"
                elif unit and unit not in ["", None]:
                    model_displays[model] = f"{value} {unit}"
                else:
                    model_displays[model] = str(value)
                
                # Truncate source_text for display (80 chars max)
                if source_text and source_text not in ["", None]:
                    model_sources[model] = str(source_text)[:80] + ("..." if len(str(source_text)) > 80 else "")
                else:
                    model_sources[model] = "-"
            
            # Determine status
            all_present = len(data["models_missing"]) == 0
            all_values_match = len(set(d for d in model_displays.values() if d != "-")) <= 1
            
            if not all_present:
                # Some models missing
                missing = list(data["models_missing"])
                present = list(data["models_present"])
                if len(present) == 1:
                    status = f"ONLY {short_names[present[0]]}"
                else:
                    status = "PARTIAL"
            elif all_values_match:
                status = "AGREE"
            else:
                status = "DIFFER"
            
            # Calculate agreement percentage
            non_empty = [d for d in model_displays.values() if d != "-"]
            if len(non_empty) >= 2:
                unique_values = set(non_empty)
                if len(unique_values) == 1:
                    agreement = "100%"
                else:
                    agreement = "0%"
            else:
                agreement = "-"
            
            # Build notes
            notes = ""
            if not all_present:
                missing_names = [short_names[m] for m in data["models_missing"]]
                notes = f"Not in: {', '.join(missing_names)}"
            elif not all_values_match:
                notes = "Values differ"
            
            row = {
                "Status": status,
                "Requirement": requirement,
            }
            
            # Add model value columns with shortened names
            for model in models:
                col_name = short_names[model]
                row[col_name] = model_displays[model]
            
            row["Agreement"] = agreement
            
            # Add source_text columns for each model
            for model in models:
                col_name = f"{short_names[model]} Source"
                row[col_name] = model_sources.get(model, "-")
            
            row["Notes"] = notes
            
            rows.append(row)
        
        # Sort: AGREE first, then DIFFER, then ONLY/PARTIAL
        status_order = {"AGREE": 0, "DIFFER": 1}
        rows.sort(key=lambda r: (
            status_order.get(r["Status"], 2),
            r["Requirement"]
        ))
        
        return pd.DataFrame(rows)

    def _apply_formatting_item_centric(self, excel_path: Path, num_models: int) -> None:
        """
        Apply color coding and formatting to item-centric Excel.
        
        Colors rows based on Status:
        - Green: AGREE
        - Yellow: PARTIAL or DIFFER
        - Gray: ONLY (missing from one model)
        """
        wb = load_workbook(excel_path)
        
        # Format Summary sheet
        if "Summary" in wb.sheetnames:
            self._format_summary_sheet(wb["Summary"])
        
        # Format Item Comparison sheet with item-centric coloring
        if "Item Comparison" in wb.sheetnames:
            self._format_item_centric_sheet(wb["Item Comparison"])
        
        wb.save(excel_path)

    def _format_item_centric_sheet(self, ws) -> None:
        """Format the item-centric comparison sheet with color coding."""
        if ws.max_row < 2:
            return
        
        # Bold headers with blue background
        header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF")
        
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        
        # Find Status column
        status_col = None
        for idx, cell in enumerate(ws[1], 1):
            if cell.value == "Status":
                status_col = idx
                break
        
        if not status_col:
            self._auto_size_columns(ws)
            return
        
        # Apply conditional formatting row by row based on Status
        for row_idx in range(2, ws.max_row + 1):
            status_cell = ws.cell(row_idx, status_col)
            status_value = str(status_cell.value) if status_cell.value else ""
            
            # Determine color based on status
            if status_value == "AGREE":
                fill_color = self.COLORS["full_agreement"]  # Green
            elif status_value == "DIFFER":
                fill_color = self.COLORS["disagreement"]  # Red
            elif status_value == "PARTIAL":
                fill_color = self.COLORS["partial_agreement"]  # Yellow
            elif status_value.startswith("ONLY"):
                fill_color = self.COLORS["missing"]  # Gray
            else:
                fill_color = None
            
            if fill_color:
                fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")
                for col_idx in range(1, ws.max_column + 1):
                    ws.cell(row_idx, col_idx).fill = fill
        
        # Auto-size columns
        self._auto_size_columns(ws)

    def _generate_csv(self, result: ComparisonResult, output_path: Path) -> None:
        """
        Generate CSV (same data as Excel but no formatting).
        
        Combines context and item comparisons into a single CSV.
        """
        # Combine all comparisons
        all_comparisons = result.context_comparisons + result.item_comparisons
        
        if not all_comparisons:
            # Create empty CSV with headers
            pd.DataFrame(columns=["Item ID", "Field Path", "Agreement", "Needs Review", "Notes"]).to_csv(
                output_path, index=False
            )
            return
        
        # Build combined DataFrame
        df = self._build_comparison_df(all_comparisons, result.models)
        
        # Add a Type column to distinguish context vs item comparisons
        df.insert(0, "Type", ["Context" if i < len(result.context_comparisons) else "Item" 
                               for i in range(len(all_comparisons))])
        
        df.to_csv(output_path, index=False)

    def _generate_item_centric_csv(self, result: ComparisonResult, output_path: Path) -> None:
        """
        Generate ITEM-CENTRIC CSV - one row per item, not per field.
        
        Uses the shared _build_item_centric_df method for consistency
        with Excel output.
        """
        df = self._build_item_centric_df(result)
        df.to_csv(output_path, index=False)

    # --- Phase 8: New Methods for Completeness Validation ---

    def _build_potential_duplicates_df(self, result: ComparisonResult) -> pd.DataFrame:
        """
        Build DataFrame for potential duplicates (Phase 8).
        
        Shows items that may be the same data extracted with different requirement_type.
        Example: gpt-5 has time__reclamation_deadline_days=60
                 gpt-4.1 has time__permit_validity_days=60
        """
        if not result.potential_duplicates:
            return pd.DataFrame()
        
        rows = []
        for dup in result.potential_duplicates:
            # Build a row showing the value and which models extracted it with what keys
            value_display = f"{dup.value} {dup.unit}" if dup.unit != "N/A" else str(dup.value)
            
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

    def _build_expected_vs_found_df(self, result: ComparisonResult) -> pd.DataFrame:
        """
        Build DataFrame showing expected vs found requirements (Phase 8).
        
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
        
        # Get expected from first model's data
        first_model = models[0]
        first_result = result.completeness[first_model]
        
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

