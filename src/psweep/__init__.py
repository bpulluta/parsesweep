"""
ParseSweep

AI-powered toolkit for extracting structured data from any document type
using state-of-the-art LLMs. Supports customizable schemas, intelligent
deduplication, and automated data compilation.

Quick start (programmatic):
    >>> from psweep import DocumentExtractor, load_schema
    >>> extractor = DocumentExtractor(api_key="sk-...")
    >>> schema = load_schema("schemas/my_schema.json")
    >>> result = extractor.extract(document_text, schema)
    >>> print(result.data)

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

__all__ = [
    # Version metadata
    "__version__",
    "__author__",
    "__license__",
    # Paths
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
    # Extraction
    "DocumentExtractor",
    "ExtractionResult",
    "load_schema",
    "extract_text_from_document",
    # Discovery
    "DiscoveryEngine",
    "DiscoveryRequest",
    "DiscoveryResult",
]
