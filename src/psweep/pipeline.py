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
import shutil
import subprocess
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path
from typing import Callable, Optional, Sequence

from psweep.config import load_runtime_config_file, resolve_command_config
from psweep.config.runtime_config_loader import resolve_compilation_metadata
from psweep.compilation.input_provenance import (
    analyze_compile_input_provenance,
    INTERNAL_RECORD_DIRS,
)
from psweep.discovery.request_inputs import discovery_request_fields
from jsonschema import ValidationError

from psweep.exceptions import APIKeyError, PipelineError
from psweep.outcomes import EXIT_PARTIAL, exit_code
from psweep import extraction as extraction_api
from psweep.extraction import document_utils, transcription
from psweep.extraction.page_locator import (
    PageLocator,
    DEFAULT_PAGE_TRIGGER_CHARS,
    DEFAULT_MAX_SELECTED_PAGES,
)
from psweep.extraction.section_locator import SectionLocator
from psweep.extraction.pdf_utils import validate_pdf_scope
from psweep.extraction.llm_factory import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_CONTEXT,
    resolve_llm_kwargs,
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
from psweep.utils.file_io import artifact_sha256, atomic_json, atomic_write
from psweep.utils.page_range import load_pages_csv, parse_page_range
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


@dataclass
class PipelineResult:
    """Combined result from a full discover → extract → compile run."""

    domain: str
    stages_run: list[str]
    partial_stages: list[str] = field(default_factory=list)
    extraction: Optional[ExtractionRunResult] = None
    compilation: Optional[CompilationResult] = None


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
    manifest_json = (
        sum(1 for _ in manifests_dir.rglob("*.json"))
        if manifests_dir.exists()
        else 0
    )
    return all_json - manifest_json


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


