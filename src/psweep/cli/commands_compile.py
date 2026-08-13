"""`compile` command extracted from the legacy CLI monolith."""

from __future__ import annotations

import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from psweep.cli.ui import (
    Verbosity,
    console,
    get_verbosity,
    print_error,
    print_info,
    print_success,
    print_warning,
    with_status,
)
from psweep.compilation.data_compiler import DataCompiler
from psweep.config import RuntimeConfigError


def _build_dedup_preview_report(
    *,
    schema_info: Dict[str, Any],
    dedup_preview: Dict[str, Any],
    rows_before_dedup: int,
) -> Dict[str, Any]:
    """Build a stable dry-run report payload for machine-readable output."""
    return {
        "schema_type": schema_info["type"],
        "main_array_key": schema_info["main_array_key"],
        "rows_before_dedup": rows_before_dedup,
        "rows_after_dedup": rows_before_dedup - dedup_preview["duplicates_removed"],
        "duplicates_removed": dedup_preview["duplicates_removed"],
        "key_fields": dedup_preview["key_fields"],
        "compare_columns": dedup_preview["compare_columns"],
        "warnings": dedup_preview.get("warnings") or [],
        "suspicious_groups_count": dedup_preview.get("suspicious_groups_count", 0),
        "suspicious_groups_by_severity": dedup_preview.get(
            "suspicious_groups_by_severity"
        )
        or {
            "high": 0,
            "medium": 0,
            "low": 0,
        },
        "suspicious_groups": dedup_preview.get("suspicious_groups") or [],
        "duplicate_groups": dedup_preview["duplicate_groups"],
    }


def _should_fail_on_suspicious(
    preview_report: Dict[str, Any],
    threshold: str,
) -> bool:
    """Return whether suspicious-group counts meet or exceed the requested threshold."""
    if threshold == "none":
        return False

    severity_counts = preview_report.get("suspicious_groups_by_severity") or {}
    if threshold == "high":
        return severity_counts.get("high", 0) > 0
    if threshold == "medium":
        return (
            severity_counts.get("high", 0) > 0
            or severity_counts.get("medium", 0) > 0
        )
    if threshold == "low":
        return preview_report.get("suspicious_groups_count", 0) > 0

    return False


