"""
QA/QC Multi-Model Validation Module.

Provides multi-model validation for document extraction by:

1. Running extraction with multiple AI models
2. Comparing outputs field-by-field
3. Generating comparison reports highlighting discrepancies

Model tiers are resolved exclusively through the unified
:class:`~psweep.config.model_registry.ModelRegistry` (built from the run
config's ``models:`` block and the ``qaqc.models:`` reference list). There is
no environment-variable model source and no provider is threaded directly —
credentials come from ``registry.to_llm_kwargs``.

Examples
--------
.. code-block:: python

    from psweep.config.model_registry import ModelRegistry
    from psweep.qa_qc import run_multi_model_extraction

    registry = ModelRegistry.from_config(config_dict, llm_config)

    results = run_multi_model_extraction(
        doc_text="...",
        doc_name="document_name",
        schema=schema,
        registry=registry,
        model_tiers=["primary", "secondary"],
        output_dir=Path("processed/"),
    )

Notes
-----
Submodules:

- multi_model_extractor: Run extraction with multiple models via the registry
- comparison_engine: Compare outputs from multiple models
- report_generator: Generate comparison reports
- utils: QA/QC utilities including companion schema finder
"""

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
    "run_multi_model_extraction",
    "ModelExtractionResult",
    "ComparisonEngine",
    "ComparisonResult",
    "FieldComparison",
    "ReportGenerator",
]

__version__ = "2.0.0"
