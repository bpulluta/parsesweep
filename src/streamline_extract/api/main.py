"""
StreamlineExtract API - FastAPI Application

Production-ready REST API for universal document extraction.
Supports any document type with JSON schema-driven extraction.
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union
from enum import Enum
import os
import tempfile
import json
from pathlib import Path
from datetime import datetime
import logging

from streamline_extract.extraction import DocumentExtractor, load_schema
from streamline_extract.extraction.document_utils import extract_text_from_document, is_supported_document
from streamline_extract.consolidation.consolidator import Consolidator
from streamline_extract.utils.config import get_config

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="StreamlineExtract API",
    description="Universal document extraction system - extract structured data from any document type",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS middleware - configure for production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Configure for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
config = get_config()

# ============================================================================
# Pydantic Models (Request/Response Schemas)
# ============================================================================

class DocumentFormat(str, Enum):
    """Supported document formats."""
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    XLSX = "xlsx"
    CSV = "csv"


class OutputFormat(str, Enum):
    """Supported output formats."""
    JSON = "json"
    EXCEL = "excel"
    CSV = "csv"


class SchemaPreset(str, Enum):
    """Available schema presets."""
    GEOTHERMAL_ORDINANCE = "geothermal_ordinance"
    ELECTRICITY_TARIFF = "electricity_tariff"
    CUSTOM = "custom"


class ExtractionRequest(BaseModel):
    """Request model for document extraction."""
    schema_preset: Optional[SchemaPreset] = Field(None, description="Use a preset schema")
    custom_schema: Optional[Dict[str, Any]] = Field(None, description="Provide custom JSON schema")
    enable_qa_qc: bool = Field(False, description="Enable QA/QC validation (slower)")
    max_context_chars: int = Field(400000, description="Maximum characters to extract")
    model: str = Field("gpt-4o-mini", description="AI model to use")
    
    class Config:
        json_schema_extra = {
            "example": {
                "schema_preset": "geothermal_ordinance",
                "enable_qa_qc": False,
                "max_context_chars": 400000,
                "model": "gpt-4o-mini"
            }
        }


class ExtractionResponse(BaseModel):
    """Response model for extraction results."""
    job_id: str = Field(..., description="Unique job identifier")
    status: str = Field(..., description="Job status: completed, processing, failed")
    data: Optional[Dict[str, Any]] = Field(None, description="Extracted structured data")
    metadata: Dict[str, Any] = Field(..., description="Extraction metadata")
    completeness_score: Optional[float] = Field(None, description="Completeness score 0-1")
    cost_usd: Optional[float] = Field(None, description="Processing cost in USD")
    processing_time: Optional[float] = Field(None, description="Processing time in seconds")
    error: Optional[str] = Field(None, description="Error message if failed")


class BatchExtractionRequest(BaseModel):
    """Request model for batch extraction."""
    schema_preset: Optional[SchemaPreset] = Field(None, description="Use a preset schema")
    custom_schema: Optional[Dict[str, Any]] = Field(None, description="Provide custom JSON schema")
    enable_qa_qc: bool = Field(False, description="Enable QA/QC validation")
    callback_url: Optional[str] = Field(None, description="Webhook URL for completion notification")


class BatchExtractionResponse(BaseModel):
    """Response model for batch extraction."""
    batch_id: str = Field(..., description="Unique batch identifier")
    status: str = Field(..., description="Batch status")
    total_documents: int = Field(..., description="Total documents in batch")
    completed: int = Field(0, description="Number of completed extractions")
    estimated_completion: Optional[str] = Field(None, description="Estimated completion time")


class ConsolidationRequest(BaseModel):
    """Request model for data consolidation."""
    output_format: OutputFormat = Field(OutputFormat.EXCEL, description="Output format")
    deduplication: bool = Field(True, description="Enable deduplication")


class ConsolidationResponse(BaseModel):
    """Response model for consolidation."""
    status: str = Field(..., description="Consolidation status")
    download_url: Optional[str] = Field(None, description="URL to download result")
    stats: Dict[str, Any] = Field(..., description="Consolidation statistics")


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = Field(..., description="Service status")
    version: str = Field(..., description="API version")
    timestamp: str = Field(..., description="Current timestamp")
    dependencies: Dict[str, str] = Field(..., description="Dependency status")


# ============================================================================
# Helper Functions
# ============================================================================

def get_extractor(model: str = "gpt-4o-mini", max_context: int = 400000) -> DocumentExtractor:
    """
    Initialize document extractor with API credentials.
    
    Auto-detects Azure or OpenAI based on environment variables.
    """
    # Check for Azure credentials first
    azure_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    
    if azure_key and azure_endpoint:
        logger.info("Using Azure OpenAI")
        azure_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")
        azure_model = os.getenv("AZURE_OPENAI_MODEL", model)
        
        return DocumentExtractor(
            api_key=azure_key,
            model=azure_model,
            max_context_chars=max_context,
            use_azure=True,
            azure_endpoint=azure_endpoint,
            azure_api_version=azure_version
        )
    
    # Fall back to OpenAI
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        raise HTTPException(
            status_code=500,
            detail="No API credentials found. Set OPENAI_API_KEY or AZURE_OPENAI_* in environment."
        )
    
    logger.info("Using OpenAI")
    return DocumentExtractor(
        api_key=openai_key,
        model=model,
        max_context_chars=max_context
    )


def get_schema(schema_preset: Optional[SchemaPreset], custom_schema: Optional[Dict]) -> Dict[str, Any]:
    """Load schema from preset or custom definition."""
    if custom_schema:
        return custom_schema
    
    if schema_preset:
        schema_files = {
            SchemaPreset.GEOTHERMAL_ORDINANCE: "geothermal_ordinance_schema_streamlined.json",
            SchemaPreset.ELECTRICITY_TARIFF: "electricity_tariff_schema.json"
        }
        
        if schema_preset in schema_files:
            schema_path = config.schema_dir / schema_files[schema_preset]
            return load_schema(schema_path)
    
    # Default: use first available schema
    schemas = list(config.schema_dir.glob("*.json"))
    if schemas:
        return load_schema(schemas[0])
    
    raise HTTPException(
        status_code=400,
        detail="No schema provided and no default schema available"
    )


# ============================================================================
# API Endpoints
# ============================================================================

@app.get("/", response_model=Dict[str, str])
async def root():
    """Root endpoint - API information."""
    return {
        "name": "StreamlineExtract API",
        "version": "1.0.0",
        "description": "Universal document extraction system",
        "docs": "/docs",
        "health": "/health"
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint.
    
    Returns service status and dependency health.
    """
    # Check API credentials
    has_azure = bool(os.getenv("AZURE_OPENAI_API_KEY"))
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    
    dependencies = {
        "azure_openai": "configured" if has_azure else "not_configured",
        "openai": "configured" if has_openai else "not_configured",
        "schemas": f"{len(list(config.schema_dir.glob('*.json')))} available"
    }
    
    return HealthResponse(
        status="healthy" if (has_azure or has_openai) else "degraded",
        version="1.0.0",
        timestamp=datetime.utcnow().isoformat(),
        dependencies=dependencies
    )


