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

import csv
import hashlib
import json
import logging
import os
import re
import io
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from psweep.config import load_runtime_config_file, resolve_command_config
from psweep.config.runtime_config_loader import resolve_compilation_metadata
from psweep.compilation.input_provenance import (
    analyze_compile_input_provenance,
    INTERNAL_RECORD_DIRS,
)
from psweep.discovery.request_inputs import (
    normalize_discover_inputs,
    validate_discover_plan,
)
from jsonschema import ValidationError

from psweep.exceptions import APIKeyError
from psweep import extraction as extraction_api
from psweep.extraction import document_utils
from psweep.extraction.page_locator import (
    PageLocator,
    DEFAULT_PAGE_TRIGGER_CHARS,
    DEFAULT_MAX_SELECTED_PAGES,
)
from psweep.extraction.section_locator import SectionLocator
from psweep.extraction.pdf_utils import validate_pdf_scope
from psweep.extraction.llm_factory import (
    DEFAULT_MAX_CONTEXT,
    resolve_llm_kwargs,
)
from psweep.extraction.provenance import (
    build_source_context_map,
    prepend_source_context,
    build_run_manifest,
    generate_run_id,
)
from psweep.extraction.record_writer import (
    record_extraction_scope,
    write_extraction_record,
)
from psweep.extraction.schema_utils import (
    build_schema_validator,
    record_validator,
)
from psweep.utils.error_taxonomy import build_error_record
from psweep.utils.file_io import artifact_sha256, atomic_json, atomic_write
from psweep.utils.page_range import load_pages_csv, parse_page_range
from psweep.utils.schema_metadata import SchemaMetadata

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path-layout helpers
#
# Single source of truth for the documents -> extracted -> compiled directory
# layout and the discovered/<domain>/... convention shared by the CLI commands.
# ---------------------------------------------------------------------------


def discover_domain(path: Path) -> Optional[str]:
    """Return ``<domain>`` for a ``discovered/<domain>/...`` path, else ``None``.

    The path is resolved first so the convention holds regardless of how the
    input was spelled (relative, symlinked, or absolute).
    """
    resolved_parts = list(Path(path).resolve().parts)
    if "discovered" in resolved_parts:
        i = resolved_parts.index("discovered")
        if i + 1 < len(resolved_parts):
            return resolved_parts[i + 1]
    return None


def swap_layout_component(path: Path, source: str, target: str) -> Path:
    """Swap the first ``source`` path component for ``target``.

    Falls back to ``Path.cwd() / target / path.name`` when ``path`` has no
    ``source`` component. This is the shared documents->extracted->compiled
    directory swap.
    """
    path = Path(path)
    parts = list(path.parts)
    if source in parts:
        parts[parts.index(source)] = target
        return Path(*parts)
    return Path.cwd() / target / path.name


def resolve_extract_output_dir(input_path: Path, *, is_dir: bool) -> Path:
    """Default ``extracted/...`` output dir for an extract input path.

    ``discovered/<domain>/...`` inputs map to ``extracted/<domain>``; otherwise a
    ``documents`` component is swapped to ``extracted``; otherwise
    ``extracted/<name>``.
    """
    input_path = Path(input_path)
    probe = input_path if is_dir else input_path.parent
    domain = discover_domain(probe)
    if domain:
        return Path.cwd() / "extracted" / domain
    return swap_layout_component(probe, "documents", "extracted")


def resolve_compile_output_dir(input_dir: Path) -> Path:
    """Default ``compiled/...`` output dir for a compile input directory."""
    return swap_layout_component(Path(input_dir), "extracted", "compiled")


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
    unknown_cost_calls: int = 0
    status: str = "success"
    manifest_path: Path | None = None
    records: list[dict] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """Return the subprocess outcome without importing CLI code."""
        if self.status in {"failed", "partial_failure"}:
            return 1
        return 2 if self.status == "empty" else 0

    @property
    def success_rate(self) -> float:
        """Fraction of documents extracted without error.

        Returns
        -------
        float
            ``successful / total``, or ``0.0`` when ``total`` is zero.
        """
        return self.successful / self.total if self.total else 0.0


@dataclass
class CompilationResult:
    """Summary of a compilation run."""

    output_dir: Path
    output_files: list[Path]
    total_rows: int
    deduplicated: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    """Combined result from a full discover → extract → [validate] → compile run."""

    domain: str
    stages_run: list[str]
    extraction: Optional[ExtractionRunResult] = None
    compilation: Optional[CompilationResult] = None


def _read_config_dict(config_path: str | Path) -> dict:
    """Load run config without bypassing validation failures."""
    return load_runtime_config_file(Path(config_path))


