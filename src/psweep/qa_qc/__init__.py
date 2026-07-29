"""
QA/QC Multi-Model Validation Module.

This module provides multi-model validation for document extraction by:
1. Running extraction with multiple AI models
2. Comparing outputs field-by-field
3. Generating comparison reports highlighting discrepancies

Version: 2.0.0 (New implementation replacing old LangExtract-based QA/QC)

Usage:
    from psweep.qa_qc import ModelDetector, run_multi_model_extraction

    # Get models for QA/QC (auto-detects from environment or uses defaults)
    models = ModelDetector.get_qa_models()

    # Get current provider
    provider = ModelDetector.get_provider()

    # Run multi-model extraction (Phase 2)
    results = run_multi_model_extraction(
        doc_text="...",
        doc_name="document_name",
        schema=schema,
        models=models,
        output_dir=Path("processed/"),
        api_key="...",
        provider=provider,
    )

Submodules:
    - model_detector: Auto-detect QA/QC models from environment
    - multi_model_extractor: Run extraction with multiple models (Phase 2)
    - comparison_engine: Compare outputs from multiple models (Phase 3)
    - report_generator: Generate comparison reports (Phase 4)
    - utils: QA/QC utilities including companion schema finder
"""

from .model_detector import ModelDetector
from .multi_model_extractor import (
    run_multi_model_extraction,
    ModelExtractionResult,
)
from .comparison_engine import (
    ComparisonEngine,
    ComparisonResult,
    FieldComparison,
)
from .report_generator import ReportGenerator

__all__ = [
    "ModelDetector",
    "run_multi_model_extraction",
    "ModelExtractionResult",
    "ComparisonEngine",
    "ComparisonResult",
    "FieldComparison",
    "ReportGenerator",
]

__version__ = "2.0.0"