@app.post("/api/v1/extract", response_model=ExtractionResponse)
async def extract_document(
    file: UploadFile = File(..., description="Document to extract"),
    request: ExtractionRequest = Depends()
):
    """
    Extract structured data from a single document.
    
    **Supported formats:** PDF, DOCX, TXT, XLSX, CSV
    
    **Process:**
    1. Upload document
    2. Specify schema (preset or custom)
    3. Receive extracted structured data
    
    **Example:**
    ```bash
    curl -X POST "http://localhost:8000/api/v1/extract" \\
      -F "file=@document.pdf" \\
      -F "schema_preset=geothermal_ordinance"
    ```
    """
    job_id = f"job_{datetime.utcnow().timestamp()}"
    
    try:
        # Validate file format
        file_ext = Path(file.filename).suffix.lower()
        if not any(file_ext == f".{fmt.value}" for fmt in DocumentFormat):
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file format: {file_ext}. Supported: PDF, DOCX, TXT, XLSX, CSV"
            )
        
        # Save uploaded file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_file:
            content = await file.read()
            tmp_file.write(content)
            tmp_path = Path(tmp_file.name)
        
        try:
            # Extract text from document
            text = extract_text_from_document(tmp_path)
            
            # Get schema
            schema = get_schema(request.schema_preset, request.custom_schema)
            
            # Initialize extractor
            extractor = get_extractor(
                model=request.model,
                max_context=request.max_context_chars
            )
            
            # Perform extraction
            result = extractor.extract(
                text=text,
                schema=schema,
                enable_qa_qc=request.enable_qa_qc
            )
            
            return ExtractionResponse(
                job_id=job_id,
                status="completed",
                data=result.data,
                metadata={
                    "filename": file.filename,
                    "model": request.model,
                    "qa_qc_enabled": request.enable_qa_qc,
                    "extraction_timestamp": datetime.utcnow().isoformat()
                },
                completeness_score=result.completeness_score,
                cost_usd=result.cost,
                processing_time=result.processing_time
            )
            
        finally:
            # Clean up temporary file
            tmp_path.unlink(missing_ok=True)
    
    except Exception as e:
        logger.error(f"Extraction failed: {str(e)}")
        return ExtractionResponse(
            job_id=job_id,
            status="failed",
            metadata={"filename": file.filename},
            error=str(e)
        )


