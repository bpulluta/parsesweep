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
    ...     schema="config/my_domain/schema.json",
    ... )
    >>> print(f"Extracted {result.successful}/{result.total} documents")
    >>> compiled = compile_extractions(result.output_dir, schema=result.schema_path)
    >>> print(f"Compiled to {compiled.output_files}")

For full config-driven runs (recommended for complex domains):
    >>> from psweep.pipeline import run_pipeline
    >>> run_pipeline("config/my_domain/run.yaml")
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import io
import subprocess  # noqa: S404 - runs psweep stage subprocesses, no shell
import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, UTC
from uuid import uuid4
from threading import local
from pathlib import Path
from collections.abc import Callable, Sequence
from typing import Any

from psweep.config import load_runtime_config_file, resolve_command_config
from psweep.config.runtime_config_loader import resolve_compilation_metadata
from psweep.compilation.input_provenance import (
    analyze_compile_input_provenance,
    INTERNAL_RECORD_DIRS,
    list_compile_record_files,
    select_compile_inputs,
)
from psweep.discovery.request_inputs import discovery_request_fields
from psweep.discovery.concurrency import bounded_results
from jsonschema import ValidationError

from psweep.exceptions import APIKeyError, PipelineError, SourceChangedError, TargetingSafetyError
from psweep.outcomes import EXIT_PARTIAL, exit_code
from psweep import extraction as extraction_api
from psweep.extraction import document_utils, transcription, page_locator
from psweep.extraction.admission import ADMITTED_DISPOSITIONS, publish_admission
from psweep.extraction.page_locator import (
    PageLocator,
    DEFAULT_PAGE_TRIGGER_CHARS,
    DEFAULT_MAX_SELECTED_PAGES,
)
from psweep.extraction.section_locator import SectionLocator
from psweep.extraction import document_extractor, pdf_utils
from psweep.extraction.pdf_utils import RENDER_DPI, render_pages, validate_pdf_scope
from psweep.extraction.llm_factory import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_CONTEXT,
    resolve_llm_kwargs,
)
from psweep.extraction.prepared_document import (
    CONSUMPTION_VERSION,
    capture_source,
    effective_identity,
    input_provenance,
    visual_pages,
)
from psweep.extraction.provenance import (
    build_source_context_map,
    prepend_source_context,
    source_context,
    build_run_manifest,
)
from psweep.extraction.record_writer import (
    CONTRACT_VERSION,
    record_extraction_scope,
    write_extraction_record,
)
from psweep.extraction.roster import (
    QuoteRows,
    batch,
    candidate_hash,
    candidates_block,
    load_roster,
    scan_candidates,
    verify_quotes,
)
from psweep.extraction.schema_utils import (
    build_schema_validator,
    evidence_paths,
    record_validator,
)
from psweep.extraction.text_processor import calculate_completeness
from psweep.utils.error_taxonomy import build_error_record
from psweep.utils.file_io import artifact_sha256, atomic_json, atomic_write, publication_lock
from psweep.utils.log_context import document_scope
from psweep.utils.page_range import (
    format_page_ranges,
    load_pages_csv,
    normalize_page_ranges,
    parse_page_ranges,
)
from psweep.utils.schema_metadata import SchemaMetadata
from psweep.workspace import Workspace, infer_domain

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
    unknown_cost_calls: int = 0
    status: str = "success"
    manifest_path: Path | None = None
    records: list[dict] = field(default_factory=list)
    withdrawn: list[Path] = field(default_factory=list)
    admitted: int = 0

    @property
    def exit_code(self) -> int:
        """Return the subprocess outcome without importing CLI code."""
        return exit_code(self.status)

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
    status: str = "success"
    reconciliation: dict[str, int] = field(default_factory=dict)
    costs: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult:
    """Combined result from a full discover → extract → compile run."""

    domain: str
    stages_run: list[str]
    partial_stages: list[str] = field(default_factory=list)
    extraction: ExtractionRunResult | None = None
    compilation: CompilationResult | None = None


def _read_config_dict(config_path: str | Path) -> dict:
    """Load run config without bypassing validation failures."""
    return load_runtime_config_file(Path(config_path))


def resolve_run_discovery_enabled(config_path: str | Path) -> bool:
    """Return True when discovery is configured for the run config."""
    cfg = _read_config_dict(config_path)
    discovery_section = cfg.get("discovery")
    return isinstance(discovery_section, dict) and bool(discovery_section)


def run_validation_enabled(config_path: str | Path) -> bool:
    """Return True when the run config has a ``validation:`` section."""
    return bool(_read_config_dict(config_path).get("validation"))


def build_run_stage_commands(
    config_path: str | Path,
    *,
    base_cmd: Sequence[str],
    skip_discover: bool = False,
    skip_extract: bool = False,
    discovery_enabled: bool | None = None,
    fresh: bool = False,
    target_limit: int | None = None,
    extract_input_path: str | Path | None = None,
    extra_flags: Sequence[str] = (),
) -> list[tuple[str, list[str]]]:
    """Build the subprocess commands used to execute a run config pipeline.

    ``fresh`` propagates a "start fresh" signal to discover (ignore the
    checkpoint and refresh the search cache), extract (re-extract already-
    processed documents), and compile (clear compiled outputs before writing),
    giving ``run --fresh`` a fully clean pipeline across all stages.
    """
    cfg_path = Path(config_path)
    stage_cmds: list[tuple[str, list[str]]] = []

    if discovery_enabled is None:
        discovery_enabled = resolve_run_discovery_enabled(cfg_path)

    if not skip_discover and discovery_enabled:
        discover_flags = [*extra_flags]
        if target_limit is not None:
            discover_flags.extend(["--target-limit", str(target_limit)])
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
    compile_flags = [*extra_flags]
    if fresh:
        compile_flags.append("--fresh")
    stage_cmds.append(
        (
            "compile",
            [*base_cmd, "compile", "--config", str(cfg_path), *compile_flags],
        )
    )
    if not skip_extract and run_validation_enabled(cfg_path):
        validate_flags = [*extra_flags, *(["--fresh"] if fresh else [])]
        stage_cmds.append(
            ("validate", [*base_cmd, "validate", "--config", str(cfg_path), *validate_flags])
        )
    return stage_cmds


# ---------------------------------------------------------------------------
# Run-command on-disk helpers
#
# UI-free artifact counting shared by the CLI ``run`` driver.
# ---------------------------------------------------------------------------


def count_curated_documents(curated_dir: Path) -> int:
    """Count the documents extraction would read."""
    return len(_input_documents(curated_dir, required=False)[0])


def count_extracted_documents(extraction_dir: Path) -> int:
    """Count extracted JSONs, excluding ``run_manifests/*.json``."""
    if not extraction_dir.exists():
        return 0
    manifests_dir = extraction_dir / "run_manifests"
    all_json = sum(1 for _ in extraction_dir.rglob("*.json"))
    manifest_json = sum(1 for _ in manifests_dir.rglob("*.json")) if manifests_dir.exists() else 0
    return all_json - manifest_json


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _document_eligible(path: Path, root: Path) -> bool:
    """Exclude generated products with the shared format predicate."""
    return (
        path.is_file()
        and document_utils.is_supported_document(path)
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
        and not path.name.endswith(".section_target.txt")
    )


