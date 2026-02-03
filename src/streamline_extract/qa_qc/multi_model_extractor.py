"""
Multi-Model Extractor for QA/QC Validation.

Runs document extraction with multiple AI models and saves outputs
to organized subfolders for comparison.

This module is part of Phase 2 of the QA/QC Multi-Model Implementation Plan.

Usage:
    from streamline_extract.qa_qc.multi_model_extractor import run_multi_model_extraction
    
    output_files = run_multi_model_extraction(
        doc_text="Full document text...",
        doc_name="austin_energy_tariff",
        schema=loaded_schema,
        models=["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"],
        output_dir=Path("processed/qa_qc"),
        api_key="sk-...",
        provider="openai",
    )

Output Structure:
    processed/qa_qc/{doc_name}/
        gpt-4o.json
        gpt-4-turbo.json
        gpt-3.5-turbo.json
        metadata.json

Status: Phase 2 - Implemented
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .utils import sanitize_model_name

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


def run_multi_model_extraction(
    doc_text: str,
    doc_name: str,
    schema: dict,
    models: List[str],
    output_dir: Path,
    api_key: str,
    provider: str = "openai",
    azure_endpoint: Optional[str] = None,
    azure_api_version: Optional[str] = None,
    max_context_chars: int = 400000,
) -> Dict[str, ModelExtractionResult]:
    """
    Run extraction with multiple models.

    Args:
        doc_text: Full document text to extract from
        doc_name: Document name (without extension, used for output folder)
        schema: JSON schema for extraction
        models: List of model names (e.g., ["gpt-4o", "gpt-4-turbo"])
        output_dir: Base directory for QA/QC outputs (e.g., "processed/")
        api_key: API key for the provider
        provider: LLM provider ("openai", "azure", "anthropic", etc.)
        azure_endpoint: Azure OpenAI endpoint (if using Azure)
        azure_api_version: Azure API version (if using Azure)
        max_context_chars: Maximum characters to process

    Returns:
        Dict mapping model name to ModelExtractionResult
    """
    # Import here to avoid circular imports
    from streamline_extract.extraction import DocumentExtractor
    
    # Create output directory for this document
    doc_output_dir = Path(output_dir) / "qa_qc" / doc_name
    doc_output_dir.mkdir(parents=True, exist_ok=True)
    
    results: Dict[str, ModelExtractionResult] = {}
    total_start = time.time()
    
    logger.info(f"Starting multi-model extraction with {len(models)} models: {models}")
    
    for model in models:
        model_start = time.time()
        safe_model_name = sanitize_model_name(model)
        output_path = doc_output_dir / f"{safe_model_name}.json"
        
        logger.info(f"Running extraction with {model}...")
        
        try:
            # Create a new extractor for this model
            extractor = DocumentExtractor(
                api_key=api_key,
                model=model,
                max_context_chars=max_context_chars,
                provider=provider,
                azure_endpoint=azure_endpoint,
                azure_api_version=azure_api_version,
            )
            
            # Run extraction
            result = extractor.extract(
                text=doc_text,
                schema=schema,
            )
            
            # Add model metadata to extracted data
            output_data = {
                "_qaqc_metadata": {
                    "model": model,
                    "provider": provider,
                    "timestamp": datetime.now().isoformat(),
                    "cost": result.cost,
                    "processing_time": result.processing_time,
                    "completeness_score": result.completeness_score,
                    "validation_notes": result.validation_notes,
                },
                **result.data,
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
            )
            
            logger.info(
                f"✓ {model} completed: {output_path.name} "
                f"(${result.cost:.4f}, {processing_time:.1f}s)"
            )
            
        except Exception as e:
            processing_time = time.time() - model_start
            error_msg = str(e)
            
            results[model] = ModelExtractionResult(
                model=model,
                success=False,
                output_path=None,
                data=None,
                cost=0.0,
                processing_time=processing_time,
                error=error_msg,
            )
            
            logger.error(f"✗ {model} failed: {error_msg}")
    
    # Calculate totals
    total_time = time.time() - total_start
    total_cost = sum(r.cost for r in results.values())
    successful = sum(1 for r in results.values() if r.success)
    
    # Save metadata
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "document": doc_name,
        "models": models,
        "version": "2.0.0",
        "status": "completed" if successful == len(models) else "partial",
        "summary": {
            "total_models": len(models),
            "successful": successful,
            "failed": len(models) - successful,
            "total_cost": total_cost,
            "total_time": total_time,
        },
        "results": {
            model: {
                "success": r.success,
                "output_file": r.output_path.name if r.output_path else None,
                "cost": r.cost,
                "processing_time": r.processing_time,
                "error": r.error,
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


def save_metadata(
    output_dir: Path,
    models: List[str],
    document_name: str,
    extra_info: Optional[dict] = None,
) -> Path:
    """
    Save QA/QC run metadata alongside extractions.

    Args:
        output_dir: Directory to save metadata
        models: List of models used
        document_name: Name of the document processed
        extra_info: Optional additional metadata

    Returns:
        Path to the saved metadata file
        
    Status: Phase 2 - Not Yet Implemented
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "document": document_name,
        "models": models,
        "version": "2.0.0",
        "status": "pending",  # Will be updated after extraction
    }
    
    if extra_info:
        metadata.update(extra_info)
    
    metadata_path = output_dir / "metadata.json"
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)
    
    logger.info(f"Saved QA/QC metadata to {metadata_path}")
    return metadata_path