@app.post("/api/v1/extract/batch", response_model=BatchExtractionResponse)
async def extract_batch(
    files: List[UploadFile] = File(..., description="Documents to extract"),
    request: BatchExtractionRequest = Depends(),
    background_tasks: BackgroundTasks = None
):
    """
    Extract structured data from multiple documents.
    
    **Process:**
    1. Upload multiple documents
    2. Processing happens asynchronously
    3. Receive batch ID for tracking
    4. Optional webhook notification on completion
    
    **Example:**
    ```bash
    curl -X POST "http://localhost:8000/api/v1/extract/batch" \\
      -F "files=@doc1.pdf" \\
      -F "files=@doc2.pdf" \\
      -F "schema_preset=electricity_tariff"
    ```
    """
    batch_id = f"batch_{datetime.utcnow().timestamp()}"
    
    # TODO: Implement with Celery for true async processing
    # For now, return batch info
    return BatchExtractionResponse(
        batch_id=batch_id,
        status="queued",
        total_documents=len(files),
        completed=0,
        estimated_completion=None
    )


@app.get("/api/v1/schemas")
async def list_schemas():
    """
    List available schema presets.
    
    Returns all available schema names and their descriptions.
    """
    schemas = []
    for schema_file in config.schema_dir.glob("*.json"):
        schema_data = load_schema(schema_file)
        schemas.append({
            "name": schema_file.stem,
            "description": schema_data.get("description", "No description"),
            "title": schema_data.get("title", schema_file.stem)
        })
    
    return {"schemas": schemas}


@app.get("/api/v1/schemas/{schema_name}", response_model=Dict[str, Any])
async def get_schema_by_name(schema_name: str):
    """
    Get a specific schema by name.
    
    Returns the full JSON schema definition.
    """
    schema_path = config.schema_dir / f"{schema_name}.json"
    
    if not schema_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Schema not found: {schema_name}"
        )
    
    return load_schema(schema_path)


@app.get("/api/v1/metrics")
async def get_metrics():
    """
    Get API metrics (for Prometheus monitoring).
    
    Returns basic metrics in Prometheus format.
    """
    # TODO: Implement proper Prometheus metrics
    return {
        "requests_total": 0,
        "requests_success": 0,
        "requests_failed": 0,
        "avg_processing_time": 0
    }


# ============================================================================
# Error Handlers
# ============================================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """Custom HTTP exception handler."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code,
            "timestamp": datetime.utcnow().isoformat()
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """General exception handler."""
    logger.error(f"Unhandled exception: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": str(exc) if os.getenv("DEBUG") else "An error occurred",
            "timestamp": datetime.utcnow().isoformat()
        }
    )


# ============================================================================
# Startup/Shutdown Events
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
    logger.info("StreamlineExtract API starting up...")
    logger.info(f"Available schemas: {len(list(config.schema_dir.glob('*.json')))}")
    
    # Verify API credentials
    has_credentials = bool(os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY"))
    if not has_credentials:
        logger.warning("⚠️  No API credentials found! Set OPENAI_API_KEY or AZURE_OPENAI_* environment variables.")
    else:
        logger.info("✓ API credentials configured")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    logger.info("StreamlineExtract API shutting down...")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