def _input_documents(path: Path, *, required: bool = True):
    """Resolve authoritative documents and their common input root."""
    if not path.exists():
        if not required:
            return [], path
        message = f"Documents path not found: {path}"
        raise FileNotFoundError(message)
    root = path if path.is_dir() else path.parent
    documents = sorted(
        p for p in (root.rglob("*") if path.is_dir() else [path]) if _document_eligible(p, root)
    )
    return documents, root


def _validate_extract_paths(
    output: Path,
    root: Path,
    protected: Sequence[Path],
):
    """Reject destructive or self-inventoried output overlaps."""
    if output == Path(output.anchor):
        message = "Extraction output cannot be a filesystem root"
        raise ValueError(message)
    if output == root or output in root.parents or (root.is_dir() and root in output.parents):
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
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return evidence


def _withdrawn_records(plan: dict) -> list[Path]:
    """Records whose source document no longer exists under the input root."""
    output, root = plan["output"], plan["root"]
    claimed = set(plan["outputs"].values())
    withdrawn = []
    for path in list_compile_record_files(output) if output.is_dir() else []:
        if path in claimed:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            source = Path(record["document"]["source_path"])
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if source.is_relative_to(root) and not source.exists():
            withdrawn.append(path)
    return withdrawn


_TARGETABLE_SUFFIXES = frozenset({".pdf", ".html", ".htm", ".doc", ".docx", ".txt"})
NO_USABLE_SCOPE = "targeting_no_usable_scope"
CONTEXT_BUDGET_EXCEEDED = "targeting_context_budget_exceeded"


def _auto_targeted(inputs, doc, ranges, candidates, roster_errors, anchors) -> bool:
    """True when automatic targeting governs ``doc`` and its input must fail closed.

    ``ranges`` is the ORIGINAL manual mapping: membership, even with a ``None``
    (blank/full) value, is an explicit user choice that targeting must not replace.
    """
    return bool(
        (inputs.get("page_targeting") or {}).get("enabled")
        and doc.suffix.lower() in _TARGETABLE_SUFFIXES
        and doc not in ranges
        and doc not in candidates
        and doc not in roster_errors
        and anchors.get(doc) != set()
    )


def _plan_targeted(plan: dict, doc: Path) -> bool:
    if not (plan["inputs"].get("page_targeting") or {}).get("enabled"):
        return False
    return _auto_targeted(
        plan["inputs"],
        doc,
        plan["ranges"],
        plan["candidates"],
        plan["roster_errors"],
        plan["anchors"],
    )


def _reusable_record(
    path: Path,
    fingerprint: str,
    evidence: dict,
    validator,
    *,
    protected: bool = False,
    derived_text_sha256: str | None = None,
    ocr_entries: dict[int, str] | None = None,
):
    """Require digest evidence and valid record/payload contracts.

    ``derived_text_sha256`` proves a transcript. ``ocr_entries`` are the valid OCR
    page entries now cached for a PDF: any that disagree with the record's own
    per-page provenance make it stale, while a missing entry is regenerable.
    """
    try:
        matching = [
            entry
            for entry in evidence.get(str(path), [])
            if entry.get("input_fingerprint") == fingerprint
        ]
        if not matching:
            return None
        record_bytes = path.read_bytes()
        digest = hashlib.sha256(record_bytes).hexdigest()
        proof = next(
            (
                entry
                for entry in matching
                if entry.get("record_sha256") == digest
                and entry.get("consumption_version") == CONSUMPTION_VERSION
                and (
                    derived_text_sha256 is None
                    or entry.get("derived_text_sha256") == derived_text_sha256
                )
            ),
            None,
        )
        if proof is None:
            return None
        record = json.loads(record_bytes)
        if (
            record["contract_version"] != CONTRACT_VERSION
            or record["lineage"].get("input_fingerprint") != fingerprint
        ):
            return None
        record_validator().validate(record)
        validator.validate(record["payload"])
        if ocr_entries and pdf_utils.ocr_reuse_conflict(record["input"]["pages"], ocr_entries):
            return None
        coverage = record.get("processing_metrics", {}).get("coverage", {})
        if coverage.get("parser_partial"):
            return None
        if protected and coverage.get("targeting_incomplete"):
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
        return record, proof


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
        canonical = {Path(key).resolve(): value for key, value in mappings.items()}
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
        ranges[documents[0]] = parse_page_ranges(inputs["pages"])
    return ranges