def _omit_none(d: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *d* with all keys whose value is None removed (one level)."""
    return {k: v for k, v in d.items() if v is not None}


def _round_cost(v: float) -> float:
    return round(v, 6)


def _round_secs(v: float) -> float:
    return round(v, 3)


def _round_mb(v: float) -> float:
    return round(v, 1)


def _has_any_files(path: Path) -> bool:
    """Return True when *path* contains at least one file."""
    if not path.exists() or not path.is_dir():
        return False
    return any(p.is_file() for p in path.rglob("*"))


def _discover_domain_roots(
    *,
    domain: str,
    extraction_dir: Path,
    discovery_input_dir: Optional[str],
) -> List[Path]:
    """Return candidate ``discovered/<domain>`` roots for accounting lookup."""
    roots: List[Path] = []
    seen: set[str] = set()

    def _add_root(candidate: Path) -> None:
        key = str(candidate)
        if key in seen:
            return
        seen.add(key)
        roots.append(candidate)

    for base in [Path.cwd(), extraction_dir.parent, extraction_dir.parent.parent]:
        _add_root(base / "discovered" / domain)

    if not discovery_input_dir:
        return roots

    disc_path = Path(discovery_input_dir)
    if disc_path.is_file():
        disc_path = disc_path.parent

    for parent in [disc_path, *disc_path.parents]:
        if parent.name == domain and parent.parent.name == "discovered":
            _add_root(parent)
            break
        if parent.name == "discovered":
            _add_root(parent / domain)
            break
    return roots


def _collect_discovery_manifests(
    *,
    domain: str,
    extraction_dir: Path,
    discovery_input_dir: Optional[str],
) -> List[Path]:
    """Collect discovery run manifests (deduplicated, oldest first)."""
    manifests: List[Path] = []
    seen: set[str] = set()

    def _add_manifest(path: Path) -> None:
        key = str(path)
        if key in seen or not path.exists():
            return
        seen.add(key)
        manifests.append(path)

    for root in _discover_domain_roots(
        domain=domain,
        extraction_dir=extraction_dir,
        discovery_input_dir=discovery_input_dir,
    ):
        runs_dir = root / "runs"
        if runs_dir.is_dir():
            for manifest in sorted(runs_dir.glob("*/manifest.json")):
                _add_manifest(manifest)
        latest_manifest = root / "latest" / "manifest.json"
        _add_manifest(latest_manifest)

    manifests.sort(key=lambda p: str(p))
    return manifests


def _discovery_domain_root_from_manifest(manifest_path: Path, domain: str) -> Optional[Path]:
    """Return discovered/<domain> root for known manifest layouts."""
    parent = manifest_path.parent
    if (
        parent.name == "latest"
        and parent.parent.name == domain
        and parent.parent.parent.name == "discovered"
    ):
        return parent.parent
    if (
        parent.parent.name == "runs"
        and parent.parent.parent.name == domain
        and parent.parent.parent.parent.name == "discovered"
    ):
        return parent.parent.parent
    return None


def _canonical_discovery_manifest_path(
    *,
    manifest_path: Path,
    domain: str,
    run_id: str,
) -> str:
    """Prefer discovered/<domain>/runs/<run_id>/manifest.json over latest symlink."""
    domain_root = _discovery_domain_root_from_manifest(manifest_path, domain)
    if domain_root is None:
        return str(manifest_path)
    candidate = domain_root / "runs" / run_id / "manifest.json"
    if candidate.exists():
        return str(candidate)
    return str(manifest_path)


def _build_pipeline_accounting(
    *,
    domain: str,
    extraction_dir: Path,
    output_dir: Path,
    compilation_stats: Dict[str, Any],
    discovery_input_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a unified run_accounting.json for the compile stage.

    The accounting file records WHAT HAPPENED in a run. It is a factual log,
    not a system manual: fields only appear when they carry information.

    Cost model
    ----------
    ``summary`` is the quick-read block (cost, volume, efficiency). The
    per-stage detail lives in ``stages.discovery``, ``stages.extraction``, and
    ``stages.compilation``. LLM costs (extraction + document-review) accumulate
    in ``summary.total_cost_usd``.

    Search API (SerpApi and future flat-rate providers) is tracked as query
    consumption only — it is NOT added to cost_usd because the plan is a
    monthly flat-rate subscription, not per-query billing. Query counts appear
    in the summary only when queries were actually made.

    Reruns
    ------
    Stage histories are cumulative across immutable run manifests:
    - discovery: discovered/<domain>/runs/*/manifest.json
    - extraction: extracted/<domain>/run_manifests/*.manifest.json
    - compilation: compiled/<domain>/run_manifests/*.manifest.json
    This keeps accounting stable across iterative workflows where users reuse,
    rerun, or partially reprocess stages.

    Scale notes
    -----------
    The summary block is designed to be the first thing a user reads: six
    fields cover cost, volume, and efficiency at any scale from a single
    laptop run to a 100k-site batch.
    """
    warnings: List[str] = []

    # Accumulators — built up as stages are processed, then frozen into summary.
    total_llm_cost: float = 0.0
    total_llm_calls: int = 0
    total_tokens: int = 0
    total_search_queries: int = 0
    search_api_provider: Optional[str] = None
    total_docs_extracted: int = 0
    total_extraction_runs: int = 0
    extraction_cost_total: float = 0.0
    discovery_completed_at: Optional[str] = None
    earliest_extraction_completed_at: Optional[str] = None

    stages: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Discovery stage (cumulative across all discovery run manifests)
    # ------------------------------------------------------------------
    discovery_runs: List[Dict[str, Any]] = []
    discovery_cost_acc: List[float] = []
    discovery_input_tokens = 0
    discovery_output_tokens = 0
    discovery_llm_calls = 0
    discovery_queries = 0
    discovery_candidates = 0
    discovery_downloaded = 0
    discovery_failed_downloads = 0
    discovery_total_downloads = 0
    discovery_providers: set[str] = set()
    discovered_run_ids_seen: set[str] = set()

    discovery_manifests = _collect_discovery_manifests(
        domain=domain,
        extraction_dir=extraction_dir,
        discovery_input_dir=discovery_input_dir,
    )
    for manifest_candidate in discovery_manifests:
        try:
            disc = json.loads(manifest_candidate.read_text(encoding="utf-8"))
            if not isinstance(disc, dict):
                raise ValueError("Discovery manifest is not a JSON object")

            run_id = str(disc.get("run_id") or manifest_candidate.parent.name)
            if run_id in discovered_run_ids_seen:
                continue
            discovered_run_ids_seen.add(run_id)

            timing = disc.get("timing") or {}
            stage_summaries = disc.get("stage_summaries") or {}
            seeker = stage_summaries.get("seeker") or {}
            review = stage_summaries.get("document_review") or {}
            review_costs = review.get("costs") or {}
            downloads_raw = stage_summaries.get("downloads") or {}
            acceptance = stage_summaries.get("acceptance_metrics") or {}
            udt = acceptance.get("unknown_target_discovery") or {}

            run_completed_at = timing.get("completed_at") or None
            run_elapsed = _round_secs(float(timing.get("elapsed_seconds") or 0.0))

            source_status = str(disc.get("status") or "scaffold")
            source_is_dry_run = source_status.startswith("scaffold_dry_run")

            rev_cost = _round_cost(float(review_costs.get("total_cost_usd") or 0.0))
            rev_input_tokens = int(review_costs.get("total_input_tokens") or 0)
            rev_output_tokens = int(review_costs.get("total_output_tokens") or 0)
            rev_calls = int(
                review_costs.get("model_calls")
                or review_costs.get("llm_calls")
                or 0
            )
            rev_total_tokens = rev_input_tokens + rev_output_tokens
            rev_reviewed = int(review_costs.get("documents_reviewed") or rev_calls or 0)

            seeker_provider = seeker.get("provider") or None
            seeker_queries = int(seeker.get("queries_executed") or 0)
            seeker_candidates = int(seeker.get("candidates_discovered") or 0)

            targets_total = udt.get("target_count")
            if targets_total is None and disc.get("input"):
                inp_targets = (disc.get("input") or {}).get("targets") or []
                if inp_targets:
                    targets_total = len(inp_targets)

            dl_downloaded_raw = downloads_raw.get("downloaded")
            dl_downloaded = (
                int(dl_downloaded_raw) if dl_downloaded_raw is not None else 0
            )
            dl_failed_raw = downloads_raw.get("failed")
            dl_failed = int(dl_failed_raw) if dl_failed_raw is not None else 0
            dl_total_raw = downloads_raw.get("total")
            dl_total = int(dl_total_raw) if dl_total_raw is not None else 0
            dl_bytes = downloads_raw.get("total_bytes")
            dl_mb = _round_mb(dl_bytes / 1_048_576) if dl_bytes else None

            disc_error_summary = disc.get("error_summary") or {}
            disc_errors_list = disc.get("errors") or []
            disc_error_total = int(disc_error_summary.get("total_errors") or 0)
            disc_error_codes = disc_error_summary.get("by_code") or {}

            has_activity = (
                seeker_queries > 0
                or seeker_candidates > 0
                or rev_calls > 0
                or rev_total_tokens > 0
                or rev_cost > 0
                or dl_downloaded > 0
                or dl_failed > 0
                or dl_total > 0
            )
            discovery_domain_root = _discovery_domain_root_from_manifest(
                manifest_candidate,
                domain,
            )
            has_reuse_evidence = bool(
                discovery_domain_root
                and _has_any_files(discovery_domain_root / "curated")
            )
            is_reused = (not has_activity) and has_reuse_evidence and not source_is_dry_run

            if source_status == "scaffold_dry_run":
                user_status = "dry_run"
            elif source_status == "scaffold_dry_run_with_errors":
                user_status = "dry_run_with_errors"
            elif is_reused:
                user_status = "reused"
            elif has_activity and disc_error_total > 0:
                user_status = "completed_with_errors"
            elif disc_error_total > 0:
                user_status = "incomplete"
            else:
                user_status = "completed"

            run_payload: Dict[str, Any] = {
                "run_id": run_id,
                "status": user_status,
                "completed_at": run_completed_at,
                "elapsed_seconds": run_elapsed if run_elapsed else None,
                "manifest_path": _canonical_discovery_manifest_path(
                    manifest_path=manifest_candidate,
                    domain=domain,
                    run_id=run_id,
                ),
            }
            if targets_total is not None:
                run_payload["targets_configured"] = targets_total

            if seeker_queries > 0 or seeker_candidates > 0:
                run_payload["search_api"] = _omit_none(
                    {
                        "provider": seeker_provider,
                        "queries_used": seeker_queries if seeker_queries else None,
                        "candidates_discovered": seeker_candidates if seeker_candidates else None,
                    }
                )

            if rev_calls > 0 or rev_total_tokens > 0 or rev_reviewed > 0 or rev_cost > 0:
                run_payload["document_review"] = _omit_none(
                    {
                        "documents_reviewed": rev_reviewed if rev_reviewed else None,
                        "llm_calls": rev_calls if rev_calls else None,
                        "input_tokens": rev_input_tokens if rev_input_tokens else None,
                        "output_tokens": rev_output_tokens if rev_output_tokens else None,
                        "tokens": rev_total_tokens if rev_total_tokens else None,
                        "cost_usd": rev_cost if rev_cost else None,
                    }
                )

            if dl_downloaded > 0 or dl_failed > 0 or dl_total > 0:
                run_payload["downloads"] = _omit_none(
                    {
                        "downloaded": dl_downloaded if dl_downloaded else None,
                        "failed": dl_failed if dl_failed else None,
                        "total": dl_total if dl_total else None,
                        "total_mb": dl_mb,
                    }
                )

            if disc_error_total > 0 and not is_reused:
                run_payload["errors"] = _omit_none(
                    {
                        "total": disc_error_total,
                        "by_code": disc_error_codes or None,
                        "messages": [
                            e.get("message")
                            for e in disc_errors_list[:3]
                            if isinstance(e, dict) and e.get("message")
                        ] or None,
                    }
                )

            discovery_runs.append(_omit_none(run_payload))

            discovery_cost_acc.append(rev_cost)
            discovery_input_tokens += rev_input_tokens
            discovery_output_tokens += rev_output_tokens
            discovery_llm_calls += rev_calls
            discovery_queries += seeker_queries
            discovery_candidates += seeker_candidates
            discovery_downloaded += dl_downloaded
            discovery_failed_downloads += dl_failed
            discovery_total_downloads += dl_total
            if seeker_provider:
                discovery_providers.add(str(seeker_provider))

        except (json.JSONDecodeError, ValueError, OSError, KeyError) as exc:
            warnings.append(
                f"discovery_manifest_parse_error: {manifest_candidate} — {exc}"
            )

    if discovery_runs:
        discovery_runs.sort(key=lambda run: run.get("completed_at") or "")
        latest_discovery = discovery_runs[-1]
        discovery_completed_at = latest_discovery.get("completed_at")
        stages["discovery"] = {
            "status": latest_discovery.get("status", "completed"),
            "run_count": len(discovery_runs),
            "aggregate": _omit_none(
                {
                    "metered_llm_cost_usd": _round_cost(math.fsum(discovery_cost_acc)),
                    "document_review_llm_calls": discovery_llm_calls or None,
                    "document_review_input_tokens": (
                        discovery_input_tokens if discovery_input_tokens else None
                    ),
                    "document_review_output_tokens": (
                        discovery_output_tokens if discovery_output_tokens else None
                    ),
                    "document_review_tokens": (
                        discovery_input_tokens + discovery_output_tokens
                        if (discovery_input_tokens + discovery_output_tokens) > 0
                        else None
                    ),
                    "search_api_queries": discovery_queries or None,
                    "candidates_discovered": discovery_candidates or None,
                    "documents_downloaded": discovery_downloaded or None,
                    "download_failures": discovery_failed_downloads or None,
                    "download_attempts": discovery_total_downloads or None,
                    "search_api_providers": (
                        sorted(discovery_providers) if discovery_providers else None
                    ),
                }
            ),
            "runs": discovery_runs,
        }
        total_llm_cost = _round_cost(
            math.fsum([total_llm_cost, math.fsum(discovery_cost_acc)])
        )
        total_llm_calls += discovery_llm_calls
        total_tokens += discovery_input_tokens + discovery_output_tokens
        total_search_queries += discovery_queries
        if discovery_providers:
            search_api_provider = ", ".join(sorted(discovery_providers))
    else:
        stages["discovery"] = {
            "status": "not_applicable",
            "input_source": "local_documents",
        }

    # ------------------------------------------------------------------
    # Extraction stage
    # ------------------------------------------------------------------
    # Primary source: run_manifests/*.manifest.json (one file per run/rerun).
    # Fallback: individual extraction record *.json files (legacy / no-config runs).
    run_manifest_dir = extraction_dir / "run_manifests"
    run_manifest_files = (
        sorted(run_manifest_dir.glob("*.manifest.json"))
        if run_manifest_dir.is_dir()
        else []
    )

    # Deduplicate manifests by run_id to prevent double-counting reruns.
    seen_run_ids: set[str] = set()

    if run_manifest_files:
        runs_data: List[Dict[str, Any]] = []
        ext_cost_acc: List[float] = []
        ext_input_tokens = 0
        ext_output_tokens = 0
        ext_docs_billed = 0
        providers: set[str] = set()
        models: set[str] = set()
        schema_ids: set[str] = set()
        run_completed_ats: List[str] = []

        for mf_path in run_manifest_files:
            try:
                mf = json.loads(mf_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                warnings.append(f"run_manifest_parse_error: {mf_path.name} — {exc}")
                continue
            if not isinstance(mf, dict):
                continue

            # Clean run_id: strip ".manifest" suffix left by mf_path.stem
            raw_stem = mf_path.stem  # e.g. "run_abc123.manifest"
            clean_stem = raw_stem.removesuffix(".manifest") if raw_stem.endswith(".manifest") else raw_stem
            run_id = mf.get("run_id") or clean_stem

            if run_id in seen_run_ids:
                warnings.append(f"duplicate_run_manifest_skipped: {mf_path.name}")
                continue
            seen_run_ids.add(run_id)

            costs = mf.get("costs") or {}
            lineage = mf.get("lineage") or {}
            run_timing = mf.get("timing") or {}

            run_cost = _round_cost(float(costs.get("total_cost_usd") or 0.0))
            run_input = int(costs.get("total_input_tokens") or 0)
            run_output = int(costs.get("total_output_tokens") or 0)
            run_docs = int(costs.get("documents_billed") or 0)
            run_model = lineage.get("model") or costs.get("model")
            run_provider = lineage.get("provider")
            run_schema = lineage.get("schema_id")
            run_completed = run_timing.get("finished_at") or run_timing.get("completed_at")
            run_started = run_timing.get("started_at")
            run_elapsed_raw = run_timing.get("elapsed_seconds")
            run_elapsed = _round_secs(float(run_elapsed_raw)) if run_elapsed_raw is not None else None

            ext_cost_acc.append(run_cost)
            ext_input_tokens += run_input
            ext_output_tokens += run_output
            ext_docs_billed += run_docs
            if run_provider:
                providers.add(str(run_provider))
            if run_model:
                models.add(str(run_model))
            if run_schema:
                schema_ids.add(str(run_schema))
            if run_completed:
                run_completed_ats.append(run_completed)

            run_record: Dict[str, Any] = {
                "run_id": run_id,
                "documents_billed": run_docs,
                "cost_usd": run_cost,
                "zero_cost_reason": (
                    None if run_cost > 0 else "provider reported zero billable cost"
                ),
                "cost_per_document_usd": (
                    _round_cost(run_cost / run_docs) if run_docs else 0.0
                ),
                "input_tokens": run_input,
                "output_tokens": run_output,
                "tokens": run_input + run_output,
                "elapsed_seconds": run_elapsed,
            }
            # Add optional fields
            if run_model:
                run_record["model"] = run_model
            if run_provider:
                run_record["provider"] = run_provider
            if run_schema:
                run_record["schema_id"] = run_schema
            if run_started:
                run_record["started_at"] = run_started
            if run_completed:
                run_record["completed_at"] = run_completed
            mode = mf.get("mode")
            if mode and mode != "single_model":
                run_record["mode"] = mode
            runs_data.append(run_record)

        # Sort runs by completed_at (chronological) — works even if some are None.
        runs_data.sort(key=lambda r: r.get("completed_at") or "")

        ext_cost = _round_cost(math.fsum(ext_cost_acc))
        ext_tokens = ext_input_tokens + ext_output_tokens

        # Track earliest extraction completed_at for stale-discovery warning.
        if run_completed_ats:
            earliest_extraction_completed_at = min(run_completed_ats)

        stage_payload: Dict[str, Any] = {
            "status": "completed",
            "cost_model": "per_document_llm_extraction",
            "documents_billed": ext_docs_billed,
            "cost_usd": ext_cost,
            "zero_cost_reason": (
                None if ext_cost > 0 else "no documents billed"
            ),
            "cost_per_document_usd": (
                _round_cost(ext_cost / ext_docs_billed) if ext_docs_billed else 0.0
            ),
            "input_tokens": ext_input_tokens,
            "output_tokens": ext_output_tokens,
            "tokens": ext_tokens,
            "llm_calls": ext_docs_billed,
            "extraction_runs": len(runs_data),
            "runs": runs_data,
        }
        # Add optional fields
        if providers:
            stage_payload["provider"] = list(sorted(providers)) if len(providers) > 1 else next(iter(providers), None)
        if models:
            stage_payload["model"] = list(sorted(models)) if len(models) > 1 else next(iter(models), None)
        if schema_ids:
            stage_payload["schema_id"] = list(sorted(schema_ids)) if len(schema_ids) > 1 else next(iter(schema_ids), None)
        stage_payload["records_dir"] = str(extraction_dir)
        if len(runs_data) > 1:
            stage_payload["note"] = (
                f"{len(runs_data)} extraction run(s) listed in runs[]; "
                "costs are cumulative — reruns incur additional LLM cost per document"
            )

        stages["extraction"] = stage_payload
        total_llm_cost = _round_cost(math.fsum([total_llm_cost, ext_cost]))
        total_llm_calls += ext_docs_billed
        total_tokens += ext_tokens
        total_docs_extracted = ext_docs_billed
        total_extraction_runs = len(runs_data)
        extraction_cost_total = ext_cost

    else:
        # Fallback: scan individual extraction JSON files.
        extraction_records = [
            p for p in extraction_dir.rglob("*.json")
            if "run_manifests" not in p.parts
        ]
        if extraction_records:
            doc_total = 0
            doc_success = 0
            ext_cost_acc2: List[float] = []
            ext_elapsed = 0.0
            ext_tokens = 0
            ext_calls = 0
            ext_input_tokens = 0
            ext_output_tokens = 0
            provider = model = None
            run_id = schema_id = artifact_id = None
            failed_examples: List[Dict[str, Any]] = []
            error_totals: Dict[str, int] = {}
            merged_fields_union: set[str] = set()
            input_chars_total = 0
            files_with_input_chars = 0

            for record_path in extraction_records:
                try:
                    rec = json.loads(record_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if not isinstance(rec, dict):
                    continue

                metrics = rec.get("processing_metrics") or {}
                lineage = rec.get("lineage") or {}
                quality = rec.get("quality") or {}
                document = rec.get("document") or {}

                if provider is None:
                    provider = lineage.get("provider")
                if model is None:
                    model = lineage.get("model")
                if run_id is None:
                    run_id = lineage.get("run_id")
                if schema_id is None:
                    schema_id = lineage.get("schema_id")
                if artifact_id is None:
                    artifact_id = lineage.get("artifact_id")

                input_chars = metrics.get("input_chars")
                if isinstance(input_chars, int):
                    input_chars_total += input_chars
                    files_with_input_chars += 1

                doc_total += 1
                file_cost = float(metrics.get("cost_usd") or 0.0)
                file_in_tok = int(metrics.get("input_tokens") or 0)
                file_out_tok = int(metrics.get("output_tokens") or 0)
                ext_cost_acc2.append(file_cost)
                ext_elapsed += float(metrics.get("duration_seconds") or 0.0)
                ext_input_tokens += file_in_tok
                ext_output_tokens += file_out_tok
                ext_tokens += file_in_tok + file_out_tok
                ext_calls += 1

                q_errors = quality.get("errors") or []
                if q_errors:
                    for err in q_errors:
                        code = (
                            err.get("code") if isinstance(err, dict) else None
                        ) or "unspecified_error"
                        error_totals[code] = error_totals.get(code, 0) + 1
                    if len(failed_examples) < 10:
                        failed_examples.append(
                            {
                                "record": str(record_path),
                                "source_document_id": document.get("source_document_id"),
                                "error_codes": list(
                                    {
                                        (
                                            e.get("code")
                                            if isinstance(e, dict)
                                            else "unspecified_error"
                                        )
                                        for e in q_errors
                                    }
                                ),
                            }
                        )
                else:
                    doc_success += 1

                notes = rec.get("notes")
                if isinstance(notes, str):
                    marker = "Merged duplicate from source row(s) with differing values in: "
                    if marker in notes:
                        merged_part = notes.split(marker, 1)[1].split(";", 1)[0]
                        for field_name in [
                            f.strip() for f in merged_part.split(",") if f.strip()
                        ]:
                            merged_fields_union.add(field_name)

            ext_cost_fallback = _round_cost(math.fsum(ext_cost_acc2))
            stage_payload_fb: Dict[str, Any] = _omit_none({
                "status": "completed",
                "records_dir": str(extraction_dir),
                "documents_total": doc_total,
                "documents_successful": doc_success,
                "documents_failed": max(doc_total - doc_success, 0) or None,
                "provider": provider,
                "model": model,
                "run_id": run_id,
                "schema_id": schema_id,
                "artifact_id": artifact_id,
                "cost_usd": ext_cost_fallback,
                "cost_per_document_usd": (
                    _round_cost(ext_cost_fallback / doc_total) if doc_total else None
                ),
                "input_tokens": ext_input_tokens,
                "output_tokens": ext_output_tokens,
                "elapsed_seconds": _round_secs(ext_elapsed),
                "llm_calls": ext_calls,
                "tokens": ext_tokens,
                "avg_input_chars": (
                    round(input_chars_total / files_with_input_chars)
                    if files_with_input_chars
                    else None
                ),
            })
            if error_totals:
                stage_payload_fb["error_summary"] = error_totals
            if failed_examples:
                stage_payload_fb["failed_examples"] = failed_examples
            if merged_fields_union:
                stage_payload_fb["merged_conflict_fields"] = sorted(merged_fields_union)

            stages["extraction"] = stage_payload_fb
            total_llm_cost = _round_cost(math.fsum([total_llm_cost, ext_cost_fallback]))
            total_llm_calls += ext_calls
            total_tokens += ext_tokens
            total_docs_extracted = doc_total
            extraction_cost_total = ext_cost_fallback

    # ------------------------------------------------------------------
    # Stale discovery warning
    # ------------------------------------------------------------------
    if (
        discovery_completed_at
        and earliest_extraction_completed_at
        and stages.get("discovery")
    ):
        try:
            disc_dt = datetime.fromisoformat(
                discovery_completed_at.replace("Z", "+00:00")
            )
            ext_dt = datetime.fromisoformat(
                earliest_extraction_completed_at.replace("Z", "+00:00")
            )
            age_hours = (ext_dt - disc_dt).total_seconds() / 3600
            if age_hours > 24:
                warnings.append(
                    f"stale_discovery: discovery completed {age_hours:.0f}h before "
                    "earliest extraction run — document availability may have changed"
                )
        except (ValueError, TypeError):
            pass

    # ------------------------------------------------------------------
    # Compilation stage (cumulative across compilation run manifests)
    # ------------------------------------------------------------------
    compilation_runs: List[Dict[str, Any]] = []
    compilation_cost_acc: List[float] = []
    compilation_llm_calls = 0
    compilation_input_tokens = 0
    compilation_output_tokens = 0
    compile_manifest_dir = output_dir / "run_manifests"
    compile_manifest_files = (
        sorted(compile_manifest_dir.glob("*.manifest.json"))
        if compile_manifest_dir.is_dir()
        else []
    )
    seen_compile_run_ids: set[str] = set()
    for mf_path in compile_manifest_files:
        try:
            mf = json.loads(mf_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            warnings.append(f"compile_manifest_parse_error: {mf_path.name} — {exc}")
            continue
        if not isinstance(mf, dict):
            continue

        raw_stem = mf_path.stem
        clean_stem = (
            raw_stem.removesuffix(".manifest")
            if raw_stem.endswith(".manifest")
            else raw_stem
        )
        run_id = str(mf.get("run_id") or clean_stem)
        if run_id in seen_compile_run_ids:
            continue
        seen_compile_run_ids.add(run_id)

        costs = mf.get("costs") or {}
        lineage = mf.get("lineage") or {}
        run_timing = mf.get("timing") or {}
        outputs = mf.get("outputs") or {}
        status_block = mf.get("status") or {}
        run_cost = _round_cost(float(costs.get("total_cost_usd") or 0.0))
        run_input = int(costs.get("total_input_tokens") or 0)
        run_output = int(costs.get("total_output_tokens") or 0)
        run_calls = int(costs.get("llm_calls") or 0)
        run_record: Dict[str, Any] = _omit_none(
            {
                "run_id": run_id,
                "status": (
                    status_block.get("result")
                    if isinstance(status_block, dict)
                    else status_block
                )
                or "completed",
                "mode": mf.get("mode"),
                "cost_usd": run_cost if run_cost else None,
                "llm_calls": run_calls if run_calls else None,
                "input_tokens": run_input if run_input else None,
                "output_tokens": run_output if run_output else None,
                "tokens": (run_input + run_output) if (run_input + run_output) else None,
                "model": lineage.get("model"),
                "provider": lineage.get("provider"),
                "schema_id": lineage.get("schema_id"),
                "started_at": run_timing.get("started_at"),
                "completed_at": (
                    run_timing.get("finished_at")
                    or run_timing.get("completed_at")
                ),
                "records": outputs.get("records"),
                "columns": outputs.get("columns"),
                "output_formats": outputs.get("output_formats"),
            }
        )
        compilation_runs.append(run_record)
        compilation_cost_acc.append(run_cost)
        compilation_llm_calls += run_calls
        compilation_input_tokens += run_input
        compilation_output_tokens += run_output

    stages["compilation"] = {
        "status": "completed",
        "output_dir": str(output_dir),
        **compilation_stats,
    }
    if compilation_runs:
        compilation_runs.sort(key=lambda run: run.get("completed_at") or "")
        stages["compilation"]["run_count"] = len(compilation_runs)
        stages["compilation"]["runs"] = compilation_runs
        stages["compilation"]["aggregate"] = _omit_none(
            {
                "metered_llm_cost_usd": _round_cost(math.fsum(compilation_cost_acc)),
                "llm_calls": compilation_llm_calls or None,
                "input_tokens": (
                    compilation_input_tokens if compilation_input_tokens else None
                ),
                "output_tokens": (
                    compilation_output_tokens if compilation_output_tokens else None
                ),
                "tokens": (
                    compilation_input_tokens + compilation_output_tokens
                    if (compilation_input_tokens + compilation_output_tokens) > 0
                    else None
                ),
            }
        )
        total_llm_cost = _round_cost(
            math.fsum([total_llm_cost, math.fsum(compilation_cost_acc)])
        )
        total_llm_calls += compilation_llm_calls
        total_tokens += compilation_input_tokens + compilation_output_tokens

    # ------------------------------------------------------------------
    # Summary block — designed for quick first-read at any scale
    # ------------------------------------------------------------------
    summary: Dict[str, Any] = {
        "total_cost_usd": total_llm_cost,
        "cost_scope": "metered_llm_only",
        "documents_extracted": total_docs_extracted,
        "cost_per_document_usd": (
            _round_cost(extraction_cost_total / total_docs_extracted)
            if total_docs_extracted
            else 0.0
        ),
        "llm_calls_total": total_llm_calls,
        "tokens_total": total_tokens,
        "extraction_runs": total_extraction_runs,
    }
    # Only include conditional fields when they carry information:
    if total_llm_cost == 0:
        summary["zero_cost_reason"] = "no LLM-billable operations in this run"
    if total_search_queries > 0:
        summary["search_api_queries"] = total_search_queries
    if search_api_provider:
        summary["search_api_provider"] = search_api_provider

    # Extract domain schema version from the schema file
    domain_schema_version: Optional[str] = None
    _schema_id = stages.get("extraction", {}).get("schema_id")
    if _schema_id and isinstance(_schema_id, str):
        try:
            _schema_path = Path(_schema_id)
            if not _schema_path.is_absolute():
                _schema_path = Path.cwd() / _schema_id
            if _schema_path.exists():
                _schema_data = json.loads(_schema_path.read_text(encoding="utf-8"))
                domain_schema_version = (_schema_data.get("$metadata") or {}).get("version")
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    # ------------------------------------------------------------------
    # Final structure
    # ------------------------------------------------------------------
    output: Dict[str, Any] = {
        "accounting_version": "2.0.0",
        "domain_schema_version": domain_schema_version,
        "domain": domain,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "summary": summary,
        "stages": stages,
    }

    if warnings:
        output["warnings"] = warnings

    return output


def _resolve_compilation_output_formats(
    metadata_overrides: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Resolve which compilation outputs to emit."""
    output_config = ((metadata_overrides or {}).get("compilation") or {}).get("output") or {}
    requested_format = output_config.get("default_format")

    if requested_format is None:
        return ["csv", "excel"]

    normalized_format = str(requested_format).strip().lower()
    if normalized_format == "excel":
        return ["excel"]
    if normalized_format == "csv":
        return ["csv"]
    if normalized_format in {"both", "all"}:
        return ["csv", "excel"]

    logging.getLogger(__name__).warning(
        "Unsupported compilation output format '%s'; falling back to csv+excel",
        requested_format,
    )
    return ["csv", "excel"]


def _write_compilation_run_manifest(
    *,
    output_dir: Path,
    schema_id: str,
    synthesis_active: bool,
    synthesis_model: Optional[str],
    synthesis_provider: Optional[str],
    synthesis_llm_calls: int,
    records: int,
    columns: int,
    duplicates_removed: int,
    output_formats: List[str],
    started_at: datetime,
) -> Optional[Path]:
    """Persist one immutable compilation run manifest for cumulative accounting."""
    run_manifest_dir = output_dir / "run_manifests"
    run_manifest_dir.mkdir(parents=True, exist_ok=True)

    completed_at = datetime.now(timezone.utc)
    run_id = f"compile://{completed_at.strftime('%Y%m%dT%H%M%S%fZ')}"
    manifest_path = (
        run_manifest_dir
        / f"{completed_at.strftime('%Y%m%dT%H%M%S%fZ')}.manifest.json"
    )
    manifest = {
        "manifest_version": "1.0.0",
        "run_id": run_id,
        "mode": "synthesis" if synthesis_active else "deterministic",
        "status": {
            "result": "success",
        },
        "timing": {
            "started_at": started_at.isoformat().replace("+00:00", "Z"),
            "finished_at": completed_at.isoformat().replace("+00:00", "Z"),
            "elapsed_seconds": _round_secs((completed_at - started_at).total_seconds()),
        },
        "lineage": {
            "schema_id": schema_id,
            "model": synthesis_model,
            "provider": synthesis_provider,
        },
        "costs": {
            "total_cost_usd": 0.0,
            "llm_calls": synthesis_llm_calls,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        },
        "outputs": {
            "records": records,
            "columns": columns,
            "duplicates_removed": duplicates_removed,
            "output_formats": output_formats,
        },
    }
    try:
        manifest_path.write_text(
            json.dumps(_omit_none(manifest), indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest_path
    except OSError:
        return None


@click.command()
@click.argument("extracted_dir", type=click.Path(exists=True), required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Domain config file (RECOMMENDED — includes schema, page targeting, dedup)",
)
@click.option(
    "--show-effective-config",
    is_flag=True,
    help="Print resolved command inputs with source attribution and continue",
)
@click.option(
    "--validate-config",
    "validate_config_only",
    is_flag=True,
    help="Validate resolved command inputs and exit without compiling",
)
@click.option(
    "--config-strict",
    is_flag=True,
    help="Fail on unknown keys in runtime config sections",
)
@click.option(
    "--schema",
    "-s",
    type=click.Path(exists=True),
    required=False,
    help="Schema file (for quick testing without a config YAML)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output directory (auto-detected if not specified)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview compilation and deduplication without writing output files",
)
@click.option(
    "--report-format",
    type=click.Choice(["text", "json"], case_sensitive=False),
    default="text",
    show_default=True,
    help="Dry-run preview output format",
)
@click.option(
    "--fail-on-suspicious",
    type=click.Choice(["none", "high", "medium", "low"], case_sensitive=False),
    default="none",
    show_default=True,
    help="With --dry-run, exit non-zero when suspicious duplicate groups meet this severity threshold",
)
@click.option("--quiet", "-q", is_flag=True, help="Minimal output (machine-readable)")
@click.option("--verbose", "-v", is_flag=True, help="Detailed output with statistics")
@click.option("--debug", is_flag=True, help="Debug mode with full logs")
def compile(
    extracted_dir: Optional[str],
    config_path: Optional[str],
    show_effective_config: bool,
    validate_config_only: bool,
    config_strict: bool,
    schema: Optional[str],
    output: Optional[str],
    dry_run: bool,
    report_format: str,
    fail_on_suspicious: str,
    quiet: bool,
    verbose: bool,
    debug: bool,
) -> None:
    """Compile extracted JSON files into clean Excel/CSV output.

    Reads per-document JSON files from EXTRACTED_DIR (or the configured
    input_dir), deduplicates records using the schema's identity rules, and
    writes a consolidated Excel and/or CSV file to the output directory.

    Run ``psweep compile --help`` for every option and its default.

    Examples
    --------
    ::

        psweep compile extracted/my_domain/ --schema schemas/my_schema.json
        psweep compile --config config/my_domain/my_domain.yaml
        psweep compile extracted/ --schema s.json --dry-run --verbose
    """
    from psweep.cli.commands import (
        _explicit_cli_overrides,
        _print_effective_config,
        _resolve_runtime_command_inputs,
        begin_run,
    )

    view = begin_run("compile", quiet=quiet, verbose=verbose, debug=debug)

    cli_overrides = _explicit_cli_overrides(
        [
            "extracted_dir",
            "schema",
            "output",
            "dry_run",
            "report_format",
            "fail_on_suspicious",
        ]
    )

    try:
        resolved_inputs = _resolve_runtime_command_inputs(
            command_name="compile",
            config_path=config_path,
            strict=config_strict,
            cli_values=cli_overrides,
        )
    except RuntimeConfigError as exc:
        view.error("Runtime config resolution failed", str(exc))
        sys.exit(1)

    warnings = resolved_inputs.get("_config_warnings", [])
    view.warnings(warnings)

    if show_effective_config and not get_verbosity().is_quiet:
        _print_effective_config("compile", resolved_inputs)
        console.print()

    if validate_config_only:
        if get_verbosity().is_quiet:
            click.echo(
                json.dumps(
                    {
                        "command": "compile",
                        "status": "valid",
                        "resolved": {
                            key: value
                            for key, value in resolved_inputs.items()
                            if not key.startswith("_")
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print_success("Runtime config validation passed for compile command")
        return

    extracted_dir = resolved_inputs["extracted_dir"]
    schema = resolved_inputs["schema"]
    output = resolved_inputs.get("output", output)
    dry_run = resolved_inputs.get("dry_run", dry_run)
    report_format = resolved_inputs.get("report_format", report_format)
    fail_on_suspicious = resolved_inputs.get(
        "fail_on_suspicious", fail_on_suspicious
    )
    synthesis_cfg = resolved_inputs.get("synthesis")
    synthesis_active = isinstance(synthesis_cfg, dict) and bool(
        synthesis_cfg.get("enabled")
    )

    input_dir = Path(extracted_dir)
    if not input_dir.exists():
        print_error("Input directory not found", input_dir.as_posix())
        sys.exit(1)

    emit_json_report = dry_run and report_format.lower() == "json"

    if fail_on_suspicious != "none" and not dry_run:
        print_error(
            "Invalid option combination",
            "--fail-on-suspicious only applies with --dry-run",
        )
        sys.exit(1)

    if output:
        output_dir = Path(output)
    else:
        parts = list(input_dir.parts)
        if "extracted" in parts:
            index = parts.index("extracted")
            parts[index] = "compiled"
            output_dir = Path(*parts)
        else:
            output_dir = Path.cwd() / "compiled" / input_dir.name

    output_dir.mkdir(parents=True, exist_ok=True)

    if not emit_json_report:
        view.header("COMPILATION")

    matched_schema = Path(schema)
    if not matched_schema.exists():
        print_error("Schema not found", matched_schema.as_posix())
        sys.exit(1)

    try:
        from psweep.utils.schema_metadata import SchemaMetadata

        metadata_overrides: Dict[str, Any] = {}
        compilation_overrides: Dict[str, Any] = {}

        config_output = resolved_inputs.get("compilation_output")
        if isinstance(config_output, dict):
            compilation_overrides["output"] = config_output

        config_norm = resolved_inputs.get("normalization")
        if isinstance(config_norm, dict):
            compilation_overrides["normalization"] = config_norm

        if compilation_overrides:
            metadata_overrides["compilation"] = compilation_overrides

        schema_metadata = SchemaMetadata(
            matched_schema,
            metadata_overrides=metadata_overrides or None,
        )

        if not emit_json_report and not view.is_quiet:
            try:
                schema_display = str(matched_schema.relative_to(Path.cwd()))
            except ValueError:
                schema_display = str(matched_schema)
            config_info = {
                "Input": str(input_dir),
                "Output": str(output_dir),
                "Schema": schema_display,
            }
            if metadata_overrides:
                config_info["Overrides"] = "config-owned compilation settings active"
            view.config(config_info)
    except Exception as exc:
        print_error(
            "Schema validation failed",
            f"Schema {matched_schema.name} is missing required $metadata section: {exc}\n"
            "ParseSweep v2.0+ requires schemas with $metadata.\n"
            "See schemas/SCHEMA_BEST_PRACTICES.md for examples.",
        )
        return

    if not emit_json_report:
        view.phase("Analyzing schema structure")
    compiler = DataCompiler(
        schema_metadata=schema_metadata,
        verbose=view.verbosity.shows_detail,
        debug=view.verbosity is Verbosity.DEBUG,
    )
    synth_client = None
    synthesizer = None
    compile_started_at = datetime.now(timezone.utc)

    try:
        if synthesis_active:
            from psweep.compilation.synthesizer import Synthesizer
            from psweep.extraction.llm_factory import build_llm_client

            synth_model = synthesis_cfg.get("model")
            if not synth_model and not resolved_inputs.get("models") and not config.llm_config.get("model"):
                raise click.UsageError(
                    "No model configured for the compilation synthesis stage. "
                    "Add 'model: primary' under 'compilation.synthesis:' "
                    "in your run config, or set AZURE_OPENAI_MODEL in your .env file."
                )
            synth_client = build_llm_client(
                synth_model,
                models=resolved_inputs.get("models"),
            )
            if not emit_json_report:
                view.phase(
                    "Synthesizing one record per entity "
                    f"(model={synth_client.raw_model})"
                )
            synthesizer = Synthesizer(
                schema_metadata=schema_metadata,
                config=synthesis_cfg,
                llm_client=synth_client,
                verbose=view.verbosity.shows_detail,
            )
            quiet = emit_json_report or view.is_quiet
            with with_status(
                f"Synthesizing with {synth_client.raw_model}...", quiet=quiet
            ):
                df = synthesizer.synthesize_from_directory(input_dir)
            if not emit_json_report:
                total_rows = synthesizer.llm_calls + synthesizer.deterministic_rows
                view.status(
                    "info",
                    f"Synthesis: {synthesizer.llm_calls} LLM reconciliation "
                    f"call(s), {synthesizer.deterministic_rows} resolved "
                    f"deterministically (no API) of {total_rows} entities",
                )
            schema_info = {
                "type": f"Synthesized: {schema_metadata.get_main_data_array()}",
                "main_array_key": (synthesis_cfg.get("group_by") or ["entity"])[0],
            }
        else:
            quiet = emit_json_report or view.is_quiet
            if not quiet:
                _json_files = list(input_dir.glob("*.json"))
                view.phase(
                    f"Found {len(_json_files)} extracted JSON file(s) in {input_dir.name}"
                )
            with with_status("Loading extracted JSON files...", quiet=quiet):
                df, schema_info = compiler.compile_from_directory(
                    input_dir,
                    apply_deduplication=not dry_run,
                )

        if df.empty:
            print_warning("No data found to compile")
            return

        if not emit_json_report:
            view.success(f"Schema detected: {schema_info['type']}")
            view.status("info", f"Main entity: {schema_info['main_array_key']}")

        if dry_run and synthesis_active:
            print_info(
                f"Dry run: synthesized {len(df)} entity row(s); no files written."
            )
            return

        if dry_run:
            dedup_preview = compiler.deduplicator.preview_deduplication(df)
            preview_report = _build_dedup_preview_report(
                schema_info=schema_info,
                dedup_preview=dedup_preview,
                rows_before_dedup=len(df),
            )
            preview_report["fail_on_suspicious"] = fail_on_suspicious
            preview_report["would_fail_on_suspicious"] = _should_fail_on_suspicious(
                preview_report,
                fail_on_suspicious,
            )

            if emit_json_report or view.is_quiet:
                click.echo(json.dumps(preview_report, indent=2, sort_keys=True))
            else:
                view.phase("Previewing deduplication")
                preview_stats = {
                    "Schema Type": preview_report["schema_type"],
                    "Rows Before Dedup": str(preview_report["rows_before_dedup"]),
                    "Rows After Dedup": str(preview_report["rows_after_dedup"]),
                    "Duplicates Removed": str(preview_report["duplicates_removed"]),
                    "Suspicious Groups": str(preview_report["suspicious_groups_count"]),
                    "Severity Mix": ", ".join(
                        f"{severity}={count}"
                        for severity, count in preview_report[
                            "suspicious_groups_by_severity"
                        ].items()
                        if count > 0
                    )
                    or "none",
                    "Fail Threshold": preview_report["fail_on_suspicious"],
                    "Key Fields": ", ".join(preview_report["key_fields"]) or "none",
                    "Compare Columns": ", ".join(preview_report["compare_columns"])
                    or "none",
                }
                view.summary(preview_stats, title="Deduplication Preview")

                for warning in preview_report["warnings"]:
                    view.warning(warning)

                for index, group in enumerate(
                    preview_report["duplicate_groups"][:5],
                    start=1,
                ):
                    sample_values = (
                        ", ".join(
                            f"{field}={value}"
                            for field, value in group["sample_values"].items()
                            if value not in (None, "")
                        )
                        or "no populated key values"
                    )
                    group_label = f"Group {index}"
                    if group.get("suspicious"):
                        group_label += f" (suspicious:{group.get('severity', 'low')})"
                    view.status(
                        "warning" if group.get("suspicious") else "info",
                        f"{group_label}: keep row {group['keep_index']}, "
                        f"drop {group['drop_indices']}",
                        sample_values,
                    )
                    view.status("info", group["note"])
                    if group.get("conflicting_columns"):
                        view.status(
                            "warning",
                            f"Conflicts: {', '.join(group['conflicting_columns'])}",
                        )

                if len(preview_report["duplicate_groups"]) > 5:
                    view.status(
                        "info",
                        f"... {len(preview_report['duplicate_groups']) - 5} "
                        "more duplicate group(s) omitted",
                    )

                view.info("Dry run complete - no CSV/Excel files were written")

            if preview_report["would_fail_on_suspicious"]:
                if not emit_json_report and not view.is_quiet:
                    view.error(
                        "Suspicious deduplication threshold exceeded",
                        "Dry-run found suspicious groups at or above "
                        f"'{fail_on_suspicious}' severity",
                    )
                sys.exit(2)
            return

        view.phase("Creating outputs")
        base_name = input_dir.name.replace("_", "-")
        output_formats = _resolve_compilation_output_formats(metadata_overrides or None)
        emitted_paths: List[Path] = []

        if "csv" in output_formats:
            csv_path = output_dir / f"{base_name}.csv"
            compiler.save_csv(df, csv_path)
            emitted_paths.append(csv_path)
            csv_size_mb = csv_path.stat().st_size / (1024 * 1024)

            if view.verbosity.shows_detail:
                view.success(
                    f"CSV saved: {csv_path.name} ({csv_size_mb:.2f} MB, {len(df)} rows)"
                )
            else:
                view.success(f"CSV saved ({len(df)} rows)")

        if "excel" in output_formats:
            excel_path = output_dir / f"{base_name}.xlsx"
            compiler.save_excel(df, excel_path)
            emitted_paths.append(excel_path)
            excel_size_mb = excel_path.stat().st_size / (1024 * 1024)

            if view.verbosity.shows_detail:
                view.success(
                    f"Excel saved: {excel_path.name} ({excel_size_mb:.2f} MB, {len(df)} rows)"
                )
            else:
                view.success("Excel saved (clean formatting, auto-sized columns)")

        if not get_verbosity().is_quiet:
            summary_stats = {
                "Schema Type": schema_info["type"],
                "Records": str(len(df)),
                "Columns": str(len(df.columns)),
                "Outputs": ", ".join(output_formats),
            }
            if not synthesis_active and compiler.duplicates_removed > 0:
                summary_stats["Duplicates Removed"] = str(compiler.duplicates_removed)
            if synthesis_active:
                summary_stats["Model"] = synth_client.raw_model

            if schema_info.get("category_field"):
                from psweep.utils.normalizers import camel_to_title

                category_display = camel_to_title(
                    schema_info.get("category_field", "")
                )
                if category_display and category_display in df.columns:
                    top_categories = df[category_display].value_counts().head(5)
                    if not top_categories.empty:
                        top_cat_str = ", ".join(
                            [
                                f"{cat} ({count})"
                                for cat, count in list(top_categories.items())[:3]
                            ]
                        )
                        summary_stats[f"Top {category_display}s"] = top_cat_str

            view.summary(summary_stats, title="Compilation Summary")
            view.outputs(
                {
                    (path.suffix.lstrip(".").upper() or "File"): str(path.absolute())
                    for path in emitted_paths
                }
            )
            view.next_steps(["Open the CSV/Excel to review the compiled dataset"])

        # ------------------------------------------------------------------
        # Run accounting — always written regardless of verbosity / quiet mode.
        # This is a structured record of costs, not presentation output.
        # ------------------------------------------------------------------
        _summary_stats_for_acct = {
            "records": len(df),
            "columns": len(df.columns),
            "duplicates_removed": compiler.duplicates_removed,
            "output_format": ", ".join(output_formats),
        }
        _write_compilation_run_manifest(
            output_dir=output_dir,
            schema_id=str(matched_schema),
            synthesis_active=synthesis_active,
            synthesis_model=(getattr(synth_client, "raw_model", None) if synth_client else None),
            synthesis_provider=(getattr(synth_client, "provider", None) if synth_client else None),
            synthesis_llm_calls=(int(getattr(synthesizer, "llm_calls", 0)) if synthesizer else 0),
            records=len(df),
            columns=len(df.columns),
            duplicates_removed=compiler.duplicates_removed,
            output_formats=output_formats,
            started_at=compile_started_at,
        )

        acct_path = output_dir / "run_accounting.json"
        try:
            accounting = _build_pipeline_accounting(
                domain=resolved_inputs.get("domain", input_dir.name),
                extraction_dir=input_dir,
                output_dir=output_dir,
                compilation_stats=_summary_stats_for_acct,
                discovery_input_dir=resolved_inputs.get("_extraction_input_dir"),
            )
            acct_path.write_text(
                json.dumps(accounting, indent=2) + "\n",
                encoding="utf-8",
            )
            if not get_verbosity().is_quiet:
                view.outputs({"Accounting": str(acct_path.absolute())})
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning(
                "run_accounting.json could not be written: %s", exc
            )

        if get_verbosity().is_quiet:
            for emitted_path in emitted_paths:
                console.print(str(emitted_path.absolute()))

    except Exception as exc:
        view.error("Compilation failed", str(exc))
        if view.verbosity is Verbosity.DEBUG:
            import traceback

            traceback.print_exc()
        sys.exit(1)
