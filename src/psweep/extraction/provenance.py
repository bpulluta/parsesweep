"""Provenance and lineage helpers for the extraction stage.

These helpers build the deterministic run identifier and run manifest that tie
extracted records back to their inputs, and map discovered documents to the
source URL / queried target recorded during discovery. They are pure functions
(no Click, no console I/O) so they can be reused and tested outside the
``extract`` CLI command.

Public API
----------
- :func:`generate_run_id` — deterministic run id for lineage joins.
- :func:`build_run_manifest` — deterministic run-manifest payload.
- :func:`build_source_context_map` — map documents to discovery provenance.
- :func:`prepend_source_context` — prepend a CONTEXT block to document text.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from psweep.utils.error_taxonomy import summarize_error_records


def _generate_run_id(
    *,
    schema_path: Path,
    provider: str,
    model: str,
    enable_validation: bool,
    doc_files: List[Path],
    artifact_id: Optional[str],
) -> str:
    """Generate a deterministic run identifier for lineage joins."""
    seed = {
        "schema": schema_path.as_posix(),
        "provider": provider,
        "model": model,
        "mode": "validation" if enable_validation else "single_model",
        "documents": sorted(path.as_posix() for path in doc_files),
        "artifact_id": artifact_id,
    }
    canonical = json.dumps(seed, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"run://{digest[:16]}"


def _build_run_manifest(
    *,
    run_id: str,
    mode: str,
    schema_path: Path,
    provider: str,
    model: str,
    runtime_artifact: Optional[Dict[str, Any]],
    doc_files: List[Path],
    successful_output_paths: List[Path],
    started_at: str,
    finished_at: str,
    total_processed: int,
    successful_count: int,
    failed_count: int,
    failed_results: Optional[List[Dict[str, Any]]] = None,
    costs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a deterministic run manifest payload for process executions."""
    lineage = (runtime_artifact or {}).get("lineage") or {}
    manifest_errors = summarize_error_records(
        error
        for result in (failed_results or [])
        for error in result.get("errors", [])
    )
    resolved_artifact_id = (
        (runtime_artifact or {}).get("artifact_id")
        or ((runtime_artifact or {}).get("lineage") or {}).get("artifact_id")
        or f"artifact://runtime/{run_id.replace('run://', '')}"
    )
    manifest = {
        "manifest_version": "1.0.0",
        "run_id": run_id,
        "mode": mode,
        "lineage": {
            "artifact_id": resolved_artifact_id,
            "profile_id": lineage.get("profile_id") or "default",
            "schema_id": schema_path.as_posix(),
            "provider": provider,
            "model": model,
        },
        "documents": sorted(path.as_posix() for path in doc_files),
        "outputs": {
            "records": sorted(
                path.as_posix() for path in successful_output_paths
            ),
        },
        "timing": {
            "started_at": started_at,
            "finished_at": finished_at,
        },
        "status": {
            "total_processed": total_processed,
            "successful": successful_count,
            "failed": failed_count,
            "result": "success" if failed_count == 0 else "partial_failure",
        },
        "errors": manifest_errors,
    }
    if costs:
        manifest["costs"] = costs
    return manifest


def _build_source_context_map(
    input_path: Optional[Path],
    from_index: Optional[str],
) -> Dict[str, Dict[str, str]]:
    """Map each discovered document's filename to its origin URL + queried target."""
    index_path: Optional[Path] = None

    if from_index:
        candidate = Path(from_index)
        if candidate.is_file():
            index_path = candidate

    if index_path is None and input_path is not None:
        base = input_path if input_path.is_dir() else input_path.parent
        for up in (base, *base.parents):
            candidate = up / "download_index.csv"
            if candidate.is_file():
                index_path = candidate
                break
            if up.name == "discovered":
                break

    if index_path is None:
        return {}

    context_map: Dict[str, Dict[str, str]] = {}
    try:
        with index_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                path_val = row.get("path") or row.get("relative_path")
                if not path_val:
                    continue
                meta: Dict[str, Any] = {}
                meta_raw = row.get("target_metadata")
                if meta_raw:
                    try:
                        meta = json.loads(meta_raw)
                    except Exception:
                        meta = {}
                context_entry = {
                    "url": (
                        row.get("final_url") or row.get("url") or ""
                    ).strip(),
                    "site_name": str(
                        meta.get("site_name")
                        or row.get("target_label")
                        or ""
                    ),
                    "company_name": str(meta.get("company_name") or ""),
                    "jurisdiction": str(
                        meta.get("jurisdiction")
                        or row.get("source_jurisdiction", "").replace("-", " ").title()
                        or ""
                    ),
                    "city": str(meta.get("city") or ""),
                    "state": str(meta.get("state") or ""),
                }
                path_candidate = Path(path_val)
                key_candidates = {
                    path_candidate.as_posix(),
                    path_candidate.name,
                }
                rel_candidate = row.get("relative_path")
                if rel_candidate:
                    rel_path = Path(rel_candidate)
                    key_candidates.add(rel_path.as_posix())
                    key_candidates.add(rel_path.name)
                for key in key_candidates:
                    if key:
                        context_map[key] = context_entry
    except Exception:
        return {}
    return context_map


def _prepend_source_context(
    text: str,
    doc_path: Path,
    context_map: Dict[str, Dict[str, str]],
) -> str:
    """Prepend a CONTEXT block (source URL + queried site) to document text."""
    rel_cwd: Optional[str] = None
    try:
        rel_cwd = doc_path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        rel_cwd = None
    info = (
        context_map.get(doc_path.as_posix())
        or (context_map.get(rel_cwd) if rel_cwd else None)
        or context_map.get(doc_path.name)
        or context_map.get(str(doc_path))
    )
    if not info or not (info.get("url") or info.get("site_name")):
        return text

    location = ", ".join(
        part for part in (info.get("city"), info.get("state")) if part
    )
    lines = [
        "=== CONTEXT (added by ParseSweep; not part of the source document) ==="
    ]
    if info.get("url"):
        lines.append(f"SOURCE_URL: {info['url']}")
    if info.get("site_name"):
        lines.append(f"QUERIED_SITE: {info['site_name']}")
    if info.get("company_name"):
        lines.append(f"QUERIED_COMPANY: {info['company_name']}")
    # QUERIED_JURISDICTION is the authoritative municipality name from discovery.
    # Use it as ground truth so LLMs don't infer county names from document context.
    if info.get("jurisdiction"):
        lines.append(f"QUERIED_JURISDICTION: {info['jurisdiction']}")
    if location:
        lines.append(f"QUERIED_LOCATION: {location}")
    lines.append("=== END CONTEXT ===")
    lines.append("")
    return "\n".join(lines) + "\n" + text


# ---------------------------------------------------------------------------
# Public API aliases — the underscore-named functions above are the canonical
# implementations (kept verbatim from the former CLI module so behavior is
# byte-for-byte identical). These aliases give the core module a clean,
# importable surface for callers and tests.
# ---------------------------------------------------------------------------
generate_run_id = _generate_run_id
build_run_manifest = _build_run_manifest
build_source_context_map = _build_source_context_map
prepend_source_context = _prepend_source_context

__all__ = [
    "generate_run_id",
    "build_run_manifest",
    "build_source_context_map",
    "prepend_source_context",
]