def resolve_run_validation(config_path: str | Path) -> Optional[dict]:
    """Resolve QA/QC orchestration settings from a domain run config.

    QA/QC is enabled for ``run`` when the top-level ``validation.models`` list is
    present. Returns ``None`` when absent, otherwise a dict describing validate
    stage inputs.
    """
    cfg = _read_config_dict(config_path)
    validation_section = cfg.get("validation") or {}
    models = validation_section.get("models") or []
    if len(models) < 2:
        return None

    domain = cfg.get("domain", Path(config_path).parent.name)
    extraction = cfg.get("extraction") or {}
    output_dir = Path(extraction.get("output_dir", f"extracted/{domain}"))
    return {
        "schema": extraction.get("schema"),
        "validation_dir": output_dir / "validation",
        "models": models,
    }


def resolve_run_discovery_enabled(config_path: str | Path) -> bool:
    """Return True when discovery is configured for the run config."""
    cfg = _read_config_dict(config_path)
    discovery_section = cfg.get("discovery")
    return isinstance(discovery_section, dict) and bool(discovery_section)


def build_run_stage_commands(
    config_path: str | Path,
    *,
    base_cmd: Sequence[str],
    skip_discover: bool = False,
    skip_extract: bool = False,
    discovery_enabled: bool | None = None,
    fresh: bool = False,
    target_limit: int | None = None,
    retention_documents: str | None = None,
    extract_input_path: str | Path | None = None,
    extra_flags: Sequence[str] = (),
) -> list[tuple[str, list[str]]]:
    """Build the subprocess commands used to execute a run config pipeline.

    ``fresh`` propagates a "start fresh" signal to discover (ignore the
    checkpoint and refresh the search cache), extract (re-extract already-
    processed documents), and compile (clear compiled outputs before writing),
    giving ``run --fresh`` a fully clean pipeline across all stages.

    When the config enables multi-model QA/QC, an explicit ``validate`` stage
    is appended after extraction.
    """
    cfg_path = Path(config_path)
    stage_cmds: list[tuple[str, list[str]]] = []

    if discovery_enabled is None:
        discovery_enabled = resolve_run_discovery_enabled(cfg_path)

    validation = resolve_run_validation(cfg_path)

    if not skip_discover and discovery_enabled:
        discover_flags = [*extra_flags]
        if target_limit is not None:
            discover_flags.extend(["--target-limit", str(target_limit)])
        if retention_documents is not None:
            discover_flags.extend(
                ["--retention-documents", str(retention_documents)]
            )
        if fresh:
            discover_flags.append("--fresh")
        stage_cmds.append(
            (
                "discover",
                [
                    *base_cmd,
                    "discover",
                    "--config",
                    str(cfg_path),
                    *discover_flags,
                ],
            )
        )
    if not skip_extract:
        extract_flags = [*extra_flags]
        if fresh:
            extract_flags.append("--fresh")
        extract_cmd = [*base_cmd, "extract"]
        if extract_input_path is not None:
            extract_cmd.append(str(extract_input_path))
        extract_cmd.extend(["--config", str(cfg_path), *extract_flags])
        stage_cmds.append(
            (
                "extract",
                extract_cmd,
            )
        )
    if validation and not skip_extract:
        validate_flags = [*extra_flags]
        if fresh:
            validate_flags.append("--fresh")
        stage_cmds.append(
            (
                "validate",
                [
                    *base_cmd,
                    "validate",
                    "--config",
                    str(cfg_path),
                    *validate_flags,
                ],
            )
        )
    compile_flags = [*extra_flags]
    if fresh:
        compile_flags.append("--fresh")
    stage_cmds.append(
        (
            "compile",
            [*base_cmd, "compile", "--config", str(cfg_path), *compile_flags],
        )
    )
    return stage_cmds


# ---------------------------------------------------------------------------
# Run-command on-disk helpers
#
# Pure, UI-free logic shared by the CLI ``run`` driver: artifact counting,
# checkpoint inspection, and the extraction-output layout. Keeping these here
# (rather than inline in app.py) makes ``run`` a thin driver and the logic
# unit-testable without spawning the CLI.
# ---------------------------------------------------------------------------


def count_curated_documents(curated_dir: Path) -> int:
    """Count curated docs, skipping ``.text`` sidecars and ``.json``."""
    if not curated_dir.exists():
        return 0
    return sum(
        1
        for f in curated_dir.rglob("*")
        if f.is_file() and ".text" not in str(f) and f.suffix != ".json"
    )


def count_extracted_documents(extraction_dir: Path) -> int:
    """Count extracted JSONs, excluding ``run_manifests/*.json``."""
    if not extraction_dir.exists():
        return 0
    manifests_dir = extraction_dir / "run_manifests"
    all_json = sum(1 for _ in extraction_dir.rglob("*.json"))
    manifest_json = (
        sum(1 for _ in manifests_dir.rglob("*.json"))
        if manifests_dir.exists()
        else 0
    )
    return all_json - manifest_json


