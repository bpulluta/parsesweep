"""
Universal utilities for saving and visualizing QA/QC validation reports.

Works with any document type and schema.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Any
from datetime import datetime

from .qa_qc import ValidationReport, FieldValidation

logger = logging.getLogger(__name__)


def save_validation_report(
    report: ValidationReport,
    output_dir: Path,
    entity_id: str
) -> Path:
    """
    Save validation report as JSON file.
    
    Args:
        report: ValidationReport object
        output_dir: Directory to save report
        entity_id: Entity identifier (document ID, jurisdiction, etc.)
        
    Returns:
        Path to saved report file
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    report_path = output_dir / f"{entity_id}_validation_report.json"
    
    # Convert report to dictionary
    report_dict = {
        "entity_identifier": report.entity_identifier,
        "timestamp": datetime.now().isoformat(),
        "overall_confidence": report.overall_confidence,
        "validation_summary": {
            "total_fields_extracted": report.total_fields,
            "fields_cross_validated": report.fields_validated,
            "discrepancies_found": report.discrepancies,
            "overrides_applied": report.overrides_applied,
            "flags_for_review": report.flags_for_review
        },
        "field_validations": [
            {
                "field": v.field_path,
                "openai_value": v.openai_value,
                "langextract_value": v.langextract_value,
                "confidence": v.confidence,
                "status": v.status,
                "source_citation": v.source_citation,
                "reason": v.reason
            }
            for v in report.field_validations
        ],
        "recommendations": report.recommendations
    }
    
    with open(report_path, 'w') as f:
        json.dump(report_dict, f, indent=2)
    
    logger.info(f"  ✓ Saved validation report: {report_path}")
    return report_path


def generate_validation_summary_html(
    report: ValidationReport,
    output_dir: Path,
    entity_id: str
) -> Path:
    """
    Generate human-readable HTML summary of validation report.
    
    Args:
        report: ValidationReport object
        output_dir: Directory to save HTML
        entity_id: Entity identifier (document ID, jurisdiction, etc.)
        
    Returns:
        Path to saved HTML file
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    html_path = output_dir / f"{entity_id}_validation_summary.html"
    
    # Generate HTML
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>QA/QC Validation Report - {entity_id}</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            max-width: 1200px;
            margin: 40px auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 30px;
        }}
        .header h1 {{
            margin: 0;
            font-size: 2em;
        }}
        .header .subtitle {{
            margin-top: 10px;
            opacity: 0.9;
        }}
        .summary-cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .card h3 {{
            margin: 0 0 10px 0;
            font-size: 0.9em;
            color: #666;
            text-transform: uppercase;
        }}
        .card .value {{
            font-size: 2em;
            font-weight: bold;
            color: #333;
        }}
        .confidence {{
            font-size: 3em;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        .section {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .section h2 {{
            margin-top: 0;
            color: #333;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
        }}
        th {{
            background: #667eea;
            color: white;
            padding: 12px;
            text-align: left;
            font-weight: 600;
        }}
        td {{
            padding: 12px;
            border-bottom: 1px solid #eee;
        }}
        tr:hover {{
            background: #f8f9fa;
        }}
        .status-validated {{
            color: #28a745;
            font-weight: bold;
        }}
        .status-overridden {{
            color: #17a2b8;
            font-weight: bold;
        }}
        .status-flagged {{
            color: #ffc107;
            font-weight: bold;
        }}
        .status-error {{
            color: #dc3545;
            font-weight: bold;
        }}
        .recommendation {{
            padding: 15px;
            margin: 10px 0;
            border-radius: 5px;
            background: #f8f9fa;
            border-left: 4px solid #667eea;
        }}
        .citation {{
            font-size: 0.9em;
            color: #666;
            font-style: italic;
            max-width: 400px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>QA/QC Validation Report</h1>
        <div class="subtitle">{entity_id} - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>
    </div>
    
    <div class="summary-cards">
        <div class="card">
            <h3>Overall Confidence</h3>
            <div class="value confidence">{report.overall_confidence:.0%}</div>
        </div>
        <div class="card">
            <h3>Fields Validated</h3>
            <div class="value">{report.fields_validated}/{report.total_fields}</div>
        </div>
        <div class="card">
            <h3>Overrides Applied</h3>
            <div class="value" style="color: #17a2b8;">{report.overrides_applied}</div>
        </div>
        <div class="card">
            <h3>Flags for Review</h3>
            <div class="value" style="color: #ffc107;">{report.flags_for_review}</div>
        </div>
    </div>
    
    <div class="section">
        <h2>Recommendations</h2>
        {''.join(f'<div class="recommendation">{rec}</div>' for rec in report.recommendations)}
    </div>
    
    <div class="section">
        <h2>Field Validation Details</h2>
        <table>
            <thead>
                <tr>
                    <th>Field</th>
                    <th>OpenAI Value</th>
                    <th>LangExtract Value</th>
                    <th>Confidence</th>
                    <th>Status</th>
                    <th>Source Citation</th>
                </tr>
            </thead>
            <tbody>
"""
    
    # Add field validation rows
    for validation in report.field_validations:
        status_class = f"status-{validation.status}"
        citation = validation.source_citation if validation.source_citation else "N/A"
        citation_html = f'<div class="citation" title="{citation}">{citation}</div>'
        
        html += f"""
                <tr>
                    <td><code>{validation.field_path}</code></td>
                    <td>{validation.openai_value}</td>
                    <td>{validation.langextract_value if validation.langextract_value is not None else 'N/A'}</td>
                    <td>{validation.confidence:.0%}</td>
                    <td class="{status_class}">{validation.status.replace('_', ' ').title()}</td>
                    <td>{citation_html}</td>
                </tr>
"""
    
    html += """
            </tbody>
        </table>
    </div>
</body>
</html>
"""
    
    with open(html_path, 'w') as f:
        f.write(html)
    
    logger.info(f"  ✓ Saved HTML summary: {html_path}")
    return html_path


