"""
Multi-Model Extractor for QA/QC Validation.

Runs document extraction with multiple AI models and saves outputs
to organized subfolders for comparison.

Model tiers and credentials are resolved exclusively through the unified
:class:`~psweep.config.model_registry.ModelRegistry`. Callers pass the registry
plus the ``qaqc.models`` tier references; this module resolves each tier to a
concrete model and its LLM kwargs (provider/api_key/endpoint) via
``registry.to_llm_kwargs`` — never threading a provider directly.

Usage:
    from psweep.qa_qc.multi_model_extractor import run_multi_model_extraction

    output_files = run_multi_model_extraction(
        doc_text="Full document text...",
        doc_name="austin_energy_tariff",
        schema=loaded_schema,
        registry=registry,
        model_tiers=["primary", "secondary"],
        output_dir=Path("processed/qa_qc"),
    )

Output Structure:
    processed/qa_qc/{doc_name}/
        {model_name}.json  (one file per model)
        metadata.json
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence

from .utils import sanitize_model_name

if TYPE_CHECKING:
    from ..config.model_registry import ModelRegistry
from ..utils.error_taxonomy import (
    build_error_record,
    normalize_error_records,
    summarize_error_records,
)

logger = logging.getLogger(__name__)


@dataclass
class ModelExtractionResult:
    """Result from a single model extraction."""

    model: str
    success: bool
    output_path: Optional[Path]
    data: Optional[Dict[str, Any]]
    cost: float
    processing_time: float
    error: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None
    result: Optional[Any] = None


def run_multi_model_extraction(
    doc_text: str,
    doc_name: str,
    schema: dict,
    registry: "ModelRegistry",
    model_tiers: Sequence[str],
    output_dir: Path,
    max_context_chars: int = 400000,
    runtime_artifact: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
) -> Dict[str, ModelExtractionResult]:
    """
    Run extraction with multiple models resolved through the model registry.

    Args:
        doc_text: Full document text to extract from
        doc_name: Document name (without extension, used for output folder)
        schema: JSON schema for extraction
        registry: Unified :class:`ModelRegistry` — the single source of model
            tier resolution and credential (LLM kwargs) threading.
        model_tiers: Ordered list of tier/model references (e.g. the
            ``qaqc.models`` list). Resolved and de-duplicated by concrete model.
        output_dir: Base directory for QA/QC outputs (e.g., "processed/")
        max_context_chars: Maximum characters to process
        runtime_artifact: Optional compiled runtime artifact for lineage metadata
        run_id: Optional deterministic run identifier for this invocation

    Returns:
        Dict mapping concrete model name to ModelExtractionResult
    """
    # Import here to avoid circular imports
    from ..extraction import DocumentExtractor

    # Resolve tier references -> concrete model definitions (dedup by model).
    model_defs = registry.get_models(list(model_tiers))
    models = [definition.model for definition in model_defs]

    # Create output directory for this document
    doc_output_dir = Path(output_dir) / "qa_qc" / doc_name
    doc_output_dir.mkdir(parents=True, exist_ok=True)

    results: Dict[str, ModelExtractionResult] = {}
    total_start = time.time()

    logger.info(
        f"Starting multi-model extraction with {len(models)} models: {models}"
    )

    for definition in model_defs:
        model = definition.model
        model_start = time.time()
        safe_model_name = sanitize_model_name(model)
        output_path = doc_output_dir / f"{safe_model_name}.json"

        logger.info(f"Running extraction with {model}...")

        # Resolve credentials/provider through the single, env-driven path.
        llm_kwargs = registry.to_llm_kwargs(definition.tier)
        provider = llm_kwargs.get("provider", "openai")

        try:
            # Create a new extractor for this model
            extractor = DocumentExtractor(
                api_key=llm_kwargs.get("api_key"),
                model=llm_kwargs.get("model", model),
                max_context_chars=max_context_chars,
                provider=provider,
                azure_endpoint=llm_kwargs.get("azure_endpoint"),
                azure_api_version=llm_kwargs.get("azure_api_version"),
            )

            # Run extraction
            result = extractor.extract(
                text=doc_text,
                schema=schema,
            )

            extracted_at = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )
            schema_id = schema.get("$id") if isinstance(schema, dict) else None
            lineage = {
                "artifact_id": (runtime_artifact or {}).get("artifact_id")
                or "artifact://runtime/unresolved",
                "profile_id": (
                    (runtime_artifact or {}).get("lineage") or {}
                ).get("profile_id")
                or "default",
                "run_id": run_id or f"run://{doc_name}",
                "model": model,
                "provider": provider,
                "schema_id": schema_id,
                "extracted_at": extracted_at,
            }

            # Save canonical extraction-record output per model.
            output_data = {
                "record_id": f"record://{lineage['run_id'].replace('run://', '')}/{doc_name}/{safe_model_name}",
                "contract_version": "1.0.0",
                "document": {
                    "source_document_id": doc_name,
                    "source_path": doc_name,
                    "source_filename": doc_name,
                },
                "lineage": lineage,
                "payload": result.data,
                "quality": {
                    "overall_confidence": result.completeness_score,
                    "warnings": result.validation_notes or [],
                    "errors": normalize_error_records(
                        getattr(result, "processing_errors", None)
                        or getattr(result, "errors", None)
                    ),
                },
                "processing_metrics": {
                    "duration_seconds": result.processing_time,
                    "cost_usd": result.cost,
                    "input_tokens": None,
                    "output_tokens": None,
                },
            }

            # Save result to JSON file
            with open(output_path, "w") as f:
                json.dump(output_data, f, indent=2)

            processing_time = time.time() - model_start

            results[model] = ModelExtractionResult(
                model=model,
                success=True,
                output_path=output_path,
                data=result.data,
                cost=result.cost,
                processing_time=processing_time,
                result=result,
            )

            logger.info(
                f"✓ {model} completed: {output_path.name} "
                f"(${result.cost:.4f}, {processing_time:.1f}s)"
            )

        except Exception as e:
            processing_time = time.time() - model_start
            error_msg = str(e)
            error_details = build_error_record(
                e,
                stage="qa_qc",
                document_path=doc_name,
                model=model,
                provider=provider,
            )

            results[model] = ModelExtractionResult(
                model=model,
                success=False,
                output_path=None,
                data=None,
                cost=0.0,
                processing_time=processing_time,
                error=error_msg,
                error_details=error_details,
            )

            logger.error(f"✗ {model} failed: {error_msg}")

    # Calculate totals
    total_time = time.time() - total_start
    total_cost = sum(r.cost for r in results.values())
    successful = sum(1 for r in results.values() if r.success)
    error_summary = summarize_error_records(
        r.error_details
        for r in results.values()
        if r.error_details is not None
    )

    # Save metadata
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "document": doc_name,
        "models": models,
        "version": "2.0.0",
        "run_id": run_id,
        "artifact_id": runtime_artifact.get("artifact_id")
        if runtime_artifact
        else None,
        "lineage": {
            **runtime_artifact.get("lineage", {}),
            "run_id": run_id,
        }
        if runtime_artifact
        else None,
        "contract_versions": runtime_artifact.get("contract_versions")
        if runtime_artifact
        else None,
        "status": "completed" if successful == len(models) else "partial",
        "summary": {
            "total_models": len(models),
            "successful": successful,
            "failed": len(models) - successful,
            "total_errors": error_summary["total_errors"],
            "total_cost": total_cost,
            "total_time": total_time,
        },
        "model_errors": {
            model: result.error_details
            for model, result in sorted(results.items())
            if result.error_details is not None
        },
        "errors": error_summary,
        "results": {
            model: {
                "success": r.success,
                "output_file": r.output_path.name if r.output_path else None,
                "cost": r.cost,
                "processing_time": r.processing_time,
                "error": r.error,
                "error_details": r.error_details,
            }
            for model, r in results.items()
        },
    }

    metadata_path = doc_output_dir / "metadata.json"
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    logger.info(
        f"Multi-model extraction complete: {successful}/{len(models)} models, "
        f"${total_cost:.4f} total, {total_time:.1f}s"
    )

    return results
