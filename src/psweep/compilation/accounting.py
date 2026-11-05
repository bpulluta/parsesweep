"""Pipeline run accounting for the compile stage.

This module builds the unified ``run_accounting.json`` payload and the
per-run compilation manifest. The logic is a factual log of what happened
across the discover -> extract -> compile pipeline, gathered from immutable
run manifests. It lives here (rather than in the thin CLI layer) so it can be
built and tested without invoking the ``compile`` Click command.

Public API: :func:`build_pipeline_accounting`,
:func:`write_compilation_run_manifest`, :func:`latest_compilation_manifest`,
:func:`collect_discovery_manifests`.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from psweep.workspace import Workspace


def _omit_none(d: dict[str, Any]) -> dict[str, Any]:
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


def collect_discovery_manifests(domain: str) -> list[Path]:
    """Discovery run manifests for *domain* plus the ``latest`` pointer, sorted by path."""
    workspace = Workspace(domain)
    manifests = list((workspace.discovered / "runs").glob("*/manifest.json"))
    latest = workspace.latest / "manifest.json"
    if latest.exists():
        manifests.append(latest)
    return sorted(manifests, key=str)


def canonical_discovery_manifest_path(domain: str, manifest_path: Path, run_id: str) -> str:
    """Prefer ``runs/<run_id>/manifest.json`` over the ``latest`` pointer."""
    candidate = Workspace(domain).discovered / "runs" / run_id / "manifest.json"
    return str(candidate if candidate.exists() else manifest_path)


def build_pipeline_accounting(
    *,
    domain: str,
    extraction_dir: Path,
    output_dir: Path,
    compilation_stats: dict[str, Any],
) -> dict[str, Any]:
    """Build run_accounting.json: a factual, cumulative log of discover, extract and compile.

    Histories accumulate across immutable run manifests (discovery runs, plus
    ``run_manifests/`` under the extraction and compilation dirs). Discovery and
    extraction LLM costs sum into ``summary.total_cost_usd``; compilation makes no
    LLM calls and records output stats only; flat-rate search APIs count queries only.
    """
    warnings: list[str] = []

    # Accumulators — built up as stages are processed, then frozen into summary.
    total_llm_cost: float = 0.0
    total_llm_calls: int = 0
    total_tokens: int = 0
    total_search_queries: int = 0
    search_api_provider: str | None = None
    total_docs_extracted: int = 0
    total_extraction_runs: int = 0
    extraction_cost_total: float = 0.0
    discovery_completed_at: str | None = None
    earliest_extraction_completed_at: str | None = None

    stages: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Discovery stage (cumulative across all discovery run manifests)
    # ------------------------------------------------------------------
    discovery_runs: list[dict[str, Any]] = []
    discovery_cost_acc: list[float] = []
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

    discovery_manifests = collect_discovery_manifests(domain)
    has_reuse_evidence = _has_any_files(Workspace(domain).curated)
    for manifest_candidate in discovery_manifests:
        try:
            disc = json.loads(manifest_candidate.read_text(encoding="utf-8"))
            if not isinstance(disc, dict):
                raise ValueError(  # noqa: TRY004 - callers handle ValueError
                    "Discovery manifest is not a JSON object"
                )

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

            source_status = str(disc.get("status") or "failed")
            source_is_dry_run = bool((disc.get("input") or {}).get("dry_run"))

            rev_cost = _round_cost(float(review_costs.get("total_cost_usd") or 0.0))
            rev_input_tokens = int(review_costs.get("total_input_tokens") or 0)
            rev_output_tokens = int(review_costs.get("total_output_tokens") or 0)
            rev_calls = int(review_costs.get("model_calls") or review_costs.get("llm_calls") or 0)
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
            dl_downloaded = int(dl_downloaded_raw) if dl_downloaded_raw is not None else 0
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
            is_reused = (not has_activity) and has_reuse_evidence and not source_is_dry_run

            if source_is_dry_run:
                user_status = "dry_run_with_errors" if disc_error_total else "dry_run"
            elif source_status == "empty":
                user_status = "empty"
            elif source_status == "reused" or is_reused:
                user_status = "reused"
            elif has_activity and disc_error_total > 0:
                user_status = "completed_with_errors"
            elif disc_error_total > 0:
                user_status = "incomplete"
            else:
                user_status = "completed"

            run_payload: dict[str, Any] = {
                "run_id": run_id,
                "status": user_status,
                "completed_at": run_completed_at,
                "elapsed_seconds": run_elapsed or None,
                "manifest_path": canonical_discovery_manifest_path(
                    domain, manifest_candidate, run_id
                ),
            }
            if targets_total is not None:
                run_payload["targets_configured"] = targets_total

            if seeker_queries > 0 or seeker_candidates > 0:
                run_payload["search_api"] = _omit_none(
                    {
                        "provider": seeker_provider,
                        "queries_used": seeker_queries or None,
                        "candidates_discovered": seeker_candidates or None,
                    }
                )

            if rev_calls > 0 or rev_total_tokens > 0 or rev_reviewed > 0 or rev_cost > 0:
                run_payload["document_review"] = _omit_none(
                    {
                        "documents_reviewed": rev_reviewed or None,
                        "llm_calls": rev_calls or None,
                        "input_tokens": rev_input_tokens or None,
                        "output_tokens": rev_output_tokens or None,
                        "tokens": rev_total_tokens or None,
                        "cost_usd": rev_cost or None,
                    }
                )

            if dl_downloaded > 0 or dl_failed > 0 or dl_total > 0:
                run_payload["downloads"] = _omit_none(
                    {
                        "downloaded": dl_downloaded or None,
                        "failed": dl_failed or None,
                        "total": dl_total or None,
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
                        ]
                        or None,
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
            warnings.append(f"discovery_manifest_parse_error: {manifest_candidate} — {exc}")

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
                    "document_review_input_tokens": (discovery_input_tokens or None),
                    "document_review_output_tokens": (discovery_output_tokens or None),
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
        total_llm_cost = _round_cost(math.fsum([total_llm_cost, math.fsum(discovery_cost_acc)]))
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
        sorted(run_manifest_dir.glob("*.manifest.json")) if run_manifest_dir.is_dir() else []
    )

    # Deduplicate manifests by run_id to prevent double-counting reruns.
    seen_run_ids: set[str] = set()

    if run_manifest_files:
        runs_data: list[dict[str, Any]] = []
        ext_cost_acc: list[float] = []
        ext_input_tokens = 0
        ext_output_tokens = 0
        ext_docs_billed = 0
        ext_calls = 0
        ext_unknown_cost_calls = 0
        providers: set[str] = set()
        models: set[str] = set()
        schema_ids: set[str] = set()
        run_completed_ats: list[str] = []

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
            clean_stem = (
                raw_stem.removesuffix(".manifest") if raw_stem.endswith(".manifest") else raw_stem
            )
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
            run_calls = int(costs.get("llm_calls", run_docs) or 0)
            run_unknown = int(costs.get("unknown_cost_calls") or 0)
            # Every model that spent (extraction, transcription,
            # locators); a run that spent nothing names its extraction
            # model.
            run_models = sorted(
                costs.get("models") or ([lineage["model"]] if lineage.get("model") else [])
            )
            run_provider = lineage.get("provider")
            run_schema = lineage.get("schema_id")
            run_completed = run_timing.get("finished_at") or run_timing.get("completed_at")
            run_started = run_timing.get("started_at")
            run_elapsed_raw = run_timing.get("elapsed_seconds")
            run_elapsed = (
                _round_secs(float(run_elapsed_raw)) if run_elapsed_raw is not None else None
            )

            ext_cost_acc.append(run_cost)
            ext_input_tokens += run_input
            ext_output_tokens += run_output
            ext_docs_billed += run_docs
            ext_calls += run_calls
            ext_unknown_cost_calls += run_unknown
            if run_provider:
                providers.add(str(run_provider))
            models.update(run_models)
            if run_schema:
                schema_ids.add(str(run_schema))
            if run_completed:
                run_completed_ats.append(run_completed)

            run_record: dict[str, Any] = {
                "run_id": run_id,
                "documents_billed": run_docs,
                "llm_calls": run_calls,
                "unknown_cost_calls": run_unknown,
                "cost_usd": run_cost,
                "zero_cost_reason": (
                    "provider cost unknown for some calls"
                    if run_unknown
                    else None
                    if run_cost > 0
                    else "provider reported zero billable cost"
                ),
                "cost_per_document_usd": (_round_cost(run_cost / run_docs) if run_docs else 0.0),
                "input_tokens": run_input,
                "output_tokens": run_output,
                "tokens": run_input + run_output,
                "elapsed_seconds": run_elapsed,
            }
            # Add optional fields
            if run_models:
                run_record["model"] = run_models if len(run_models) > 1 else run_models[0]
            if run_provider:
                run_record["provider"] = run_provider
            if run_schema:
                run_record["schema_id"] = run_schema
            if run_started:
                run_record["started_at"] = run_started
            if run_completed:
                run_record["completed_at"] = run_completed
            if mf.get("mode"):
                run_record["mode"] = mf["mode"]
            runs_data.append(run_record)

        # Sort runs by completed_at (chronological) — works even if some are None.
        runs_data.sort(key=lambda r: r.get("completed_at") or "")

        ext_cost = _round_cost(math.fsum(ext_cost_acc))
        ext_tokens = ext_input_tokens + ext_output_tokens

        # Track earliest extraction completed_at for stale-discovery warning.
        if run_completed_ats:
            earliest_extraction_completed_at = min(run_completed_ats)

        stage_payload: dict[str, Any] = {
            "status": "completed",
            "cost_model": "per_document_llm_extraction",
            "documents_billed": ext_docs_billed,
            "cost_usd": ext_cost,
            "zero_cost_reason": (
                "provider cost unknown for some calls"
                if ext_unknown_cost_calls
                else None
                if ext_cost > 0
                else "no documents billed"
            ),
            "cost_per_document_usd": (
                _round_cost(ext_cost / ext_docs_billed) if ext_docs_billed else 0.0
            ),
            "input_tokens": ext_input_tokens,
            "output_tokens": ext_output_tokens,
            "tokens": ext_tokens,
            "llm_calls": ext_calls,
            "unknown_cost_calls": ext_unknown_cost_calls,
            "extraction_runs": len(runs_data),
            "runs": runs_data,
        }
        # Add optional fields
        if providers:
            stage_payload["provider"] = (
                sorted(providers) if len(providers) > 1 else next(iter(providers), None)
            )
        if models:
            stage_payload["model"] = sorted(models) if len(models) > 1 else next(iter(models), None)
        if schema_ids:
            stage_payload["schema_id"] = (
                sorted(schema_ids) if len(schema_ids) > 1 else next(iter(schema_ids), None)
            )
        stage_payload["records_dir"] = str(extraction_dir)
        if len(runs_data) > 1:
            stage_payload["note"] = (
                f"{len(runs_data)} extraction run(s) listed in runs[]; "
                "costs are cumulative — reruns incur additional LLM cost per document"
            )

        stages["extraction"] = stage_payload
        total_llm_cost = _round_cost(math.fsum([total_llm_cost, ext_cost]))
        total_llm_calls += ext_calls
        total_tokens += ext_tokens
        total_docs_extracted = ext_docs_billed
        total_extraction_runs = len(runs_data)
        extraction_cost_total = ext_cost

    else:
        # Fallback: scan individual extraction JSON files.
        extraction_records = [
            p for p in extraction_dir.rglob("*.json") if "run_manifests" not in p.parts
        ]
        if extraction_records:
            doc_total = 0
            doc_success = 0
            ext_cost_acc2: list[float] = []
            ext_elapsed = 0.0
            ext_tokens = 0
            ext_calls = 0
            ext_input_tokens = 0
            ext_output_tokens = 0
            provider = model = None
            run_id = schema_id = artifact_id = None
            failed_examples: list[dict[str, Any]] = []
            error_totals: dict[str, int] = {}
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
                        merged_fields_union.update(
                            [f.strip() for f in merged_part.split(",") if f.strip()]
                        )

            ext_cost_fallback = _round_cost(math.fsum(ext_cost_acc2))
            stage_payload_fb: dict[str, Any] = _omit_none(
                {
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
                }
            )
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
    if discovery_completed_at and earliest_extraction_completed_at and stages.get("discovery"):
        try:
            disc_dt = datetime.fromisoformat(discovery_completed_at)
            ext_dt = datetime.fromisoformat(earliest_extraction_completed_at)
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
    compilation_runs: list[dict[str, Any]] = []
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
        run_id = str(mf.get("run_id") or mf_path.name.removesuffix(".manifest.json"))
        if run_id in seen_compile_run_ids:
            continue
        seen_compile_run_ids.add(run_id)
        run_timing = mf.get("timing") or {}
        status_block = mf.get("status") or {}
        compilation_runs.append(
            _omit_none(
                {
                    "run_id": run_id,
                    "status": (
                        status_block.get("result")
                        if isinstance(status_block, dict)
                        else status_block
                    )
                    or "completed",
                    "mode": mf.get("mode"),
                    "schema_id": (mf.get("lineage") or {}).get("schema_id"),
                    "started_at": run_timing.get("started_at"),
                    "completed_at": run_timing.get("finished_at"),
                    **(mf.get("outputs") or {}),
                }
            )
        )

    stages["compilation"] = {
        "status": "completed",
        "output_dir": str(output_dir),
        **compilation_stats,
    }
    if compilation_runs:
        compilation_runs.sort(key=lambda run: run.get("completed_at") or "")
        stages["compilation"]["run_count"] = len(compilation_runs)
        stages["compilation"]["runs"] = compilation_runs

    # ------------------------------------------------------------------
    # Summary block — designed for quick first-read at any scale
    # ------------------------------------------------------------------
    summary: dict[str, Any] = {
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
    # Unknown-priced calls are not measured zero-cost operations.
    unknown_calls = stages.get("extraction", {}).get("unknown_cost_calls", 0)
    if unknown_calls:
        summary["unknown_cost_calls"] = unknown_calls
        summary["cost_scope"] = "known_metered_llm_only"
    if total_llm_cost == 0:
        summary["zero_cost_reason"] = (
            "provider pricing unavailable for some calls"
            if unknown_calls
            else "no LLM-billable operations in this run"
        )
    if total_search_queries > 0:
        summary["search_api_queries"] = total_search_queries
    if search_api_provider:
        summary["search_api_provider"] = search_api_provider

    # Extract domain schema version from the schema file
    domain_schema_version: str | None = None
    schema_ref = stages.get("extraction", {}).get("schema_id")
    if schema_ref and isinstance(schema_ref, str):
        try:
            schema_path = Path(schema_ref)
            if not schema_path.is_absolute():
                schema_path = Path.cwd() / schema_ref
            if schema_path.exists():
                schema_data = json.loads(schema_path.read_text(encoding="utf-8"))
                domain_schema_version = (schema_data.get("$metadata") or {}).get("version")
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    # ------------------------------------------------------------------
    # Final structure
    # ------------------------------------------------------------------
    output: dict[str, Any] = {
        "accounting_version": "2.0.0",
        "domain_schema_version": domain_schema_version,
        "domain": domain,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "summary": summary,
        "stages": stages,
    }

    if warnings:
        output["warnings"] = warnings

    return output


def write_compilation_run_manifest(
    *,
    output_dir: Path,
    schema_id: str,
    mode: str,
    stats: dict[str, Any],
    started_at: datetime,
    build_fingerprint: str,
    table_sha256: str | None,
) -> Path | None:
    """Persist one immutable compilation run manifest (compile makes no LLM calls)."""
    run_manifest_dir = output_dir / "run_manifests"
    run_manifest_dir.mkdir(parents=True, exist_ok=True)

    completed_at = datetime.now(UTC)
    stamp = completed_at.strftime("%Y%m%dT%H%M%S%fZ")
    manifest = {
        "manifest_version": "2.0.0",
        "run_id": f"compile://{stamp}",
        "mode": mode,
        "status": {"result": "success"},
        "timing": {
            "started_at": started_at.isoformat().replace("+00:00", "Z"),
            "finished_at": completed_at.isoformat().replace("+00:00", "Z"),
            "elapsed_seconds": _round_secs((completed_at - started_at).total_seconds()),
        },
        "lineage": {
            "schema_id": schema_id,
            "build_fingerprint": build_fingerprint,
            "table_sha256": table_sha256,
        },
        "outputs": stats,
    }
    manifest_path = run_manifest_dir / f"{stamp}.manifest.json"
    try:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    return manifest_path


def latest_compilation_manifest(output_dir: Path) -> dict[str, Any] | None:
    """The newest compilation run manifest (stamped file names sort chronologically)."""
    paths = sorted((Path(output_dir) / "run_manifests").glob("*.manifest.json"))
    if not paths:
        return None
    try:
        manifest = json.loads(paths[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None