def _extraction_fingerprints(
    inputs,
    llm,
    locator,
    schema_path,
    documents,
    digests,
    ranges,
    context,
    roster,
    anchors,
    effective_inputs,
    roster_text_digests,
    transcriber=None,
):
    """Bind reused records to effective behavior and the prepared source digests."""
    targeting = inputs.get("page_targeting") or {}
    policy = {
        key: inputs.get(key)
        for key in (
            "model_context_windows",
            "reasoning_models",
            "ocr_corrections",
            "timeout_seconds",
            "quote_field",
        )
    }
    policy.update(
        {
            "consumption_version": CONSUMPTION_VERSION,
            "extraction_behavior": document_extractor.EXTRACTION_BEHAVIOR_VERSION,
            "model": llm["model"],
            "provider": llm["provider"],
            "endpoint": llm.get("base_url") or llm.get("azure_endpoint"),
            "api_version": llm.get("azure_api_version"),
            "max_context": inputs.get("max_context", DEFAULT_MAX_CONTEXT),
            "schema_sha256": artifact_sha256(schema_path),
            "page_targeting": targeting,
            "locator_behavior_version": (
                page_locator.LOCATOR_BEHAVIOR_VERSION if targeting.get("enabled") else None
            ),
            "roster_fields": roster.fields if roster else None,
            "target_identity_fields": inputs.get("partition_by"),
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
    if (mode := _input_mode(inputs)) != "text":  # text-only records keep their fingerprints
        policy["input_mode"] = {"mode": mode, "render_dpi": RENDER_DPI}
    fingerprints = {}
    for doc in documents:
        info = source_context(doc, context)
        cached = effective_inputs[doc]
        audio = transcription.is_audio(doc)
        data = {
            **policy,
            "effective_text": cached if cached and not audio else None,
            "table_layout": document_utils.TABLE_TEXT_LAYOUT.get(doc.suffix.lower()),
            "pdf_preparation": (
                {
                    "ocr_page_version": pdf_utils.OCR_PAGE_VERSION,
                    "page_representation_version": pdf_utils.PAGE_REPRESENTATION_VERSION,
                }
                if doc.suffix.lower() == ".pdf"
                else None
            ),
            "source": str(doc),
            "source_sha256": digests[doc],
            "manual_pages": ranges.get(doc),
            "manual_mapped": doc in ranges,
            "context": asdict(info) if info else None,
            "candidates": (candidate_hash(roster, anchors[doc]) if doc in anchors else None),
        }
        if doc in anchors:
            data["roster_text_sha256"] = roster_text_digests.get(doc)
        if audio:
            # A transcript derives from source bytes and this identity.
            data["transcription"] = getattr(transcriber, "identity", None)
        fingerprints[doc] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return fingerprints


def _extraction_layout(inputs, config_path, *, validate_only=False):
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
            message = f"Extraction main_data_array must resolve to an array: {array}"
            raise ValueError(message)
    path = Path(inputs["path"]).resolve()
    output = Path(inputs["output"]).resolve()
    documents, root = _input_documents(path, required=not validate_only)
    root = root.resolve()
    protected = [
        schema_path,
        *(Path(value) for value in (config_path, inputs.get("pages_csv")) if value),
    ]
    _validate_extract_paths(output, path, [*protected, *documents])
    outputs = {doc: output / doc.relative_to(root).with_suffix(".json") for doc in documents}
    if any(
        INTERNAL_RECORD_DIRS.intersection(target.relative_to(output).parts[:-1])
        for target in outputs.values()
    ):
        message = "Extraction output partition uses a reserved internal record directory"
        raise ValueError(message)
    protected = {path.resolve() for path in [*protected, *documents]}
    for target in outputs.values():
        resolved = target.resolve()
        if output not in resolved.parents or resolved in protected:
            message = f"Extraction output destination escapes or overlaps inputs: {target}"
            raise ValueError(message)
    if len({target.resolve() for target in outputs.values()}) != len(outputs):
        message = "Extraction output filename collision; rename or partition sources"
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


def _targeting_trigger(inputs: dict) -> int:
    """Characters above which targeting runs: never above the extraction budget.

    A document that cannot fit ``max_context`` must be targeted even when it is
    below the configured ``trigger_chars``.
    """
    configured = (inputs.get("page_targeting") or {}).get(
        "trigger_chars", DEFAULT_PAGE_TRIGGER_CHARS
    )
    return min(configured, inputs.get("max_context", DEFAULT_MAX_CONTEXT))


def _extraction_locator_config(inputs: dict, llm: dict) -> dict | None:
    """Validate targeting policy and resolve locator routing."""
    targeting = inputs.get("page_targeting") or {}
    keywords = targeting.get("keywords")
    if keywords is not None and (
        not isinstance(keywords, list) or any(not isinstance(keyword, str) for keyword in keywords)
    ):
        message = "pages.auto_locate.keywords must be a list of strings"
        raise ValueError(message)
    if not targeting.get("enabled"):
        return None
    if not str(targeting.get("section_description") or "").strip():
        message = "pages.auto_locate requires section_description"
        raise ValueError(message)
    if not targeting.get("model"):
        message = (
            "pages.auto_locate.model is required when auto_locate is enabled: selection "
            "must use an explicit model other than the extraction model"
        )
        raise ValueError(message)
    locator = resolve_llm_kwargs(
        targeting["model"],
        models=inputs.get("models"),
        default_model=llm["model"],
        llm_config=inputs.get("llm_config"),
    )
    if (locator["provider"], locator["model"]) == (llm["provider"], llm["model"]):
        message = (
            "pages.auto_locate.model resolves to the extraction model; "
            "choose a separate, independently resolved model for selection"
        )
        raise ValueError(message)
    return locator


def _prepare_extraction(
    inputs: dict,
    *,
    fresh: bool = False,
    config_path: str | None = None,
    validate_only: bool = False,
):
    """Verify work before output mutation or provider calls."""
    plan = _extraction_layout(inputs, config_path, validate_only=validate_only)
    documents = plan["documents"]
    ranges = _manual_ranges(inputs, documents, plan["root"])
    llm = resolve_llm_kwargs(
        inputs.get("model"),
        models=inputs.get("models"),
        provider=inputs.get("provider"),
        llm_config=inputs.get("llm_config"),
    )
    locator = _extraction_locator_config(inputs, llm)
    transcriber = transcription.build_transcriber(inputs)
    context = build_source_context_map(Path(inputs["path"]).resolve())
    roster = load_roster(inputs, plan["metadata"])
    digests = {doc: artifact_sha256(doc) for doc in documents}
    effective_inputs = {}
    for doc in documents:
        cached = document_utils.read_text_cache(doc, return_meta=True, source_sha256=digests[doc])
        if (
            cached
            and transcription.is_audio(doc)
            and not transcription.accepts_transcript(cached[1].get("method"), transcriber)
        ):
            cached = None
        effective_inputs[doc] = effective_identity(cached[1]) if cached else None
    candidates, anchors, roster_errors, roster_text_digests = (
        _roster_plan(
            documents, plan["root"], context, roster, inputs, digests, effective_inputs, transcriber
        )
        if roster and not validate_only
        else ({}, {}, {}, {})
    )
    fingerprints = _extraction_fingerprints(
        inputs,
        llm,
        locator,
        plan["schema_path"],
        documents,
        digests,
        ranges,
        context,
        roster,
        anchors,
        effective_inputs,
        roster_text_digests,
        transcriber,
    )
    if fresh and not validate_only:
        for doc, selected in ranges.items():
            if selected is not None and doc.suffix.lower() == ".pdf":
                with capture_source(doc, expected=digests[doc]) as source:
                    validate_pdf_scope(source.path, selected)
    evidence = (
        _reuse_evidence(plan["output"]) if not fresh and inputs.get("skip_existing", True) else {}
    )
    validator = build_schema_validator(plan["schema"])
    reused, reuse_proofs = {}, {}
    for doc in documents:
        if doc in roster_errors:
            continue
        record = _reusable_record(
            plan["outputs"][doc],
            fingerprints[doc],
            evidence,
            validator,
            protected=_auto_targeted(inputs, doc, ranges, candidates, roster_errors, anchors),
            derived_text_sha256=(
                effective_inputs[doc]["text_sha256"]
                if effective_inputs[doc] and transcription.is_audio(doc)
                else None
            ),
            ocr_entries=(
                document_utils.ocr_page_cache(doc, digests[doc]).entries()
                if doc.suffix.lower() == ".pdf"
                else None
            ),
        )
        if record is not None:
            reused[doc], reuse_proofs[doc] = record
    pending = [doc for doc in documents if doc not in reused]
    if inputs.get("limit"):
        pending = pending[: inputs["limit"]]
    return {
        **plan,
        "pending": pending,
        "reused": reused,
        "reuse_proofs": reuse_proofs,
        "llm": llm,
        "transcriber": transcriber,
        "ranges": ranges,
        "context": context,
        "roster": roster,
        "candidates": candidates,
        "anchors": anchors,
        "roster_errors": roster_errors,
        "validator": validator,
        "evidence_paths": evidence_paths(plan["schema"]),
        "quote_rows": (
            QuoteRows(
                plan["metadata"].get_main_data_array(),
                plan["metadata"].get_row_array(),
                inputs["quote_field"],
            )
            if inputs.get("quote_field")
            else None
        ),
        "fingerprints": fingerprints,
        "source_digests": digests,
        "effective_inputs": effective_inputs,
        "roster_text_digests": roster_text_digests,
        "inputs": inputs,
        "fresh": fresh,
        "withdrawn": [] if fresh or validate_only else _withdrawn_records(plan),
    }


def _document_text(
    doc_path,
    page_ranges,
    section_text,
    corrections,
    transcriber=None,
    source=None,
    *,
    allow_empty=False,
):
    """Prepare text and effective scope for one document.

    ``source`` is the captured version consumed; without one the live path is read.
    ``allow_empty`` lets a visual input mode proceed when no page has readable text.
    """
    scope = {}
    if section_text is not None:
        text, effective = section_text, page_ranges
    elif doc_path.suffix.lower() == ".pdf":
        text, scope = document_utils.extract_text_from_document(
            doc_path,
            page_ranges=page_ranges,
            ocr_corrections=corrections,
            return_meta=True,
            source=source,
        )
        effective = scope.get("page_ranges") if page_ranges is not None else None
    else:
        text = document_utils.extract_text_from_document(
            doc_path,
            page_ranges=page_ranges,
            ocr_corrections=corrections,
            transcriber=transcriber,
            source=source,
        )
        effective = page_ranges
    if not text.strip() and not allow_empty:
        message = "Document parser returned no source text"
        raise ValueError(message)
    return text, effective, scope


def _input_mode(inputs) -> str:
    return inputs.get("input_mode") or "text"


def _visual_pages(inputs, parser_scope) -> list[int]:
    return visual_pages(_input_mode(inputs), parser_scope)


def _prepared_input(doc_path, plan, page_ranges, section_text, source=None):
    """Return text, the exact final prompt, and scope; hold unsafe input.

    No document reaches the primary request oversized: the final string is
    measured after every source hint and candidate prefix. Protected (automatic)
    documents are additionally held when targeting selected nothing.
    """
    inputs = plan["inputs"]
    protected = _plan_targeted(plan, doc_path)
    scoped = bool(page_ranges) or section_text is not None
    name = doc_path.name
    if protected and not scoped and doc_path in plan.get("targeting_incomplete", ()):
        message = f"Automatic targeting did not produce a usable scope for {name}"
        raise TargetingSafetyError(message, code=NO_USABLE_SCOPE, document=name)
    text, effective, parser_scope = _document_text(
        doc_path,
        page_ranges,
        section_text,
        inputs.get("ocr_corrections"),
        plan.get("transcriber"),
        source,
        allow_empty=_input_mode(inputs) != "text",
    )
    if not text.strip() and not _visual_pages(inputs, parser_scope):
        message = "Document parser returned no source text"
        raise ValueError(message)
    prompt = prepend_source_context(text, source_context(doc_path, plan["context"]))
    if anchors := plan["anchors"].get(doc_path):
        prompt = candidates_block(plan["roster"], anchors) + prompt
    if protected:
        trigger = _targeting_trigger(inputs)
        if not scoped and len(text) > trigger:
            message = (
                f"{name} has {len(text)} readable characters (pages.auto_locate.trigger_chars "
                f"{trigger}) but targeting selected nothing"
            )
            raise TargetingSafetyError(message, code=NO_USABLE_SCOPE, document=name)
    # Every path, including manual scope and disabled targeting, obeys the same
    # budget: an oversized input is held, never silently recropped.
    limit = inputs.get("max_context", DEFAULT_MAX_CONTEXT)
    if len(prompt) > limit:
        message = (
            f"Prepared extraction input for {name} is {len(prompt)} characters; "
            f"extraction.max_context is {limit}"
        )
        raise TargetingSafetyError(message, code=CONTEXT_BUDGET_EXCEEDED, document=name)
    return text, prompt, effective, parser_scope


def _failed_outcome(doc_path, exc, plan, result=None) -> dict:
    """Canonical failed-document outcome shared by extraction and preflight."""
    usage = getattr(exc, "usage", None) or {}
    blocked = isinstance(exc, TargetingSafetyError)
    outcome = {
        "file": doc_path.name,
        "success": False,
        "error": str(exc),
        "errors": [
            build_error_record(
                exc,
                stage="extract",
                document_path=str(doc_path),
                model=plan["llm"]["model"],
                provider=plan["llm"]["provider"],
            )
        ],
        "cost": 0 if blocked else getattr(result, "cost", usage.get("cost")),
        "input_tokens": getattr(result, "input_tokens", usage.get("input_tokens")),
        "output_tokens": getattr(result, "output_tokens", usage.get("output_tokens")),
    }
    if blocked:
        outcome["targeting_blocked"] = True
    return outcome


def _roster_plan(
    documents, root, context, roster, inputs, digests, effective_inputs, transcriber=None
):
    """Anchor every document to roster targets before any provider call.

    Shared documents are scanned from the version whose digest was fingerprinted.
    """
    candidates, anchors, errors, text_digests = {}, {}, {}, {}
    for doc in documents:
        info = source_context(doc, context)
        if info is None:
            errors[doc] = (
                "No download_index row for "
                f"{doc.relative_to(root).as_posix()}; roster extraction "
                "needs discovery source context"
            )
        elif info.scope == "shared":
            try:
                with capture_source(
                    doc,
                    expected=digests[doc],
                    expected_effective=effective_inputs[doc],
                    transcriber=transcriber,
                ) as source:
                    text = _document_text(
                        doc, None, None, inputs.get("ocr_corrections"), transcriber, source
                    )[0]
            except Exception as exc:  # noqa: BLE001 - isolate unreadable documents
                errors[doc] = f"Roster candidate scan failed: {exc}"
                continue
            text_digests[doc] = hashlib.sha256(text.encode()).hexdigest()
            candidates[doc] = scan_candidates(text, roster)
            anchors[doc] = set().union(*(ids for _, ids in candidates[doc]))
        else:
            anchors[doc] = {info.target_id} & roster.targets.keys()
    return candidates, anchors, errors, text_digests


def _no_roster_match(array):
    return extraction_api.ExtractionResult(
        data={array: []},
        completeness_score=0.0,
        cost=0.0,
        processing_time=0.0,
        validation_notes=[],
        status="no_roster_match",
    )


def _keep_roster_items(data, array, roster, target_ids) -> int:
    """Keep only items naming one of ``target_ids``; return the dropped count."""
    allowed = {roster.key(roster.targets[target_id]) for target_id in target_ids}
    items = data.get(array) or []
    data[array] = [item for item in items if roster.key(item) in allowed]
    return len(items) - len(data[array])


def _roster_extract(extractor, schema, array, roster, candidates, budget, info):
    """Extract shared-document candidate windows and drop off-roster items."""
    budget -= len(candidates_block(roster, set().union(*(ids for _, ids in candidates)))) + len(
        prepend_source_context("", info)
    )
    results, items, dropped = [], [], 0
    for text, target_ids in batch(candidates, max(budget, 1)):
        result = extractor.extract(
            candidates_block(roster, target_ids) + prepend_source_context(text, info),
            schema,
        )
        dropped += _keep_roster_items(result.data, array, roster, target_ids)
        items.extend(result.data[array])
        results.append(result)
    data = {**results[0].data, array: items}
    costs = [result.cost for result in results]

    def total(name):
        values = [getattr(result, name) for result in results]
        return None if None in values else sum(values)

    return extraction_api.ExtractionResult(
        data=data,
        completeness_score=calculate_completeness(data),
        cost=None if None in costs else sum(costs),
        processing_time=sum(result.processing_time for result in results),
        validation_notes=[note for result in results for note in result.validation_notes],
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        coverage={
            "input_characters": sum(r.coverage["input_characters"] for r in results),
            "extracted_characters": sum(r.coverage["extracted_characters"] for r in results),
        },
        off_candidate_items=dropped,
    )


def _capture_input(doc, plan):
    return capture_source(
        doc,
        expected=plan["source_digests"][doc],
        expected_effective=plan["effective_inputs"][doc],
        transcriber=plan.get("transcriber"),
    )


def _verified_roster_text(doc, plan, source):
    text, _, scope = _document_text(
        doc, None, None, plan["inputs"].get("ocr_corrections"), plan.get("transcriber"), source
    )
    if hashlib.sha256(text.encode()).hexdigest() != plan["roster_text_digests"][doc]:
        raise SourceChangedError(
            f"{doc.name} roster evidence changed after preparation; rerun extraction",
            document=doc.name,
        )
    return text, scope


def _extract_one_document(
    doc_path: Path,
    plan: dict,
    *,
    extractor,
    run_id,
    page_ranges=None,
    section_text=None,
    prepare=None,
) -> dict:
    """Extract and persist one record without UI or batch decisions.

    ``prepare(doc)`` resolves ``(page_ranges, section_text)`` inside the document
    boundary, so a targeting failure is this document's failure.
    """
    result = None
    parser_scope = {}
    llm, inputs = plan["llm"], plan["inputs"]
    model, provider = llm["model"], llm["provider"]
    output_path = plan["outputs"][doc_path]
    roster = plan["roster"]
    array = plan["metadata"].get_main_data_array()
    info = source_context(doc_path, plan["context"])
    anchors = plan["anchors"].get(doc_path)
    stack = ExitStack()
    try:
        source = stack.enter_context(_capture_input(doc_path, plan))
        if prepare is not None:
            page_ranges, section_text = prepare(source)
        if doc_path in plan["roster_errors"]:
            raise ValueError(plan["roster_errors"][doc_path])
        text = ""
        provenance = input_provenance("text", {}, [])
        if doc_path in plan["candidates"]:
            text, parser_scope = _verified_roster_text(doc_path, plan, source)
            provenance = input_provenance("text", parser_scope, [])
        if anchors == set():
            result = _no_roster_match(array)
        elif doc_path in plan["candidates"]:
            result = _roster_extract(
                extractor,
                plan["schema"],
                array,
                roster,
                plan["candidates"][doc_path],
                inputs.get("max_context", DEFAULT_MAX_CONTEXT),
                info,
            )
        else:
            text, prompt, effective_ranges, parser_scope = _prepared_input(
                doc_path, plan, page_ranges, section_text, source
            )
            pages = _visual_pages(inputs, parser_scope)
            provenance = input_provenance(_input_mode(inputs), parser_scope, pages)
            visual = {"page_images": render_pages(source.path, pages)} if pages else {}
            result = extractor.extract(prompt, plan["schema"], **visual)
            record_extraction_scope(
                result,
                page_ranges=effective_ranges,
                omitted_page_ranges=plan.get("targeting_omitted", {}).get(doc_path),
                section_filtered=section_text is not None,
                parser_scope=parser_scope,
                targeting_incomplete=doc_path in plan.get("targeting_incomplete", ()),
                visual_pages=pages,
            )
            if anchors:
                result.off_candidate_items = _keep_roster_items(result.data, array, roster, anchors)
        rejected = verify_quotes(
            result.data,
            text,
            evidence=plan["evidence_paths"],
            rows=plan["quote_rows"],
        )
        result.unverified_quotes = len(rejected)
        result.validation_notes = [*(result.validation_notes or []), *rejected]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count, record_digest = write_extraction_record(
            output_path,
            doc_path=doc_path,
            source_document_id=doc_path.relative_to(plan["root"]).as_posix(),
            source=info,
            result=result,
            model=model,
            provider=provider,
            run_id=run_id,
            schema_id=str(plan["schema_path"]),
            schema_metadata=plan["metadata"],
            source_sha256=source.sha256,
            input_provenance=provenance,
            input_fingerprint=plan["fingerprints"][doc_path],
            payload_validator=plan["validator"],
        )
        return {
            "file": doc_path.name,
            "success": True,
            "items": count,
            "record_sha256": record_digest,
            "cost": result.cost,
            "time": getattr(result, "processing_time", 0),
            "input_tokens": getattr(result, "input_tokens", None),
            "output_tokens": getattr(result, "output_tokens", None),
            "output_path": str(output_path),
            "coverage": getattr(result, "coverage", {}),
            "derived_text_sha256": (
                None
                if page_ranges is not None
                else source.cached_text.sha256
                if source.cached_text and transcription.is_audio(doc_path)
                else hashlib.sha256(text.encode()).hexdigest()
                if transcription.is_audio(doc_path)
                else None
            ),
        }
    except Exception as exc:
        logger.debug(
            "Document extraction failed for %s",
            doc_path,
            exc_info=True,
        )
        return _failed_outcome(doc_path, exc, plan, result)
    finally:
        stack.close()


def _target_extraction_inputs(plan: dict):
    """Build lazy document targeting with worker-local clients and mutable hints."""
    inputs, llm = plan["inputs"], plan["llm"]
    plan["targeting_incomplete"] = set()
    plan["targeting_omitted"] = {}
    ranges, sections, locator_clients = dict(plan["ranges"]), {}, []
    targeting = inputs.get("page_targeting") or {}
    worker = local()
    prepared = {}
    plan["scope_identities"] = prepared

    def target(source):
        doc = source.original
        if doc in prepared:
            if prepared[doc] != source.identity:
                raise SourceChangedError(
                    f"{doc.name} scope input changed since preflight; rerun extraction",
                    document=doc.name,
                )
            return
        if not _plan_targeted(plan, doc):
            return
        locator_type = PageLocator if doc.suffix.lower() == ".pdf" else SectionLocator
        if not hasattr(worker, "locators"):
            worker.locators = {}
        if locator_type not in worker.locators:
            options = {
                "model": targeting.get("model"),
                "models": inputs.get("models"),
                "default_model": llm["model"],
                "llm_config": (inputs.get("llm_config") if targeting.get("model") else llm),
                "keywords": targeting.get("keywords"),
                "trigger_chars": _targeting_trigger(inputs),
            }
            if locator_type is PageLocator:
                options["max_selected_pages"] = targeting.get(
                    "max_selected_pages", DEFAULT_MAX_SELECTED_PAGES
                )
            locator = locator_type(targeting["section_description"], **options)
            worker.locators[locator_type] = locator
            locator_clients.append(locator)
        locator = worker.locators[locator_type]
        info = source_context(doc, plan["context"])
        roster = plan["roster"]
        fields = roster.fields if roster else inputs.get("partition_by")
        identity = {}
        if info is not None:
            identity = roster.targets.get(info.target_id, {}) if roster else info.target_metadata
        subject = identity.get(fields[-1]) if fields else None
        locator.set_source_context(info, subject=str(subject) if subject is not None else None)
        with document_scope(doc.name):
            selection = locator.locate(source)
            if locator_type is PageLocator:
                if selection:
                    ranges[doc] = selection.ranges
                    logger.info(
                        "Page targeting selected pages %s", format_page_ranges(selection.ranges)
                    )
                if selection and selection.omitted:
                    plan["targeting_omitted"][doc] = selection.omitted
                    logger.warning(
                        "Page targeting left out relevant pages %s to stay within "
                        "max_selected_pages; raise pages.auto_locate.max_selected_pages "
                        "to include them",
                        format_page_ranges(selection.omitted),
                    )
            elif selection is not None:
                sections[doc] = selection
            if locator.incomplete:
                plan["targeting_incomplete"].add(doc)
                logger.warning(
                    "Targeting incomplete; %s (record will not be reused)",
                    "no usable selection, document held"
                    if selection is None
                    else "using a partial selection",
                )
        prepared[doc] = source.identity

    return ranges, sections, locator_clients, target


def extraction_lock_path(output: Path) -> Path:
    """Sibling lock: one writer per extraction output, outside its inventoried tree."""
    return output.parent / f".{output.name}.publication.lock"


def _admission_scope(plan, results) -> dict[str, dict]:
    """Disposition of every document in this invocation's scope, before ``limit``."""
    output = plan["output"]
    succeeded = {result["output_path"]: result for result in results if result["success"]}
    attempted = set(plan["pending"]) | set(plan["reused"])
    scope = {}
    for doc in plan["documents"]:
        path = plan["outputs"][doc]
        outcome = succeeded.get(str(path))
        if outcome is not None:
            entry = {
                "disposition": "reused" if outcome.get("skipped") else "new",
                "record_sha256": outcome["record_sha256"],
                "source_sha256": plan["source_digests"][doc],
                "input_fingerprint": plan["fingerprints"][doc],
                "items": outcome.get("items", 0),
            }
        else:
            entry = {"disposition": "failed" if doc in attempted else "deferred"}
        scope[path.relative_to(output).as_posix()] = entry
    return scope


def _complete_extraction(plan, results, extractor, locator_clients, run_id, started):
    """Aggregate outcomes and publish immutable run evidence."""
    output = plan["output"]
    pending = plan["pending"]
    llm = plan["llm"]
    successes = [result for result in results if result["success"]]
    failed = [result for result in results if not result["success"]]
    blocked = sum(bool(result.get("targeting_blocked")) for result in failed)
    status = (
        "failed"
        if blocked
        else "partial_failure"
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
    returned_usage = any(result.get("input_tokens") is not None for result in results)
    if main_usage is not None and (main_usage["calls"] or not returned_usage):
        measured = dict(main_usage)
    else:
        measured = {
            "calls": sum(
                not (result.get("skipped") or result.get("targeting_blocked")) for result in results
            ),
            "cost": sum(result.get("cost") or 0 for result in results),
            "input_tokens": sum(result.get("input_tokens") or 0 for result in results),
            "output_tokens": sum(result.get("output_tokens") or 0 for result in results),
            "unknown_cost_calls": sum(result.get("cost") is None for result in results),
        }
    spent = {llm["model"]} if measured["calls"] else set()
    for client in (*locator_clients, plan.get("transcriber")):
        usage = getattr(client, "usage", {})
        if usage.get("calls"):
            spent.add(client.model)
        for key in measured:
            measured[key] += usage.get(key, 0)
    total_cost = measured["cost"]
    manifest = build_run_manifest(
        run_id=run_id,
        schema_path=plan["schema_path"],
        provider=llm["provider"],
        model=llm["model"],
        runtime_artifact=None,
        doc_files=[*plan["reused"], *pending],
        successful_output_paths=[Path(result["output_path"]) for result in successes],
        started_at=started,
        finished_at=datetime.now(UTC).isoformat(),
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
            "documents_billed": len(pending) - blocked,
            "models": sorted(spent),
        },
    )
    manifest["status"]["result"] = status
    manifest["status"]["items"] = sum(result.get("items", 0) for result in successes)
    manifest["status"]["zero_item_records"] = sum(not result.get("items") for result in successes)
    manifest["coverage"] = {
        result["output_path"]: result["coverage"] for result in successes if result.get("coverage")
    }
    successful_by_path = {result["output_path"]: result for result in successes}
    manifest["reuse"] = {
        str(plan["outputs"][doc]): {
            "input_fingerprint": plan["fingerprints"][doc],
            "record_sha256": successful_by_path[str(plan["outputs"][doc])]["record_sha256"],
            "consumption_version": CONSUMPTION_VERSION,
            "derived_text_sha256": successful_by_path[str(plan["outputs"][doc])].get(
                "derived_text_sha256"
            ),
        }
        for doc in pending
        if plan["outputs"][doc].is_file()
        and doc not in plan.get("targeting_incomplete", set())
        and str(plan["outputs"][doc]) in successful_by_path
    }
    manifest["withdrawn"] = [str(path) for path in plan["withdrawn"]]
    manifest_path = output / "run_manifests" / f"{run_id.removeprefix('run://')}.manifest.json"
    atomic_json(manifest_path, manifest)
    admission = publish_admission(output, run_id=run_id, scope=_admission_scope(plan, results))
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
        withdrawn=plan["withdrawn"],
        admitted=sum(entry["disposition"] in ADMITTED_DISPOSITIONS for entry in admission.values()),
    )


def _execute_extraction(plan: dict, *, on_progress=None) -> ExtractionRunResult:
    """Execute the plan once under the output's single-writer publication lock."""
    with publication_lock(extraction_lock_path(plan["output"])):
        return _execute_locked(plan, on_progress=on_progress)


def _execute_locked(plan: dict, *, on_progress=None) -> ExtractionRunResult:
    """Execute the plan once and publish its measured outcome.

    ``on_progress(doc, None)`` fires when a pending document starts and
    ``on_progress(doc, outcome)`` as soon as it finishes, so one slow document
    never hides the others; ``records`` keep the planned document order. Log
    records emitted while a document is located or extracted name it.
    """
    output, pending = plan["output"], plan["pending"]
    inputs, llm = plan["inputs"], plan["llm"]
    extractor = None
    if pending:
        _require_extraction_credentials(llm, None)
        extractor = extraction_api.DocumentExtractor(
            **llm,
            max_context_chars=inputs.get("max_context", DEFAULT_MAX_CONTEXT),
            context_windows=inputs.get("model_context_windows"),
            reasoning_models=inputs.get("reasoning_models"),
            timeout=inputs.get("timeout_seconds"),
        )
    run_id = f"run://{uuid4().hex}"
    started = datetime.now(UTC).isoformat()
    workers = inputs.get("max_concurrent", DEFAULT_MAX_CONCURRENT)
    ranges, sections, locator_clients, target = _target_extraction_inputs(plan)
    if plan["fresh"] and output.exists() and plan["documents"]:
        # Preserve prior output if a needed locator lacks credentials or any
        # protected input is unsafe. Normal runs target inside extraction workers
        # instead of a preparation barrier; only compact scopes survive preflight.
        def preflight(doc):
            try:
                with _capture_input(doc, plan) as source:
                    target(source)
                    if doc in plan["candidates"]:
                        _verified_roster_text(doc, plan, source)
                    elif _plan_targeted(plan, doc):
                        _prepared_input(doc, plan, ranges.get(doc), sections.get(doc), source)
            except (TargetingSafetyError, SourceChangedError) as exc:
                return doc, exc
            return doc, None

        order = {doc: index for index, doc in enumerate(pending)}
        held = sorted(
            (
                (doc, exc)
                for doc, exc in bounded_results(preflight, pending, workers=workers)
                if exc is not None
            ),
            key=lambda item: order[item[0]],
        )
        if held:
            blocked = []
            for doc, exc in held:
                if on_progress:
                    on_progress(doc, None)
                blocked.append(_failed_outcome(doc, exc, plan))
                if on_progress:
                    on_progress(doc, blocked[-1])
            return _complete_extraction(
                {**plan, "pending": [doc for doc, _ in held], "reused": {}, "withdrawn": []},
                blocked,
                extractor,
                locator_clients,
                run_id,
                started,
            )
        # Do not destroy historical bytes before workers verify retained scopes.
        # Remove stale products only after every fresh consumer succeeded.
    fresh_products = (
        set(output.rglob("*")) if plan["fresh"] and plan["documents"] and output.exists() else set()
    )
    results = []
    for doc, record in plan["reused"].items():
        try:
            # Reuse was judged at prepare time, before this writer held the lock.
            if (
                hashlib.sha256(plan["outputs"][doc].read_bytes()).hexdigest()
                != plan["reuse_proofs"][doc]["record_sha256"]
            ):
                raise SourceChangedError(
                    f"{doc.name} record changed since preparation; rerun extraction",
                    document=doc.name,
                )
            with _capture_input(doc, plan) as source:
                stale = (
                    source.cached_text.sha256
                    != plan["reuse_proofs"][doc].get("derived_text_sha256")
                    if source.cached_text and transcription.is_audio(doc)
                    else pdf_utils.ocr_reuse_conflict(record["input"]["pages"], source.ocr_pages)
                )
                if stale:
                    raise SourceChangedError(
                        f"{doc.name} derived evidence differs from reuse proof; rerun extraction",
                        document=doc.name,
                    )
                if doc in plan["candidates"]:
                    _verified_roster_text(doc, plan, source)
        except Exception as exc:  # noqa: BLE001 - stale reuse is a document failure
            results.append(_failed_outcome(doc, exc, plan))
            continue
        results.append(
            {
                "file": doc.name,
                "success": True,
                "skipped": True,
                "items": record.get("item_count", 0),
                "record_sha256": plan["reuse_proofs"][doc]["record_sha256"],
                "cost": 0,
                "output_path": str(plan["outputs"][doc]),
                "coverage": record.get("processing_metrics", {}).get("coverage", {}),
            }
        )

    def scope(source):
        target(source)
        return ranges.get(source.original), sections.get(source.original)

    def extract(doc):
        return _extract_one_document(doc, plan, extractor=extractor, run_id=run_id, prepare=scope)

    if pending:
        # Records are about to be rewritten one by one; until the final pointer
        # lands they are not admitted, so an interrupted run never leaves new
        # bytes under an old digest. Rerunning extraction re-admits them.
        publish_admission(
            output,
            run_id=run_id,
            scope={
                plan["outputs"][doc].relative_to(output).as_posix(): {"disposition": "deferred"}
                for doc in pending
            },
        )
    queue, running = enumerate(pending), {}
    outcomes = [None] * len(pending)
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        while True:
            # Submit only into free workers, so a started document is running.
            while len(running) < workers and (item := next(queue, None)):
                doc = item[1]
                if on_progress:
                    on_progress(doc, None)
                future = pool.submit(document_scope(doc.name)(extract), doc)
                running[future] = item
            if not running:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=running.get):
                index, doc = running.pop(future)
                outcomes[index] = future.result()
                section = sections.pop(doc, None)
                if (
                    section is not None
                    and outcomes[index]["success"]
                    and inputs.get("page_targeting", {}).get("save_discovered")
                ):
                    atomic_write(
                        output
                        / ".sections"
                        / doc.relative_to(plan["root"]).with_suffix(doc.suffix + ".txt"),
                        section,
                    )
                if on_progress:
                    on_progress(doc, outcomes[index])
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    results.extend(outcomes)
    if fresh_products and all(result["success"] for result in outcomes):
        current = {plan["outputs"][doc] for doc in pending}
        current.update(
            output / ".sections" / doc.relative_to(plan["root"]).with_suffix(doc.suffix + ".txt")
            for doc, result in zip(pending, outcomes, strict=True)
            if result.get("coverage", {}).get("section_filtered")
        )
        for path in fresh_products - current:
            if path.is_file():
                path.unlink(missing_ok=True)
        for path in sorted(fresh_products, key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    if inputs.get("page_targeting", {}).get("save_discovered"):
        # A reused record was extracted from the pages its coverage recorded.
        successful = {result["output_path"]: result for result in results if result["success"]}
        used = {
            doc: normalize_page_ranges(coverage["selected_page_ranges"])
            for doc, path in plan["outputs"].items()
            if str(path) in successful
            and "selected_page_ranges" in (coverage := successful[str(path)].get("coverage", {}))
        }
        used.update(
            {doc: found for doc, found in ranges.items() if str(plan["outputs"][doc]) in successful}
        )
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["file_path", "start_page", "end_page"])
        writer.writerows(
            (str(doc), start, end)
            for doc in plan["documents"]
            for start, end in used.get(doc) or ()
        )
        atomic_write(output / "discovered_page_ranges.csv", buffer.getvalue())
    for record in plan["withdrawn"]:
        record.unlink(missing_ok=True)
        logger.info("Withdrew %s: its source document left %s", record, plan["root"])
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
    """Extract every document under *documents_path* (no CLI, Rich, or sys.exit).

    ``output_dir`` defaults to ``outputs/<domain>/extracted`` with the domain
    inferred from *documents_path*; per-document errors land in ``result.errors``.
    """
    docs_path = Path(documents_path)
    inputs = {
        "path": str(docs_path),
        "schema": str(schema),
        "output": str(output_dir or Workspace(infer_domain(docs_path)).extracted),
        "model": model,
        "provider": provider,
        "max_context": max_context,
        "skip_existing": skip_existing,
        "limit": limit,
        "pages_csv": str(pages_csv) if pages_csv else None,
    }
    plan = _prepare_extraction(inputs)
    return _execute_extraction(plan)