def resolve_run_extraction_dir(cfg: dict, domain: str) -> Path:
    """Resolve the extraction output dir for a run config.

    Uses ``extraction.output_dir`` when set, else the ``extracted/<domain>``
    convention.
    """
    extraction_cfg = cfg.get("extraction") or {}
    return Path(extraction_cfg.get("output_dir", f"extracted/{domain}"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _document_eligible(path: Path, root: Path) -> bool:
    """Exclude generated products with the shared format predicate."""
    return (
        path.is_file()
        and document_utils.is_supported_document(path)
        and not any(
            part.startswith(".") for part in path.relative_to(root).parts
        )
        and not path.name.endswith(".section_target.txt")
    )


def _input_documents(path: Path, index: str | None, filters: Sequence[str]):
    """Resolve authoritative documents and their common input root."""
    if index is not None:
        return _index_documents(Path(index), filters)
    if not path.exists():
        message = f"Documents path not found: {path}"
        raise FileNotFoundError(message)
    root = path if path.is_dir() else path.parent
    documents = sorted(
        p
        for p in (root.rglob("*") if path.is_dir() else [path])
        if _document_eligible(p, root)
    )
    return documents, root


def _indexed_source(row: dict, index: Path) -> tuple[Path, Path]:
    """Resolve an indexed document and its declared corpus root."""
    source = Path(row["path"])
    document = (
        source
        if source.is_absolute() or source.is_file()
        else index.parent / source
    ).resolve()
    relative = Path(row.get("relative_path") or document.name)
    if relative.is_absolute() or ".." in relative.parts:
        message = "Download index relative_path must stay inside its corpus"
        raise ValueError(message)
    corpus = document.parent
    if tuple(document.parts[-len(relative.parts) :]) == relative.parts:
        for _ in relative.parts[1:]:
            corpus = corpus.parent
    return document, corpus


def _index_filter_value(value) -> str:
    """Normalize index filters for punctuation-insensitive matching."""
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


def _index_documents(index: Path, filters: Sequence[str]):
    """Read explicit index scope without falling back to scanning."""
    parsed_filters = []
    for value in filters:
        key, separator, term = value.partition("=")
        if not separator or not key.strip():
            message = "Index filters require column=value"
            raise ValueError(message)
        parsed_filters.append((key.strip(), term))

    documents, roots = [], []
    with Path(index).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"status", "path", *(key for key, _ in parsed_filters)}
        if not required.issubset(reader.fieldnames or []):
            message = "Download index is missing required columns"
            raise ValueError(message)
        for row in reader:
            if None in row or None in row.values():
                message = "Download index has malformed rows"
                raise ValueError(message)
            if row["status"] != "downloaded":
                continue
            doc, corpus = _indexed_source(row, index)
            roots.append(corpus)
            if any(
                _index_filter_value(row[key]) != _index_filter_value(term)
                for key, term in parsed_filters
            ):
                continue
            if not doc.is_file():
                message = f"Selected index document is missing: {doc}"
                raise FileNotFoundError(message)
            if _document_eligible(doc, doc.parent):
                documents.append(doc)
    root = Path(
        os.path.commonpath(
            [str(root) for root in roots] or [str(index.parent)]
        )
    )
    return sorted(set(documents)), root


def _validate_extract_paths(
    output: Path,
    root: Path,
    protected: Sequence[Path],
    *,
    inventory: bool = True,
):
    """Reject destructive or self-inventoried output overlaps."""
    if output == Path(output.anchor):
        message = "Extraction output cannot be a filesystem root"
        raise ValueError(message)
    if (
        output == root
        or output in root.parents
        or (inventory and root.is_dir() and root in output.parents)
    ):
        message = "Extraction input/output overlap is not allowed"
        raise ValueError(message)
    for path in protected:
        resolved = path.resolve()
        if output == resolved or output in resolved.parents:
            message = f"Extraction output contains a protected input: {path}"
            raise ValueError(message)


def _reuse_evidence(output: Path) -> dict:
    """Load manifest evidence separately from extracted record JSON."""
    evidence = {}
    for path in sorted((output / "run_manifests").glob("*.manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            for record, value in manifest.get("reuse", {}).items():
                if isinstance(value, dict):
                    evidence.setdefault(record, []).append(value)
        except (OSError, ValueError, TypeError, AttributeError):  # noqa: PERF203 - corrupt manifests invalidate only their own evidence
            continue
    return evidence


def _reusable_record(path: Path, fingerprint: str, evidence: dict, validator):
    """Require digest evidence and valid record/payload contracts."""
    try:
        matching = [
            entry
            for entry in evidence.get(str(path), [])
            if entry.get("input_fingerprint") == fingerprint
        ]
        if not matching:
            return None
        digest = artifact_sha256(path)
        if not any(entry.get("record_sha256") == digest for entry in matching):
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            record["contract_version"] != "1.0.0"
            or record["lineage"].get("input_fingerprint") != fingerprint
        ):
            return None
        record_validator().validate(record)
        validator.validate(record["payload"])
        if (
            record.get("processing_metrics", {})
            .get("coverage", {})
            .get("parser_partial")
        ):
            return None
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        ValidationError,
    ):
        return None
    else:
        return record


def _manual_ranges(inputs: dict, documents: list[Path], root: Path) -> dict:
    """Validate limits and explicit page selection before mutation."""
    limit = inputs.get("limit")
    max_context = inputs.get("max_context", DEFAULT_MAX_CONTEXT)
    if limit is not None and (type(limit) is not int or limit < 1):
        message = "Extraction limit must be a positive integer"
        raise ValueError(message)
    if type(max_context) is not int or max_context < 1:
        message = "Extraction max_context must be a positive integer"
        raise ValueError(message)
    ranges = {}
    pages_csv = inputs.get("pages_csv")
    if pages_csv:
        mappings = load_pages_csv(Path(pages_csv))
        canonical = {
            Path(key).resolve(): value for key, value in mappings.items()
        }
        for doc in documents:
            if doc.resolve() in canonical:
                ranges[doc] = canonical[doc.resolve()]
                continue
            for key in (str(doc), doc.relative_to(root).as_posix(), doc.name):
                if key in mappings:
                    ranges[doc] = mappings[key]
                    break
    elif inputs.get("pages"):
        if len(documents) != 1 or documents[0].suffix.lower() != ".pdf":
            message = "--pages requires exactly one selected PDF"
            raise ValueError(message)
        ranges[documents[0]] = parse_page_range(inputs["pages"])
    return ranges


def _extraction_fingerprints(
    inputs, llm, locator, schema_path, documents, ranges, context
):
    """Bind reused records to effective behavior and source bytes."""
    targeting = inputs.get("page_targeting") or {}
    policy = {
        key: inputs.get(key)
        for key in (
            "model_context_windows",
            "reasoning_models",
            "ocr_corrections",
            "timeout_seconds",
        )
    }
    policy.update(
        {
            "model": llm["model"],
            "provider": llm["provider"],
            "endpoint": llm.get("base_url") or llm.get("azure_endpoint"),
            "api_version": llm.get("azure_api_version"),
            "max_context": inputs.get("max_context", DEFAULT_MAX_CONTEXT),
            "schema_sha256": artifact_sha256(schema_path),
            "page_targeting": targeting,
            "locator": {
                key: (locator or {}).get(key)
                for key in (
                    "model",
                    "provider",
                    "base_url",
                    "azure_endpoint",
                    "azure_api_version",
                )
            },
        }
    )
    fingerprints = {}
    for doc in documents:
        cached = document_utils.read_text_cache(doc, return_meta=True)
        data = {
            **policy,
            "effective_text": (
                {key: cached[1].get(key) for key in ("method", "text_sha256")}
                if cached and cached[1].get("method") != "ocr"
                else None
            ),
            "source": str(doc),
            "source_sha256": artifact_sha256(doc),
            "manual_pages": ranges.get(doc),
            "context": context.get(str(doc), context.get(doc.name)),
        }
        fingerprints[doc] = hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest()
    return fingerprints


def _extraction_layout(inputs, from_index, index_filters, config_path):
    """Resolve schema, documents, outputs, and protected paths once."""
    schema_path = Path(inputs["schema"]).resolve()
    metadata = SchemaMetadata(schema_path)
    array = metadata.get_main_data_array()
    if metadata.schema.get("properties"):
        _, node = metadata.resolve_schema_field(array, root=metadata.schema)
        types = node.get("type")
        if (
            not any(key in node for key in ("anyOf", "allOf", "oneOf"))
            and types != "array"
            and not (isinstance(types, list) and "array" in types)
        ):
            message = (
                f"Extraction main_data_array must resolve to an array: {array}"
            )
            raise ValueError(message)
    path = Path(inputs["path"]).resolve()
    output = Path(
        inputs.get("output")
        or resolve_extract_output_dir(path, is_dir=path.is_dir())
    ).resolve()
    documents, root = _input_documents(path, from_index, index_filters)
    root = root.resolve()
    protected = [
        schema_path,
        *(
            Path(value)
            for value in (config_path, from_index, inputs.get("pages_csv"))
            if value
        ),
    ]
    inventory_root = root if from_index and documents else path

    _validate_extract_paths(
        output,
        inventory_root,
        [*protected, *documents],
        inventory=not from_index,
    )
    outputs = {
        doc: output / doc.relative_to(root).with_suffix(".json")
        for doc in documents
    }
    if any(
        INTERNAL_RECORD_DIRS.intersection(
            target.relative_to(output).parts[:-1]
        )
        for target in outputs.values()
    ):
        message = (
            "Extraction output partition uses a reserved internal "
            "record directory"
        )
        raise ValueError(message)
    protected = {path.resolve() for path in [*protected, *documents]}
    for target in outputs.values():
        resolved = target.resolve()
        if output not in resolved.parents or resolved in protected:
            message = (
                "Extraction output destination escapes or overlaps inputs: "
                f"{target}"
            )
            raise ValueError(message)
    if len({target.resolve() for target in outputs.values()}) != len(outputs):
        message = (
            "Extraction output filename collision; rename or partition sources"
        )
        raise ValueError(message)
    return {
        "schema_path": schema_path,
        "schema": metadata.schema,
        "metadata": metadata,
        "documents": documents,
        "root": root,
        "output": output,
        "outputs": outputs,
    }


def _require_extraction_credentials(llm, locator):
    """Validate only clients that planned extraction will use."""
    for client in (llm, locator):
        if client is None:
            continue
        if not client.get("api_key") or (
            client["provider"] == "azure"
            and not client.get("base_url")
            and not client.get("azure_endpoint")
        ):
            raise APIKeyError


def _extraction_locator_config(inputs: dict, llm: dict) -> dict | None:
    """Validate targeting policy and resolve locator routing."""
    targeting = inputs.get("page_targeting") or {}
    keywords = targeting.get("keywords")
    if keywords is not None and (
        not isinstance(keywords, list)
        or any(not isinstance(keyword, str) for keyword in keywords)
    ):
        message = "pages.auto_locate.keywords must be a list of strings"
        raise ValueError(message)
    if not targeting.get("enabled"):
        return None
    if not str(targeting.get("section_description") or "").strip():
        message = "pages.auto_locate requires section_description"
        raise ValueError(message)
    return resolve_llm_kwargs(
        targeting.get("model"),
        models=inputs.get("models"),
        default_model=llm["model"],
        llm_config=inputs.get("llm_config") if targeting.get("model") else llm,
        provider=None if targeting.get("model") else llm["provider"],
    )


def _prepare_extraction(
    inputs: dict,
    *,
    fresh: bool = False,
    from_index: str | None = None,
    index_filters: Sequence[str] = (),
    config_path: str | None = None,
    validate_only: bool = False,
):
    """Verify work before output mutation or provider calls."""
    plan = _extraction_layout(inputs, from_index, index_filters, config_path)
    documents = plan["documents"]
    ranges = _manual_ranges(inputs, documents, plan["root"])
    llm = resolve_llm_kwargs(
        inputs.get("model"),
        models=inputs.get("models"),
        provider=inputs.get("provider"),
        llm_config=inputs.get("llm_config"),
    )
    locator = _extraction_locator_config(inputs, llm)
    context = build_source_context_map(
        Path(inputs["path"]).resolve(), from_index
    )
    fingerprints = _extraction_fingerprints(
        inputs,
        llm,
        locator,
        plan["schema_path"],
        documents,
        ranges,
        context,
    )
    if fresh and not validate_only:
        for doc, selected in ranges.items():
            if selected is not None and doc.suffix.lower() == ".pdf":
                validate_pdf_scope(doc, selected)
    evidence = (
        _reuse_evidence(plan["output"])
        if not fresh and inputs.get("skip_existing", True)
        else {}
    )
    validator = build_schema_validator(plan["schema"])
    reused = {}
    for doc in documents:
        record = _reusable_record(
            plan["outputs"][doc], fingerprints[doc], evidence, validator
        )
        if record is not None:
            reused[doc] = record
    pending = [doc for doc in documents if doc not in reused]
    if inputs.get("limit"):
        pending = pending[: inputs["limit"]]
    if pending and not validate_only:
        _require_extraction_credentials(llm, None)
    return {
        **plan,
        "pending": pending,
        "reused": reused,
        "llm": llm,
        "ranges": ranges,
        "context": context,
        "fingerprints": fingerprints,
        "inputs": inputs,
        "fresh": fresh,
    }


def _document_text(doc_path, page_range, section_text, corrections):
    """Prepare text and effective scope for one document."""
    scope = {}
    if section_text is not None:
        text, effective = section_text, page_range
    elif doc_path.suffix.lower() == ".pdf":
        text, scope = document_utils.extract_text_from_document(
            doc_path,
            page_range=page_range,
            ocr_corrections=corrections,
            return_meta=True,
        )
        effective = scope.get("page_range") if page_range is not None else None
    else:
        text = document_utils.extract_text_from_document(
            doc_path, page_range=page_range
        )
        effective = page_range
    if not text.strip():
        message = "Document parser returned no source text"
        raise ValueError(message)
    return text, effective, scope


def _extract_one_document(  # noqa: PLR0913 - explicit per-document scope and lineage
    doc_path: Path,
    *,
    extractor,
    loaded_schema: dict,
    schema_metadata,
    schema_path: Path,
    output_path: Path,
    model: str,
    provider: str,
    run_id: str,
    input_fingerprint: str,
    page_range=None,
    section_text=None,
    source_context_map=None,
    ocr_corrections=None,
) -> dict:
    """Extract and persist one record without UI or batch decisions."""
    result = None
    try:
        text, effective_range, parser_scope = _document_text(
            doc_path,
            page_range,
            section_text,
            ocr_corrections,
        )
        text = prepend_source_context(text, doc_path, source_context_map or {})
        result = extractor.extract(text, loaded_schema)
        record_extraction_scope(
            result,
            page_range=effective_range,
            section_filtered=section_text is not None,
            parser_scope=parser_scope,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = write_extraction_record(
            output_path,
            doc_path=doc_path,
            result=result,
            model=model,
            provider=provider,
            run_id=run_id,
            schema_id=str(schema_path),
            schema_metadata=schema_metadata,
            input_fingerprint=input_fingerprint,
        )
        return {
            "file": doc_path.name,
            "success": True,
            "items": count,
            "cost": result.cost,
            "time": getattr(result, "processing_time", 0),
            "input_tokens": getattr(result, "input_tokens", None),
            "output_tokens": getattr(result, "output_tokens", None),
            "output_path": str(output_path),
            "coverage": getattr(result, "coverage", {}),
        }
    except Exception as exc:  # noqa: BLE001 - isolate attempted-document failures
        logger.debug(
            "Document extraction failed for %s",
            doc_path,
            exc_info=True,
        )
        usage = getattr(exc, "usage", None) or {}
        return {
            "file": doc_path.name,
            "success": False,
            "error": str(exc),
            "errors": [
                build_error_record(
                    exc,
                    stage="extract",
                    document_path=str(doc_path),
                    model=model,
                    provider=provider,
                )
            ],
            "cost": getattr(result, "cost", usage.get("cost")),
            "input_tokens": getattr(
                result, "input_tokens", usage.get("input_tokens")
            ),
            "output_tokens": getattr(
                result, "output_tokens", usage.get("output_tokens")
            ),
        }


def _target_extraction_inputs(plan: dict):
    """Resolve optional targeting once for planned documents only."""
    inputs, llm = plan["inputs"], plan["llm"]
    pending = plan["pending"]
    plan["targeting_incomplete"] = set()
    ranges = dict(plan["ranges"])
    sections = {}
    targeting = inputs.get("page_targeting") or {}
    locator_clients = []
    if pending and targeting.get("enabled"):
        options = {
            "model": targeting.get("model"),
            "models": inputs.get("models"),
            "default_model": llm["model"],
            "llm_config": (
                inputs.get("llm_config") if targeting.get("model") else llm
            ),
            "keywords": targeting.get("keywords"),
            "trigger_chars": targeting.get(
                "trigger_chars", DEFAULT_PAGE_TRIGGER_CHARS
            ),
        }
        page_locator = PageLocator(
            targeting["section_description"],
            max_selected_pages=targeting.get(
                "max_selected_pages", DEFAULT_MAX_SELECTED_PAGES
            ),
            **options,
        )
        section_locator = SectionLocator(
            targeting["section_description"], **options
        )
        for doc in pending:
            if doc.suffix.lower() == ".pdf" and doc not in ranges:
                page_locator.incomplete = False
                located = page_locator.locate(doc)
                if page_locator.incomplete:
                    plan["targeting_incomplete"].add(doc)
                if located:
                    ranges[doc] = located
            elif doc.suffix.lower() in {
                ".html",
                ".htm",
                ".doc",
                ".docx",
                ".txt",
            }:
                section_locator.incomplete = False
                text = section_locator.locate(doc)
                if section_locator.incomplete:
                    plan["targeting_incomplete"].add(doc)
                if text is not None:
                    sections[doc] = text
        locator_clients = [page_locator, section_locator]
    return ranges, sections, locator_clients


def _complete_extraction(
    plan, results, extractor, locator_clients, run_id, started
):
    """Aggregate outcomes and publish immutable run evidence."""
    output = plan["output"]
    pending = plan["pending"]
    llm = plan["llm"]
    successes = [result for result in results if result["success"]]
    failed = [result for result in results if not result["success"]]
    status = (
        "partial_failure"
        if failed and successes
        else "failed"
        if failed
        else "reused"
        if results and not pending
        else "success"
        if results
        else "empty"
    )
    main_usage = getattr(getattr(extractor, "client", None), "usage", None)
    returned_usage = any(
        result.get("input_tokens") is not None for result in results
    )
    if main_usage is not None and (main_usage["calls"] or not returned_usage):
        measured = dict(main_usage)
    else:
        measured = {
            "calls": len(pending),
            "cost": sum(result.get("cost") or 0 for result in results),
            "input_tokens": sum(
                result.get("input_tokens") or 0 for result in results
            ),
            "output_tokens": sum(
                result.get("output_tokens") or 0 for result in results
            ),
            "unknown_cost_calls": sum(
                result.get("cost") is None for result in results
            ),
        }
    for client in locator_clients:
        usage = getattr(client, "usage", {})
        for key in measured:
            measured[key] += usage.get(key, 0)
    total_cost = measured["cost"]
    manifest = build_run_manifest(
        run_id=run_id,
        mode="single_model",
        schema_path=plan["schema_path"],
        provider=llm["provider"],
        model=llm["model"],
        runtime_artifact=None,
        doc_files=[*plan["reused"], *pending],
        successful_output_paths=[
            Path(result["output_path"]) for result in successes
        ],
        started_at=started,
        finished_at=datetime.now(timezone.utc).isoformat(),
        total_processed=len(results),
        successful_count=len(successes),
        failed_count=len(failed),
        failed_results=failed,
        costs={
            "total_cost_usd": total_cost,
            "total_input_tokens": measured["input_tokens"],
            "total_output_tokens": measured["output_tokens"],
            "llm_calls": measured["calls"],
            "unknown_cost_calls": measured["unknown_cost_calls"],
            "documents_billed": len(pending),
            "model": llm["model"],
        },
    )
    manifest["status"]["result"] = status
    manifest["coverage"] = {
        result["output_path"]: result["coverage"]
        for result in successes
        if result.get("coverage")
    }
    successful_paths = {result["output_path"] for result in successes}
    manifest["reuse"] = {
        str(plan["outputs"][doc]): {
            "input_fingerprint": plan["fingerprints"][doc],
            "record_sha256": artifact_sha256(plan["outputs"][doc]),
        }
        for doc in pending
        if plan["outputs"][doc].is_file()
        and doc not in plan.get("targeting_incomplete", set())
        and str(plan["outputs"][doc]) in successful_paths
    }
    manifest_path = (
        output
        / "run_manifests"
        / f"{run_id.removeprefix('run://')}.manifest.json"
    )
    atomic_json(manifest_path, manifest)
    return ExtractionRunResult(
        total=len(results),
        successful=len(successes),
        failed=len(failed),
        total_items=sum(result.get("items", 0) for result in successes),
        total_cost=total_cost,
        unknown_cost_calls=measured["unknown_cost_calls"],
        output_dir=output,
        schema_path=plan["schema_path"],
        errors=failed,
        status=status,
        manifest_path=manifest_path,
        records=results,
    )


def _execute_extraction(
    plan: dict, *, on_progress=None
) -> ExtractionRunResult:
    """Execute the plan once and publish its measured outcome."""
    output, pending = plan["output"], plan["pending"]
    inputs, llm = plan["inputs"], plan["llm"]
    extractor = None
    if pending:
        extractor = extraction_api.DocumentExtractor(
            **llm,
            schema_metadata=plan["metadata"],
            max_context_chars=inputs.get("max_context", DEFAULT_MAX_CONTEXT),
            context_windows=inputs.get("model_context_windows"),
            reasoning_models=inputs.get("reasoning_models"),
            timeout=inputs.get("timeout_seconds"),
        )
    run_id = generate_run_id(
        schema_path=plan["schema_path"],
        provider=llm["provider"],
        model=llm["model"],
        enable_validation=False,
        doc_files=plan["documents"],
        artifact_id=None,
    )
    started = datetime.now(timezone.utc).isoformat()
    ranges, sections, locator_clients = _target_extraction_inputs(plan)
    if plan["fresh"] and output.exists() and plan["documents"]:
        shutil.rmtree(output)
    if inputs.get("page_targeting", {}).get("save_discovered"):
        for doc, text in sections.items():
            atomic_write(output / ".sections" / f"{doc.name}.txt", text)
        if ranges:
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            writer.writerow(["file_path", "start_page", "end_page"])
            writer.writerows(
                (str(doc), *rng) for doc, rng in ranges.items() if rng
            )
            atomic_write(
                output / "discovered_page_ranges.csv", buffer.getvalue()
            )
    results = []
    for doc, record in plan["reused"].items():
        results.append(
            {
                "file": doc.name,
                "success": True,
                "skipped": True,
                "items": record.get("item_count", 0),
                "cost": 0,
                "output_path": str(plan["outputs"][doc]),
                "coverage": record.get("processing_metrics", {}).get(
                    "coverage", {}
                ),
            }
        )
    for doc in pending:
        if on_progress:
            on_progress(doc, None)
        result = _extract_one_document(
            doc,
            extractor=extractor,
            loaded_schema=plan["schema"],
            schema_metadata=plan["metadata"],
            schema_path=plan["schema_path"],
            output_path=plan["outputs"][doc],
            model=llm["model"],
            provider=llm["provider"],
            run_id=run_id,
            input_fingerprint=plan["fingerprints"][doc],
            page_range=ranges.get(doc),
            section_text=sections.get(doc),
            source_context_map=plan["context"],
            ocr_corrections=inputs.get("ocr_corrections"),
        )
        results.append(result)
        if on_progress:
            on_progress(doc, result)
    return _complete_extraction(
        plan,
        results,
        extractor,
        locator_clients,
        run_id,
        started,
    )


def extract_documents(
    documents_path: str | Path,
    schema: str | Path,
    *,
    output_dir: str | Path | None = None,
    model: str | None = None,
    provider: str = "auto",
    max_context: int = DEFAULT_MAX_CONTEXT,
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
    docs_path = Path(documents_path)
    inputs = {
        "path": str(docs_path),
        "schema": str(schema),
        "output": str(output_dir or Path("extracted") / docs_path.name),
        "model": model,
        "provider": provider,
        "max_context": max_context,
        "skip_existing": skip_existing,
        "limit": limit,
        "pages_csv": str(pages_csv) if pages_csv else None,
    }
    plan = _prepare_extraction(inputs)
    return _execute_extraction(plan)


def compile_extractions(
    extraction_dir: str | Path,
    schema: str | Path,
    *,
    config_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    report_format: str | None = None,
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
        ``"xlsx"``, ``"csv"``, or ``"both"``. If omitted, runtime output settings
        override schema output settings; absent both, CSV and Excel are emitted.
    dry_run:
        If True, parse and validate but write no output files.

    Returns
    -------
    CompilationResult
    """
    from psweep.compilation.data_compiler import DataCompiler

    ext_dir = Path(extraction_dir)
    schema_path = Path(schema)

    if not ext_dir.exists():
        raise FileNotFoundError(f"Extraction directory not found: {ext_dir}")
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")

    # See ``extract_documents`` above: the Python API keeps the minimal
    # ``compiled/<input-dir-name>`` convention rather than the CLI's
    # ``resolve_compile_output_dir`` (which swaps a nested ``extracted`` component
    # in place and returns an absolute path). Pass ``output_dir`` for full control.
    out_dir = (
        Path(output_dir) if output_dir else Path("compiled") / ext_dir.name
    )
    resolved: dict = {"schema": str(schema_path)}
    if config_path:
        cfg = load_runtime_config_file(Path(config_path))
        resolved = resolve_command_config(
            command="compile",
            cli_values={
                "schema": str(schema_path),
                "extracted_dir": str(ext_dir),
            },
            config_data=cfg,
            strict=False,
        )
    if report_format is not None:
        resolved["compilation_output"] = {
            **resolved.get("compilation_output", {}),
            "default_format": report_format,
        }
    schema_metadata = resolve_compilation_metadata(resolved)
    if (
        schema_metadata.metadata.get("compilation", {})
        .get("synthesis", {})
        .get("enabled")
    ):
        message = (
            "Library compile_extractions does not run synthesis; "
            "use the compile CLI"
        )
        raise ValueError(message)
    compiler = DataCompiler(
        schema_metadata=schema_metadata,
        verbose=False,
        debug=False,
    )
    provenance_warnings = analyze_compile_input_provenance(ext_dir)
    df, _schema_info = compiler.compile_from_directory(
        ext_dir,
        apply_deduplication=not dry_run,
    )

    rows = len(df)
    output_files: list[Path] = []
    if not df.empty:
        if dry_run:
            from contextlib import redirect_stdout
            from io import StringIO

            with redirect_stdout(StringIO()):
                export_df = compiler.deduplicator.deduplicate(df.copy())
            frame = compiler._prepare_output_dataframe(export_df)
            compiler.validate_export(
                frame, schema_metadata.get_output_formats()
            )
        else:
            output_files, _ = compiler.export(
                df,
                out_dir,
                ext_dir.name.replace("_", "-"),
                schema_metadata.get_output_formats(),
            )

    return CompilationResult(
        output_dir=out_dir,
        output_files=output_files,
        total_rows=max(rows, 0),
        deduplicated=not dry_run
        and bool(schema_metadata.get_deduplication_key_fields()),
        warnings=provenance_warnings,
    )


def run_pipeline(
    config_path: str | Path,
    *,
    skip_discover: bool = False,
    skip_extract: bool = False,
    fresh: bool = False,
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
    fresh:
        If True, re-run discovery without checkpoint reuse, re-extract already-
        processed documents, and clear compiled outputs before recompiling.

    Returns
    -------
    PipelineResult

    Raises
    ------
    psweep.exceptions.PipelineError
        If any stage fails.
    """
    from psweep.exceptions import PipelineError

    cfg_path = Path(config_path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config not found: {cfg_path}")

    runtime_cfg = load_runtime_config_file(cfg_path)
    resolve_compilation_metadata(
        resolve_command_config(
            command="compile",
            cli_values={},
            config_data=runtime_cfg,
            strict=True,
        )
    )
    if not skip_extract:
        from psweep.extraction.schema_utils import load_schema

        extract_inputs = resolve_command_config(
            command="extract",
            cli_values={},
            config_data=runtime_cfg,
            strict=True,
        )
        load_schema(Path(extract_inputs["schema"]))

    discovery_enabled = resolve_run_discovery_enabled(cfg_path)
    if discovery_enabled and not skip_discover:
        discovery_inputs = resolve_command_config(
            command="discover",
            cli_values={},
            config_data=runtime_cfg,
            strict=True,
        )
        validate_discover_plan(normalize_discover_inputs(discovery_inputs))
    domain = runtime_cfg.get("domain", cfg_path.parent.name)
    stage_cmds = build_run_stage_commands(
        cfg_path,
        base_cmd=[sys.executable, "-m", "psweep.cli.main"],
        skip_discover=skip_discover,
        skip_extract=skip_extract,
        discovery_enabled=discovery_enabled,
        fresh=fresh,
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

    return PipelineResult(
        domain=domain, stages_run=[stage for stage, _ in stage_cmds]
    )
