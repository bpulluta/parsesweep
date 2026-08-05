"""Pure Python pipeline API for ParseSweep.

This module provides high-level orchestration functions that call the
underlying engines (DocumentExtractor, DataCompiler, DiscoveryEngine)
without any CLI, Rich, or Typer dependencies. Use these functions to
integrate ParseSweep into your own Python code, scripts, or a future
REST API layer.

Quick start:
    >>> from psweep.pipeline import extract_documents, compile_extractions
    >>> result = extract_documents(
    ...     "documents/my_domain/",
    ...     schema="schemas/personal/my_schema.json",
    ... )
    >>> print(f"Extracted {result.successful}/{result.total} documents")
    >>> compiled = compile_extractions(result.output_dir, schema=result.schema_path)
    >>> print(f"Compiled to {compiled.output_files}")

For full config-driven runs (recommended for complex domains):
    >>> from psweep.pipeline import run_pipeline
    >>> run_pipeline("config/my_domain/my_domain.yaml")
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from psweep.extraction.llm_factory import DEFAULT_MODEL

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ExtractionRunResult:
    """Summary of a batch document extraction run."""

    total: int
    successful: int
    failed: int
    total_items: int
    total_cost: float
    output_dir: Path
    schema_path: Path
    errors: list[dict] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.successful / self.total if self.total else 0.0


@dataclass
class CompilationResult:
    """Summary of a compilation run."""

    output_dir: Path
    output_files: list[Path]
    total_rows: int
    deduplicated: bool


@dataclass
class PipelineResult:
    """Combined result from a full discover → extract → [validate] → compile run."""

    domain: str
    stages_run: list[str]
    extraction: Optional[ExtractionRunResult] = None
    compilation: Optional[CompilationResult] = None


def _read_config_dict(config_path: str | Path) -> dict:
    """Load a domain run config file as a plain dict (empty on failure)."""
    import yaml

    cfg_path = Path(config_path)
    if not cfg_path.exists():
        return {}
    with cfg_path.open() as fh:
        return yaml.safe_load(fh) or {}


def resolve_run_qaqc(config_path: str | Path) -> Optional[dict]:
    """Resolve QA/QC orchestration settings from a domain run config.

    QA/QC is enabled for ``run`` when the top-level ``qaqc.models`` list is
    present. Returns ``None`` when absent, otherwise a dict describing validate
    stage inputs.
    """
    cfg = _read_config_dict(config_path)
    qaqc_section = cfg.get("qaqc") or {}
    models = qaqc_section.get("models") or []
    if len(models) < 2:
        return None

    domain = cfg.get("domain", Path(config_path).parent.name)
    extraction = cfg.get("extraction") or {}
    output_dir = Path(extraction.get("output_dir", f"extracted/{domain}"))
    return {
        "schema": extraction.get("schema"),
        "qa_qc_dir": output_dir / "qa_qc",
        "models": models,
    }


def build_run_stage_commands(
    config_path: str | Path,
    *,
    base_cmd: Sequence[str],
    skip_discover: bool = False,
    skip_extract: bool = False,
    reprocess: bool = False,
    extra_flags: Sequence[str] = (),
) -> list[tuple[str, list[str]]]:
    """Build the subprocess commands used to execute a run config pipeline.

    ``reprocess`` propagates a "start fresh" signal to both the discover stage
    (``--reprocess``: ignore the checkpoint and refresh the search cache) and the
    extract stage (``--fresh``: re-extract already-processed documents), giving
    ``run --reprocess`` a single "start fresh" behavior across stages.

    When the config enables multi-model QA/QC, an explicit ``validate`` stage
    is appended after extraction.
    """
    cfg_path = Path(config_path)
    stage_cmds: list[tuple[str, list[str]]] = []

    qaqc = resolve_run_qaqc(cfg_path)

    if not skip_discover:
        discover_flags = [*extra_flags]
        if reprocess:
            discover_flags.append("--reprocess")
        stage_cmds.append(
            (
                "discover",
                [*base_cmd, "discover", "--config", str(cfg_path), *discover_flags],
            )
        )
    if not skip_extract:
        extract_flags = [*extra_flags]
        if reprocess:
            extract_flags.append("--fresh")
        stage_cmds.append(
            (
                "extract",
                [*base_cmd, "extract", "--config", str(cfg_path), *extract_flags],
            )
        )
    if qaqc and not skip_extract:
        stage_cmds.append(
            (
                "validate",
                [*base_cmd, "validate", "--config", str(cfg_path), *extra_flags],
            )
        )
    stage_cmds.append(
        (
            "compile",
            [*base_cmd, "compile", "--config", str(cfg_path), *extra_flags],
        )
    )
    return stage_cmds


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_api_provider() -> tuple[str | None, bool, str | None]:
    """Auto-detect which LLM provider to use from environment variables.

    Returns:
        (provider_name, is_valid, error_message)
        provider_name is 'azure', 'openai', or None.
    """
    azure_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    if azure_key and azure_endpoint:
        return "azure", True, None

    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return "openai", True, None

    error = (
        "No API credentials found in the environment.\n\n"
        "Option 1 — Azure OpenAI (recommended, higher rate limits):\n"
        "  AZURE_OPENAI_API_KEY=your-azure-key\n"
        "  AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/\n\n"
        "Option 2 — OpenAI:\n"
        "  OPENAI_API_KEY=sk-your-key-here"
    )
    return None, False, error


def extract_documents(
    documents_path: str | Path,
    schema: str | Path,
    *,
    output_dir: str | Path | None = None,
    model: str = DEFAULT_MODEL,
    provider: str = "auto",
    max_context: int = 400_000,
    skip_existing: bool = True,
    limit: int | None = None,
    pages_csv: str | Path | None = None,
) -> ExtractionRunResult:
    """Extract structured data from all documents in a folder.

    This is a pure Python function — no CLI, no Rich, no sys.exit. It calls
    :class:`psweep.extraction.DocumentExtractor` for each supported document,
    persists per-document JSON, and returns an aggregated result.

    Parameters
    ----------
    documents_path:
        Folder containing PDF/DOCX/TXT/XLSX/CSV documents.
    schema:
        Path to a valid ParseSweep v2.0+ JSON schema with ``$metadata``.
    output_dir:
        Where to write extracted JSON files. Defaults to
        ``extracted/<documents_path.name>/``.
    model:
        LLM model identifier (e.g. ``gpt-4o-mini``, ``claude-3.5-sonnet``).
    provider:
        LLM provider. ``"auto"`` detects from environment variables.
    max_context:
        Maximum characters to send to the LLM per document.
    skip_existing:
        If True, skip documents whose output JSON already exists.
    limit:
        Cap the number of documents processed (useful for testing).
    pages_csv:
        Optional CSV mapping filenames to page ranges.

    Returns
    -------
    ExtractionRunResult
        Aggregated counts, cost, and output location.

    Raises
    ------
    psweep.exceptions.APIKeyError
        If no LLM credentials are found.
    psweep.exceptions.SchemaError
        If the schema is invalid or missing ``$metadata``.
    psweep.exceptions.ExtractionError
        If a document fails to extract (per-document errors are collected and
        returned in ``result.errors`` rather than raised, unless *all* fail).
    """
    from psweep.exceptions import APIKeyError, SchemaError
    from psweep.extraction import DocumentExtractor, load_schema
    from psweep.extraction.document_utils import (
        SUPPORTED_EXTENSIONS,
        is_supported_document,
    )
    from psweep.extraction.record_writer import write_extraction_record
    from psweep.utils.page_range import load_pages_csv

    docs_path = Path(documents_path)
    schema_path = Path(schema)

    if not docs_path.exists():
        raise FileNotFoundError(f"Documents path not found: {docs_path}")
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")

    # Validate credentials
    prov, ok, err = detect_api_provider()
    if not ok:
        raise APIKeyError()

    # Load and validate schema
    try:
        loaded_schema = load_schema(schema_path)
    except Exception as exc:
        raise SchemaError(f"Failed to load schema {schema_path}: {exc}") from exc

    if "$metadata" not in loaded_schema:
        raise SchemaError(
            f"Schema {schema_path.name} is missing the required $metadata section.",
            hint="Add $metadata.extraction.main_data_array and identifier_fields.",
        )

    # Resolve output directory
    category = docs_path.name
    out_dir = Path(output_dir) if output_dir else Path("extracted") / category
    out_dir.mkdir(parents=True, exist_ok=True)

    # Collect documents
    docs = sorted(
        f for f in docs_path.iterdir() if is_supported_document(f)
    )
    if limit:
        docs = docs[:limit]

    # Load page ranges if provided
    page_range_map: dict[Path, tuple[int, int] | None] = {}
    if pages_csv:
        page_mappings = load_pages_csv(Path(pages_csv))
        for doc in docs:
            rel_keys = (doc.as_posix(), doc.name)
            for key in rel_keys:
                if key in page_mappings:
                    page_range_map[doc] = page_mappings[key]
                    break

    # Resolve actual provider / model
    actual_provider = prov if provider == "auto" else provider

    extractor = DocumentExtractor(
        model=model,
        provider=actual_provider,
        max_context_chars=max_context,
    )

    from psweep.extraction.document_utils import extract_text_from_document

    results = []
    for doc in docs:
        output_file = out_dir / f"{doc.stem}.json"
        if skip_existing and output_file.exists():
            logger.debug("Skipping %s (already extracted)", doc.name)
            results.append({"file": doc.name, "success": True, "skipped": True})
            continue

        try:
            page_range = page_range_map.get(doc)
            text = extract_text_from_document(doc, page_range=page_range)
            result = extractor.extract(text, loaded_schema)

            num_items = write_extraction_record(
                output_file,
                doc_path=doc,
                result=result,
                model=model,
                provider=actual_provider,
                schema_id=schema_path.as_posix(),
                identifier_fields=loaded_schema.get("$metadata", {})
                .get("extraction", {})
                .get("identifier_fields"),
            )
            results.append({
                "file": doc.name,
                "success": True,
                "items": num_items,
                "cost": result.cost,
            })
        except Exception as exc:
            logger.warning("Failed to extract %s: %s", doc.name, exc)
            results.append({"file": doc.name, "success": False, "error": str(exc)})

    successful = [r for r in results if r.get("success") and not r.get("skipped")]
    failed = [r for r in results if not r.get("success")]

    return ExtractionRunResult(
        total=len(docs),
        successful=len(successful) + len([r for r in results if r.get("skipped")]),
        failed=len(failed),
        total_items=sum(r.get("items", 0) for r in successful),
        total_cost=sum(r.get("cost", 0.0) for r in results),
        output_dir=out_dir,
        schema_path=schema_path,
        errors=[{"file": r["file"], "error": r["error"]} for r in failed],
    )


def compile_extractions(
    extraction_dir: str | Path,
    schema: str | Path,
    *,
    config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    report_format: str = "both",
    dry_run: bool = False,
) -> CompilationResult:
    """Compile extracted JSON files into Excel/CSV output.

    Parameters
    ----------
    extraction_dir:
        Directory containing ``.json`` extraction files.
    schema:
        Path to the same schema used during extraction.
    config_path:
        Optional path to the domain run config (e.g.
        ``config/<domain>/<domain>.yaml``). When provided, the
        ``compilation.output`` settings (exclude_fields, column_renames,
        column_order, etc.) and ``compilation.normalization`` from the config
        are applied to the output. Matches the behavior of
        ``pixi run psweep compile --config``.
    output_dir:
        Where to write compiled output. Defaults to ``compiled/<name>/``.
    report_format:
        ``"xlsx"``, ``"csv"``, or ``"both"`` (default).
    dry_run:
        If True, parse and validate but write no output files.

    Returns
    -------
    CompilationResult
    """
    from psweep.compilation.data_compiler import DataCompiler
    from psweep.utils.schema_metadata import SchemaMetadata

    ext_dir = Path(extraction_dir)
    schema_path = Path(schema)

    if not ext_dir.exists():
        raise FileNotFoundError(f"Extraction directory not found: {ext_dir}")
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")

    out_dir = Path(output_dir) if output_dir else Path("compiled") / ext_dir.name
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    # Build metadata overrides from config when provided — this is how output
    # settings (exclude_fields, column_renames, column_order, normalization)
    # reach the compiler when calling the Python API directly.
    metadata_overrides: dict | None = None
    if config_path:
        from psweep.config import load_runtime_config_file, resolve_command_config

        cfg = load_runtime_config_file(Path(config_path))
        resolved = resolve_command_config(
            command="compile", cli_values={}, config_data=cfg, strict=False
        )
        compilation_overrides: dict = {}
        config_output = resolved.get("compilation_output")
        if isinstance(config_output, dict):
            compilation_overrides["output"] = config_output
        config_norm = resolved.get("normalization")
        if isinstance(config_norm, dict):
            compilation_overrides["normalization"] = config_norm
        if compilation_overrides:
            metadata_overrides = {"compilation": compilation_overrides}

    schema_metadata = SchemaMetadata(schema_path, metadata_overrides=metadata_overrides)
    compiler = DataCompiler(
        schema_metadata=schema_metadata,
        verbose=False,
        debug=False,
    )
    df, _schema_info = compiler.compile_from_directory(
        ext_dir,
        apply_deduplication=not dry_run,
    )

    rows = len(df)
    output_files: list[Path] = []
    if not dry_run and not df.empty:
        base_name = ext_dir.name.replace("_", "-")
        normalized_format = report_format.strip().lower()
        if normalized_format in {"both", "all"}:
            output_formats = ("csv", "xlsx")
        elif normalized_format in {"csv"}:
            output_formats = ("csv",)
        elif normalized_format in {"xlsx", "excel"}:
            output_formats = ("xlsx",)
        else:
            raise ValueError(
                "report_format must be one of: csv, xlsx, both"
            )

        for output_format in output_formats:
            if output_format == "csv":
                csv_path = out_dir / f"{base_name}.csv"
                compiler.save_csv(df, csv_path)
                output_files.append(csv_path)
            else:
                xlsx_path = out_dir / f"{base_name}.xlsx"
                compiler.save_excel(df, xlsx_path)
                output_files.append(xlsx_path)

    return CompilationResult(
        output_dir=out_dir,
        output_files=output_files,
        total_rows=max(rows, 0),
        deduplicated=not dry_run,
    )


def run_pipeline(
    config_path: str | Path,
    *,
    skip_discover: bool = False,
    skip_extract: bool = False,
    reprocess: bool = False,
) -> PipelineResult:
    """Run the full pipeline (discover → extract → compile) from a run config.

    This is a thin subprocess-based orchestrator that mirrors the CLI ``run``
    command. It is suitable for scripting and automation where you want
    programmatic control over the pipeline without the Rich terminal UI.
    When the config enables QA/QC, a ``compare`` stage runs after ``compile``.

    Parameters
    ----------
    config_path:
        Path to a domain run config file (e.g. ``config/<domain>/<domain>.yaml``).
    skip_discover:
        If True, skip the discovery stage.
    skip_extract:
        If True, skip the extraction stage (compile from existing JSON).
    reprocess:
        If True, re-extract already-processed documents.

    Returns
    -------
    PipelineResult

    Raises
    ------
    psweep.exceptions.PipelineError
        If any stage fails.
    """
    import yaml
    from psweep.exceptions import PipelineError

    cfg_path = Path(config_path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config not found: {cfg_path}")

    with cfg_path.open() as fh:
        cfg = yaml.safe_load(fh)

    domain = cfg.get("domain", cfg_path.parent.name)
    stage_cmds = build_run_stage_commands(
        cfg_path,
        base_cmd=[sys.executable, "-m", "psweep.cli.main"],
        skip_discover=skip_discover,
        skip_extract=skip_extract,
        reprocess=reprocess,
        extra_flags=("-q",),
    )

    for stage, cmd in stage_cmds:
        logger.info("Running stage: %s", stage)
        result = subprocess.run(cmd, capture_output=False)
        if result.returncode != 0:
            raise PipelineError(
                f"Stage '{stage}' failed (exit {result.returncode})",
                stage=stage,
            )

    return PipelineResult(domain=domain, stages_run=[stage for stage, _ in stage_cmds])