def _input_documents(path: Path, *, required: bool = True):
    """Resolve authoritative documents and their common input root."""
    if not path.exists():
        if not required:
            return [], path
        message = f"Documents path not found: {path}"
        raise FileNotFoundError(message)
    root = path if path.is_dir() else path.parent
    documents = sorted(
        p
        for p in (root.rglob("*") if path.is_dir() else [path])
        if _document_eligible(p, root)
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
    if (
        output == root
        or output in root.parents
        or (root.is_dir() and root in output.parents)
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
            record["contract_version"] != CONTRACT_VERSION
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


def _extraction_fingerprints(  # noqa: PLR0913 - full effective behavior
    inputs,
    llm,
    locator,
    schema_path,
    documents,
    ranges,
    context,
    roster,
    anchors,
    transcriber=None,
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
            "quote_field",
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
            "roster_fields": roster.fields if roster else None,
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
        info = source_context(doc, context)
        cached = document_utils.read_text_cache(doc, return_meta=True)
        audio = transcription.is_audio(doc)
        data = {
            **policy,
            "effective_text": (
                {key: cached[1].get(key) for key in ("method", "text_sha256")}
                if cached and cached[1].get("method") != "ocr" and not audio
                else None
            ),
            "table_layout": document_utils.TABLE_TEXT_LAYOUT.get(doc.suffix.lower()),
            "source": str(doc),
            "source_sha256": artifact_sha256(doc),
            "manual_pages": ranges.get(doc),
            "context": asdict(info) if info else None,
            "candidates": (
                candidate_hash(roster, anchors[doc]) if doc in anchors else None
            ),
        }
        if audio:
            # A transcript derives from source bytes and this identity.
            data["transcription"] = getattr(transcriber, "identity", None)
        fingerprints[doc] = hashlib.sha256(
            json.dumps(data, sort_keys=True).encode()
        ).hexdigest()
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
            message = (
                f"Extraction main_data_array must resolve to an array: {array}"
            )
            raise ValueError(message)
    path = Path(inputs["path"]).resolve()
    output = Path(inputs["output"]).resolve()
    documents, root = _input_documents(path, required=not validate_only)
    root = root.resolve()
    protected = [
        schema_path,
        *(
            Path(value)
            for value in (config_path, inputs.get("pages_csv"))
            if value
        ),
    ]
    _validate_extract_paths(output, path, [*protected, *documents])
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
    candidates, anchors, roster_errors = (
        _roster_plan(
            documents, plan["root"], context, roster, inputs, transcriber
        )
        if roster and not validate_only
        else ({}, {}, {})
    )
    fingerprints = _extraction_fingerprints(
        inputs,
        llm,
        locator,
        plan["schema_path"],
        documents,
        ranges,
        context,
        roster,
        anchors,
        transcriber,
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
        if doc in roster_errors:
            continue
        record = _reusable_record(
            plan["outputs"][doc], fingerprints[doc], evidence, validator
        )
        if record is not None:
            reused[doc] = record
    pending = [doc for doc in documents if doc not in reused]
    if inputs.get("limit"):
        pending = pending[: inputs["limit"]]
    return {
        **plan,
        "pending": pending,
        "reused": reused,
        "llm": llm,
        "transcriber": transcriber,
        "ranges": ranges,
        "context": context,
        "roster": roster,
        "candidates": candidates,
        "anchors": anchors,
        "roster_errors": roster_errors,
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
        "inputs": inputs,
        "fresh": fresh,
    }


def _document_text(
    doc_path, page_range, section_text, corrections, transcriber=None
):
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
            doc_path, page_range=page_range, transcriber=transcriber
        )
        effective = page_range
    if not text.strip():
        message = "Document parser returned no source text"
        raise ValueError(message)
    return text, effective, scope


def _roster_plan(documents, root, context, roster, inputs, transcriber=None):
    """Anchor every document to roster targets before any provider call."""
    candidates, anchors, errors = {}, {}, {}
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
                text = _document_text(
                    doc, None, None, inputs.get("ocr_corrections"), transcriber
                )[0]
            except Exception as exc:  # noqa: BLE001 - isolate unreadable documents
                errors[doc] = f"Roster candidate scan failed: {exc}"
                continue
            candidates[doc] = scan_candidates(text, roster)
            anchors[doc] = set().union(*(ids for _, ids in candidates[doc]))
        else:
            anchors[doc] = {info.target_id} & roster.targets.keys()
    return candidates, anchors, errors


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
    allowed = {
        roster.key(roster.targets[target_id]) for target_id in target_ids
    }
    items = data.get(array) or []
    data[array] = [item for item in items if roster.key(item) in allowed]
    return len(items) - len(data[array])


def _roster_extract(extractor, schema, array, roster, candidates, budget, info):
    """Extract shared-document candidate windows and drop off-roster items."""
    budget -= len(
        candidates_block(roster, set().union(*(ids for _, ids in candidates)))
    ) + len(prepend_source_context("", info))
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
        validation_notes=[
            note for result in results for note in result.validation_notes
        ],
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        coverage={
            "input_characters": sum(r.coverage["input_characters"] for r in results),
            "extracted_characters": sum(
                r.coverage["extracted_characters"] for r in results
            ),
            "context_truncated": any(r.coverage["context_truncated"] for r in results),
        },
        off_candidate_items=dropped,
    )


def _extract_one_document(doc_path: Path, plan: dict, *, extractor, run_id, page_range=None, section_text=None) -> dict:
    """Extract and persist one record without UI or batch decisions."""
    result = None
    llm, inputs = plan["llm"], plan["inputs"]
    model, provider = llm["model"], llm["provider"]
    output_path = plan["outputs"][doc_path]
    roster = plan["roster"]
    array = plan["metadata"].get_main_data_array()
    info = source_context(doc_path, plan["context"])
    anchors = plan["anchors"].get(doc_path)
    try:
        if doc_path in plan["roster_errors"]:
            raise ValueError(plan["roster_errors"][doc_path])
        if anchors == set():
            text, result = "", _no_roster_match(array)
        elif doc_path in plan["candidates"]:
            text = _document_text(
                doc_path,
                None,
                None,
                inputs.get("ocr_corrections"),
                plan.get("transcriber"),
            )[0]
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
            text, effective_range, parser_scope = _document_text(
                doc_path,
                page_range,
                section_text,
                inputs.get("ocr_corrections"),
                plan.get("transcriber"),
            )
            prompt = prepend_source_context(text, info)
            if anchors:
                prompt = candidates_block(roster, anchors) + prompt
            result = extractor.extract(prompt, plan["schema"])
            record_extraction_scope(
                result,
                page_range=effective_range,
                section_filtered=section_text is not None,
                parser_scope=parser_scope,
            )
            if anchors:
                result.off_candidate_items = _keep_roster_items(
                    result.data, array, roster, anchors
                )
        rejected = verify_quotes(
            result.data,
            text,
            evidence=plan["evidence_paths"],
            rows=plan["quote_rows"],
        )
        result.unverified_quotes = len(rejected)
        result.validation_notes = [*(result.validation_notes or []), *rejected]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = write_extraction_record(
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
            input_fingerprint=plan["fingerprints"][doc_path],
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
    pending = [
        doc
        for doc in plan["pending"]
        if doc not in plan["candidates"]
        and doc not in plan["roster_errors"]
        and plan["anchors"].get(doc) != set()
    ]
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
            "models": sorted(spent),
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
        _require_extraction_credentials(llm, None)
        extractor = extraction_api.DocumentExtractor(
            **llm,
            max_context_chars=inputs.get("max_context", DEFAULT_MAX_CONTEXT),
            context_windows=inputs.get("model_context_windows"),
            reasoning_models=inputs.get("reasoning_models"),
            timeout=inputs.get("timeout_seconds"),
        )
    run_id = f"run://{uuid4().hex}"
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
    workers = inputs.get("max_concurrent", DEFAULT_MAX_CONCURRENT)
    queue, window = iter(pending), deque()
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        while True:
            while len(window) < workers * 2 and (doc := next(queue, None)):
                if on_progress:
                    on_progress(doc, None)
                window.append(
                    (
                        doc,
                        pool.submit(
                            _extract_one_document,
                            doc,
                            plan,
                            extractor=extractor,
                            run_id=run_id,
                            page_range=ranges.get(doc),
                            section_text=sections.get(doc),
                        ),
                    )
                )
            if not window:
                break
            doc, future = window.popleft()
            results.append(future.result())
            if on_progress:
                on_progress(doc, results[-1])
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
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
    lineage = (latest_compilation_manifest(out_dir) or {}).get("lineage") or {}
    return (
        table.is_file()
        and bool(lineage.get("build_fingerprint"))
        and lineage.get("table_sha256") == artifact_sha256(table)
        and lineage["build_fingerprint"] == compiler.fingerprint(ext_dir)
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
    started_at = datetime.now(timezone.utc)
    provenance_warnings = analyze_compile_input_provenance(ext_dir)
    if protected_mode() and provenance_warnings:
        raise ValueError(
            "Protected compilation rejected provenance: "
            + "; ".join(provenance_warnings)
        )
    build_fingerprint = None if dry_run else compiler.fingerprint(ext_dir)
    df, _schema_info = compiler.compile_from_directory(
        ext_dir,
        apply_deduplication=not dry_run,
    )

    rows = len(df)
    output_files: list[Path] = []
    if dry_run:
        if not df.empty:
            from contextlib import redirect_stdout
            from io import StringIO

            with redirect_stdout(StringIO()):
                export_df = (
                    df if compiler.mode == "roster"
                    else compiler.deduplicator.deduplicate(df.copy())
                )
            frame = compiler._prepare_output_dataframe(export_df)
            compiler.validate_export(
                frame, schema_metadata.get_output_formats()
            )
    else:
        output_files, frame = compiler.export(
            df,
            out_dir,
            table.stem,
            schema_metadata.get_output_formats(),
        )
        write_compilation_run_manifest(
            output_dir=out_dir,
            schema_id=str(schema_metadata.schema_path),
            mode=compiler.mode,
            stats={
                "records": rows,
                "columns": len(frame.columns),
                "duplicates_removed": compiler.duplicates_removed,
                "output_formats": schema_metadata.get_output_formats(),
                **(compiler.roster_stats or {}),
            },
            started_at=started_at,
            build_fingerprint=build_fingerprint,
            table_sha256=artifact_sha256(table) if table.is_file() else None,
        )

    return CompilationResult(
        output_dir=out_dir,
        output_files=output_files,
        total_rows=max(rows, 0),
        deduplicated=not dry_run
        and compiler.mode == "rows"
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
        returncode = subprocess.run(cmd).returncode
        if returncode == EXIT_PARTIAL:
            partial.append(stage)
        elif returncode != 0:
            raise PipelineError(
                f"Stage '{stage}' failed (exit {returncode})",
                stage=stage,
                returncode=returncode,
            )
    return partial