def compare_with_ground_truth(
    extracted_data: Dict[str, Any],
    ground_truth: Dict[str, Any],
    entity_id: str
) -> Dict[str, Any]:
    """
    Compare extracted data with ground truth for accuracy assessment.
    
    Args:
        extracted_data: Extracted document data
        ground_truth: Known correct values
        entity_id: Entity identifier (document ID, jurisdiction, etc.)
        
    Returns:
        Comparison results with accuracy metrics
    """
    comparison = {
        "entity_identifier": entity_id,
        "accuracy_metrics": {},
        "field_comparisons": []
    }
    
    # Find main array field dynamically (works with any schema)
    main_array_key = None
    for key, value in extracted_data.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            main_array_key = key
            break
    
    if not main_array_key:
        # No array data to compare
        comparison["accuracy_metrics"] = {
            "total_fields": 0,
            "correct_fields": 0,
            "accuracy": 0,
            "error_rate": 0
        }
        return comparison
    
    # Compare items in main array
    extracted_items = extracted_data.get(main_array_key, [])
    truth_items = ground_truth.get(main_array_key, [])
    
    matches = 0
    total = 0
    
    for i, (ext_item, truth_item) in enumerate(zip(extracted_items, truth_items)):
        for field in truth_item.keys():
            total += 1
            ext_val = ext_item.get(field)
            truth_val = truth_item.get(field)
            
            # Compare with tolerance for numbers
            if isinstance(truth_val, (int, float)) and isinstance(ext_val, (int, float)):
                match = abs(ext_val - truth_val) / max(abs(truth_val), 0.001) < 0.05
            else:
                match = ext_val == truth_val
            
            if match:
                matches += 1
            
            comparison["field_comparisons"].append({
                "item_index": i,
                "field": field,
                "extracted": ext_val,
                "ground_truth": truth_val,
                "match": match
            })
    
    comparison["accuracy_metrics"] = {
        "total_fields": total,
        "correct_fields": matches,
        "accuracy": matches / total if total > 0 else 0,
        "error_rate": (total - matches) / total if total > 0 else 0
    }
    
    return comparison
