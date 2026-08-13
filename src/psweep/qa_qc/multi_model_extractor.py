"""
Multi-Model Extractor for QA/QC Validation.

Runs document extraction with multiple AI models and saves outputs
to organized subfolders for comparison.

Model tiers and credentials are resolved exclusively through the unified
:class:`~psweep.config.model_registry.ModelRegistry`. Callers pass the registry
plus the ``qaqc.models`` tier references; this module resolves each tier to a
concrete model and its LLM kwargs (provider/api_key/endpoint) via
``registry.to_llm_kwargs`` — never threading a provider directly.

Examples
--------
.. code-block:: python

    from psweep.qa_qc.multi_model_extractor import run_multi_model_extraction

    output_files = run_multi_model_extraction(
        doc_text="Full document text...",
        doc_name="austin_energy_tariff",
        schema=loaded_schema,
        registry=registry,
        model_tiers=["primary", "secondary"],
        output_dir=Path("processed/qa_qc"),
    )

Notes
-----
Output Structure::

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
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Sequence

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
    input_tokens: int = 0
    output_tokens: int = 0
    reused: bool = False
    reused_prior_cost_usd: float = 0.0
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
    timeout_seconds: Optional[int] = None,
    runtime_artifact: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    seed_records_by_model: Optional[Dict[str, Dict[str, Any]]] = None,
    model_status_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, ModelExtractionResult]:
    """
    Run extraction with multiple models resolved through the model registry.

    Parameters
    ----------
    doc_text : str
        Full document text to extract from
    doc_name : str
        Document name (without extension, used for output folder)
    schema : dict
        JSON schema for extraction
    registry : ModelRegistry
        Unified :class:`ModelRegistry` — the single source of model
        tier resolution and credential (LLM kwargs) threading.
    model_tiers : Sequence[str]
        Ordered list of tier/model references (e.g. the
        ``qaqc.models`` list). Resolved and de-duplicated by concrete model.
    output_dir : Path
        Base directory for QA/QC outputs (e.g., "processed/")
    max_context_chars : int
        Maximum characters to process
    timeout_seconds : Optional[int]
        Per-request LLM timeout for each model extraction.
    runtime_artifact : Optional[Dict[str, Any]]
        Optional compiled runtime artifact for lineage metadata
    run_id : Optional[str]
        Optional deterministic run identifier for this invocation

    Returns
    -------
    Dict[str, ModelExtractionResult]
        Dict mapping concrete model name to ModelExtractionResult
    """
    # Import here to avoid circular imports
    from ..extraction import DocumentExtractor

    # Resolve tier references -> concrete model definitions (dedup by model).
    model_defs = registry.get_models(list(model_tiers))
    models = [definition.model for definition in model_defs]
    seed_records_by_model = seed_records_by_model or {}

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
        if model_status_callback:
            model_status_callback({"event": "model_start", "model": model})

        seeded_record = seed_records_by_model.get(model)
        if isinstance(seeded_record, dict) and isinstance(
            seeded_record.get("payload"), dict
        ):
            with open(output_path, "w") as f:
                json.dump(seeded_record, f, indent=2)

            payload = seeded_record["payload"]
            metrics = seeded_record.get("processing_metrics") or {}
            processing_time = time.time() - model_start
            prior_cost = float(metrics.get("cost_usd") or 0.0)
            results[model] = ModelExtractionResult(
                model=model,
                success=True,
                output_path=output_path,
                data=payload,
                cost=0.0,
                processing_time=processing_time,
                input_tokens=int(metrics.get("input_tokens") or 0),
                output_tokens=int(metrics.get("output_tokens") or 0),
                reused=True,
                reused_prior_cost_usd=prior_cost,
                result=None,
            )
            logger.info(f"✓ {model} reused existing extraction output")
            if model_status_callback:
                model_status_callback(
                    {
                        "event": "model_reused",
                        "model": model,
                        "processing_time": processing_time,
                        "prior_cost": prior_cost,
                    }
                )
            continue

        # Resolve credentials/provider through the single, env-driven path.
        llm_kwargs = registry.to_llm_kwargs(definition.tier)
        provider = llm_kwargs.get("provider", "openai")

        try:
            # Create a new extractor for this model, threading base_url for
            # OpenAI-compatible proxy support (LiteLLM, OpenRouter, etc.)
            extractor = DocumentExtractor(
                api_key=llm_kwargs.get("api_key"),
                model=llm_kwargs.get("model", model),
                max_context_chars=max_context_chars,
                provider=provider,
                azure_endpoint=llm_kwargs.get("azure_endpoint"),
                azure_api_version=llm_kwargs.get("azure_api_version"),
                base_url=llm_kwargs.get("base_url"),
                timeout=timeout_seconds,
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
                    "input_tokens": int(result.input_tokens or 0),
                    "output_tokens": int(result.output_tokens or 0),
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
                input_tokens=int(result.input_tokens or 0),
                output_tokens=int(result.output_tokens or 0),
                result=result,
            )

            logger.info(
                f"✓ {model} completed: {output_path.name} "
                f"(${result.cost:.4f}, {processing_time:.1f}s)"
            )
            if model_status_callback:
                model_status_callback(
                    {
                        "event": "model_success",
                        "model": model,
                        "cost": float(result.cost or 0.0),
                        "processing_time": processing_time,
                    }
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
                input_tokens=0,
                output_tokens=0,
                error=error_msg,
                error_details=error_details,
            )

            logger.error(f"✗ {model} failed: {error_msg}")
            if model_status_callback:
                model_status_callback(
                    {
                        "event": "model_error",
                        "model": model,
                        "error": error_msg,
                    }
                )

    # Calculate totals
    total_time = time.time() - total_start
    total_cost = sum(r.cost for r in results.values())
    successful = sum(1 for r in results.values() if r.success)
    reused_outputs = sum(1 for r in results.values() if r.success and r.reused)
    fresh_extractions = sum(
        1 for r in results.values() if r.success and not r.reused
    )
    reused_prior_cost = sum(r.reused_prior_cost_usd for r in results.values())
    total_input_tokens = sum(int(r.input_tokens or 0) for r in results.values())
    total_output_tokens = sum(int(r.output_tokens or 0) for r in results.values())
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
            "fresh_model_extractions": fresh_extractions,
            "reused_existing_outputs": reused_outputs,
            "reused_prior_cost_usd": reused_prior_cost,
            "total_errors": error_summary["total_errors"],
            "total_cost_usd_incurred_this_run": total_cost,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
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
                "reused": r.reused,
                "reused_prior_cost_usd": r.reused_prior_cost_usd,
                "input_tokens": int(r.input_tokens or 0),
                "output_tokens": int(r.output_tokens or 0),
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
