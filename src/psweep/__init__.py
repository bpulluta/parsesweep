"""
ParseSweep

AI-powered toolkit for extracting structured data from any document type
using state-of-the-art LLMs. Supports customizable schemas, intelligent
deduplication, and automated data compilation.

Programmatic API — engine level:
    >>> from psweep import DocumentExtractor, load_schema
    >>> extractor = DocumentExtractor(model="gpt-4o-mini")
    >>> schema = load_schema("schemas/personal/my_schema.json")
    >>> result = extractor.extract(document_text, schema)
    >>> print(result.data)

Programmatic API — pipeline level (recommended for batch use):
    >>> from psweep.pipeline import extract_documents, compile_extractions
    >>> result = extract_documents(
    ...     "documents/my_domain/",
    ...     schema="schemas/personal/my_schema.json",
    ... )
    >>> print(f"Extracted {result.successful}/{result.total} documents")
    >>> compiled = compile_extractions(result.output_dir, schema=result.schema_path)
    >>> print(f"Compiled to {compiled.output_files}")

For document discovery:
    >>> from psweep import DiscoveryEngine, DiscoveryRequest
    >>> engine = DiscoveryEngine()
    >>> request = DiscoveryRequest(
    ...     domain="my-domain",
    ...     seed_urls=["https://example.com"],
    ...     query="geothermal ordinance",
    ...     enable_serpapi=False,
    ...     output_documents=None,
    ...     output_manifest=None,
    ...     dry_run=True,
    ... )
    >>> result = engine.run(request)
"""

__version__ = "2.0.1"
__author__ = "ParseSweep Team"
__license__ = "MIT"

from pathlib import Path

# Package root directory
PACKAGE_ROOT = Path(__file__).parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]

# Core extraction API  # noqa: E402
from psweep.extraction import (  # noqa: E402
    DocumentExtractor,
    ExtractionResult,
    load_schema,
    extract_text_from_document,
)

# Document discovery API
from psweep.discovery import (  # noqa: E402
    DiscoveryEngine,
    DiscoveryRequest,
    DiscoveryResult,
)

# Pipeline API (orchestration — no CLI deps)
from psweep.pipeline import (  # noqa: E402
    extract_documents,
    compile_extractions,
    run_pipeline,
    ExtractionRunResult,
    CompilationResult,
    PipelineResult,
)

# Typed exceptions
from psweep.exceptions import (  # noqa: E402
    ParseSweepError,
    ConfigurationError,
    SchemaError,
    SchemaMetadataError,
    SchemaValidationError,
    ExtractionError,
    CompilationError,
    DiscoveryError,
    PipelineError,
    APIKeyError,
)

__all__ = [
    # Version metadata
    "__version__",
    "__author__",
    "__license__",
    # Paths
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
    # Extraction engine
    "DocumentExtractor",
    "ExtractionResult",
    "load_schema",
    "extract_text_from_document",
    # Discovery engine
    "DiscoveryEngine",
    "DiscoveryRequest",
    "DiscoveryResult",
    # Pipeline API
    "extract_documents",
    "compile_extractions",
    "run_pipeline",
    "ExtractionRunResult",
    "CompilationResult",
    "PipelineResult",
    # Exceptions
    "ParseSweepError",
    "ConfigurationError",
    "SchemaError",
    "SchemaMetadataError",
    "SchemaValidationError",
    "ExtractionError",
    "CompilationError",
    "DiscoveryError",
    "PipelineError",
    "APIKeyError",
]