def _compiler(extraction_dir, schema, *, config_path=None, output_dir=None, report_format=None):
    """Resolve compile settings exactly like ``psweep compile --config``."""
    from psweep.compilation.data_compiler import DataCompiler
    from psweep.security import validate_protected_paths

    ext_dir, schema_path = Path(extraction_dir), Path(schema)
    if not ext_dir.exists():
        raise FileNotFoundError(f"Extraction directory not found: {ext_dir}")
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")
    resolved: dict = {"schema": str(schema_path)}
    if config_path:
        resolved = resolve_command_config(
            command="compile",
            cli_values={"schema": str(schema_path), "extracted_dir": str(ext_dir)},
            config_data=load_runtime_config_file(Path(config_path)),
        )
    workspace = Workspace(resolved.get("domain") or infer_domain(ext_dir))
    out_dir = Path(output_dir or workspace.compiled)
    validate_protected_paths({"input_dir": ext_dir, "output_dir": out_dir})
    if report_format is not None:
        resolved["compilation_output"] = {
            **resolved.get("compilation_output", {}),
            "default_format": report_format,
        }
    compiler = DataCompiler(
        schema_metadata=resolve_compilation_metadata(resolved),
        inputs=resolved,
        verbose=False,
        debug=False,
    )
    return compiler, ext_dir, out_dir, out_dir / f"{workspace.compiled_name}.csv"


