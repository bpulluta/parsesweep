"""
QA/QC Multi-Model Validation Module.

Provides multi-model validation for document extraction by:
1. Running extraction with multiple AI models
2. Comparing outputs field-by-field
3. Generating comparison reports highlighting discrepancies

Usage:
    from psweep.qa_qc import ModelDetector, run_multi_model_extraction

    # Requires QAQC_MODELS env var — raises ValueError if not set
    models = ModelDetector.get_qa_models()

    provider = ModelDetector.get_provider()

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
    - model_detector: Detect QA/QC models from QAQC_MODELS env var
    - multi_model_extractor: Run extraction with multiple models
    - comparison_engine: Compare outputs from multiple models
    - report_generator: Generate comparison reports
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