def compiled_table_fresh(
    extraction_dir: str | Path,
    schema: str | Path,
    *,
    config_path: str | Path | None,
    output_dir: str | Path | None,
) -> bool:
    """True when the newest compile manifest matches the records and the CSV bytes."""
    from psweep.compilation.accounting import latest_compilation_manifest

    compiler, ext_dir, out_dir, table = _compiler(
        extraction_dir, schema, config_path=config_path, output_dir=output_dir
    )
    manifest = latest_compilation_manifest(out_dir) or {}
    lineage = manifest.get("lineage") or {}
    try:
        current = compiler.fingerprint(ext_dir)
    except ValueError:  # broken admission evidence is never a fresh table
        return False
    return (
        manifest.get("status", {}).get("result", "success") == "success"
        and table.is_file()
        and bool(lineage.get("build_fingerprint"))
        and lineage.get("table_sha256") == artifact_sha256(table)
        and lineage["build_fingerprint"] == current
    )


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

    ``config_path`` applies the run config's ``compilation`` settings like
    ``psweep compile --config``; ``output_dir`` defaults to
    ``outputs/<domain>/compiled`` with the domain inferred from *extraction_dir*.
    """
    from psweep.compilation.accounting import write_compilation_run_manifest
    from psweep.security import protected_mode

    compiler, ext_dir, out_dir, table = _compiler(
        extraction_dir,
        schema,
        config_path=config_path,
        output_dir=output_dir,
        report_format=report_format,
    )
    schema_metadata = compiler.schema_metadata
    started_at = datetime.now(UTC)
    selected = select_compile_inputs(ext_dir)
    provenance_warnings = analyze_compile_input_provenance(selected)
    if protected_mode() and provenance_warnings:
        raise ValueError(
            "Protected compilation rejected provenance: " + "; ".join(provenance_warnings)
        )
    build_fingerprint = None if dry_run else compiler.fingerprint(ext_dir, inputs=selected)
    df, _schema_info = compiler.compile_from_directory(
        ext_dir,
        inputs=selected,
        apply_deduplication=not dry_run,
        allow_model=not dry_run,
    )

    rows = len(df)
    output_files: list[Path] = []
    if dry_run:
        if not df.empty:
            from contextlib import redirect_stdout
            from io import StringIO

            with redirect_stdout(StringIO()):
                export_df = (
                    df
                    if compiler.mode == "roster"
                    else compiler.deduplicator.deduplicate(df.copy())
                )
            frame = compiler._prepare_output_dataframe(export_df)  # noqa: SLF001 - package-internal helper
            compiler.validate_export(frame, schema_metadata.get_output_formats())
    else:
        try:
            output_files, frame = compiler.export(
                df,
                out_dir,
                table.stem,
                schema_metadata.get_output_formats(),
            )
        except Exception:
            write_compilation_run_manifest(
                output_dir=out_dir,
                schema_id=str(schema_metadata.schema_path),
                mode=compiler.mode,
                stats={},
                started_at=started_at,
                build_fingerprint=build_fingerprint,
                table_sha256=None,
                status="failed",
                costs=compiler.reconciliation_costs,
                reconciliation=compiler.reconciliation_stats,
            )
            raise
        write_compilation_run_manifest(
            output_dir=out_dir,
            schema_id=str(schema_metadata.schema_path),
            mode=compiler.mode,
            stats={
                "records": rows,
                "columns": len(frame.columns),
                "duplicates_removed": compiler.duplicates_removed,
                "output_formats": schema_metadata.get_output_formats(),
                "admission": selected.summary,
                **(compiler.roster_stats or {}),
            },
            started_at=started_at,
            build_fingerprint=build_fingerprint,
            table_sha256=artifact_sha256(table) if table.is_file() else None,
            status=compiler.status,
            costs=compiler.reconciliation_costs,
            reconciliation=compiler.reconciliation_stats,
        )

    return CompilationResult(
        output_dir=out_dir,
        output_files=output_files,
        total_rows=max(rows, 0),
        deduplicated=not dry_run
        and compiler.mode == "rows"
        and bool(schema_metadata.get_deduplication_key_fields()),
        warnings=provenance_warnings,
        status="dry_run" if dry_run else compiler.status,
        reconciliation=compiler.reconciliation_stats,
        costs=compiler.reconciliation_costs,
    )


def run_pipeline(
    config_path: str | Path,
    *,
    skip_discover: bool = False,
    skip_extract: bool = False,
    fresh: bool = False,
) -> PipelineResult:
    """Run the full pipeline (discover → extract → compile → validate) from a run config.

    This is a thin subprocess-based orchestrator that mirrors the CLI ``run``
    command. It is suitable for scripting and automation where you want
    programmatic control over the pipeline without the Rich terminal UI.

    Raises ``psweep.exceptions.PipelineError`` when a stage fails; stages that
    finish with usable output and recorded errors are listed in
    ``PipelineResult.partial_stages`` instead.
    """
    cfg_path = Path(config_path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config not found: {cfg_path}")

    runtime_cfg = load_runtime_config_file(cfg_path)
    resolve_compilation_metadata(
        resolve_command_config(
            command="compile",
            cli_values={},
            config_data=runtime_cfg,
        )
    )
    if not skip_extract:
        from psweep.extraction.schema_utils import load_schema

        extract_inputs = resolve_command_config(
            command="extract",
            cli_values={},
            config_data=runtime_cfg,
        )
        load_schema(Path(extract_inputs["schema"]))

    discovery_enabled = resolve_run_discovery_enabled(cfg_path)
    if discovery_enabled and not skip_discover:
        discovery_request_fields(
            resolve_command_config(
                command="discover",
                cli_values={},
                config_data=runtime_cfg,
            )
        )
    domain = runtime_cfg["domain"]
    stage_cmds = build_run_stage_commands(
        cfg_path,
        base_cmd=[sys.executable, "-m", "psweep.cli.main"],
        skip_discover=skip_discover,
        skip_extract=skip_extract,
        discovery_enabled=discovery_enabled,
        fresh=fresh,
        extra_flags=("-q",),
    )

    partial = execute_run_stages(
        stage_cmds,
        on_stage=lambda _index, stage: logger.info("Running stage: %s", stage),
    )

    return PipelineResult(
        domain=domain,
        stages_run=[stage for stage, _ in stage_cmds],
        partial_stages=partial,
    )


def execute_run_stages(
    stage_cmds: Sequence[tuple[str, list[str]]],
    on_stage: Callable[[int, str], None] | None = None,
) -> list[str]:
    """Run stage subprocesses in order and return the stages that ended partial.

    A partial stage (usable output plus recorded errors) does not stop the
    stages after it; re-running retries its failed items. Any other non-zero
    exit raises ``PipelineError``.
    """
    partial: list[str] = []
    for index, (stage, cmd) in enumerate(stage_cmds, 1):
        if on_stage is not None:
            on_stage(index, stage)
        returncode = subprocess.run(cmd, check=False).returncode  # noqa: S603 - argv built from sys.executable
        if returncode == EXIT_PARTIAL:
            partial.append(stage)
        elif returncode != 0:
            raise PipelineError(
                f"Stage '{stage}' failed (exit {returncode})",
                stage=stage,
                returncode=returncode,
            )
    return partial
