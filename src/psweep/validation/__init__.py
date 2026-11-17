"""Validation: contenders on the real pipeline, alignment, judge, scores and samples."""

from __future__ import annotations

import csv
import io
import json
import random
import shutil
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timezone, UTC
from functools import cache, partial
from pathlib import Path

from psweep.compilation.roster import METADATA_COLUMNS
from psweep.config import load_runtime_config_file, resolve_command_config
from psweep.config.runtime_config_loader import resolve_compilation_metadata
from psweep.extraction.llm_factory import build_llm_client, model_identity
from psweep.extraction.provenance import source_context
from psweep.extraction.roster import Roster
from psweep.pipeline import (
    _execute_extraction,
    _prepare_extraction,
    compile_extractions,
    compiled_table_fresh,
)
from psweep.security import validate_protected_paths
from psweep.utils.file_io import artifact_sha256, atomic_write, publication_lock
from psweep.utils.model_pricing import get_model_pricing
from psweep.validation.align import Source, context_fields, align, columns
from psweep.validation.judge import judge as judge_items, judge_identity
from psweep.validation.quality import failed_documents, model_quality
from psweep.validation.tables import validate_tables
from psweep.workspace import Workspace

Notify = Callable[[str], None]


def covered_keys(plan: dict, roster: Roster) -> frozenset[tuple]:
    """Roster keys of targets with at least one anchored curated document."""
    return frozenset(roster.key(roster.targets[t]) for ids in plan["anchors"].values() for t in ids)


def _quiet(_message: str) -> None:
    return None


def _preflight(config_path: Path) -> dict:
    """Config, primary extraction and table freshness, reference headers; no LLM call."""
    cfg = load_runtime_config_file(config_path)
    if not cfg.get("validation"):
        raise ValueError(f"{config_path} has no validation: section")
    settings = resolve_command_config(command="validate", cli_values={}, config_data=cfg)[
        "validation"
    ]
    validate_protected_paths({"references": settings["references"]})
    extract = resolve_command_config(
        command="extract", cli_values={"skip_existing": True}, config_data=cfg
    )
    plan = _prepare_extraction(extract, config_path=str(config_path))
    failed = failed_documents(plan["output"])
    stale = [
        doc
        for doc in plan["pending"]
        if doc not in plan["roster_errors"]
        and plan["anchors"].get(doc) != set()
        and str(doc) not in failed
    ]
    if stale or plan["withdrawn"]:
        raise ValueError(
            f"primary extraction is stale ({len(stale) + len(plan['withdrawn'])} documents); "
            f"run psweep extract --config {config_path}"
        )
    compile_inputs = resolve_command_config(command="compile", cli_values={}, config_data=cfg)
    if not compiled_table_fresh(
        compile_inputs["extracted_dir"],
        compile_inputs["schema"],
        config_path=config_path,
        output_dir=compile_inputs["output"],
    ):
        raise ValueError(
            f"compiled table is stale or missing; run psweep compile --config {config_path}"
        )
    metadata = resolve_compilation_metadata(compile_inputs)
    workspace = Workspace(cfg["domain"])
    references = [
        Source(Path(path).stem, "reference", Path(path), None) for path in settings["references"]
    ]
    roster = plan["roster"]
    cols = columns(
        metadata,
        roster,
        key_fields=settings["key_fields"],
        quote_field=settings["quote_field"],
        fields=settings["fields"] or None,
        references=references,
    )
    return {
        "cfg": cfg,
        "config_path": config_path,
        "settings": settings,
        "extract": extract,
        "plan": plan,
        "metadata": metadata,
        "roster": roster,
        "columns": cols,
        "references": references,
        "workspace": workspace,
        "schema": compile_inputs["schema"],
        "table": Path(compile_inputs["output"]) / f"{workspace.compiled_name}.csv",
    }


def _identity(alias: str | None, models: dict, provider: str | None) -> dict:
    model, provider, endpoint = model_identity(alias, models=models, provider=provider)
    return {"model": model, "provider": provider, "endpoint": endpoint}


def _estimate(alias: str, pending: int, primary: dict, models: dict) -> str:
    """Price pending documents at the primary's mean measured tokens per document."""
    called = primary.get("documents_called") or 0
    rates = get_model_pricing(model_identity(alias, models=models)[0])
    if not called or not rates:
        return f"{alias}: extracting {pending} documents (cost unknown)"
    tokens = primary["input_tokens"] * rates[0] + primary["output_tokens"] * rates[1]
    return f"{alias}: extracting {pending} documents (~${pending * tokens / called / 1e6:.2f})"


def _challenger(
    ctx: dict, alias: str, notify: Notify, primary_usage: Callable[[], dict]
) -> tuple[Path, int, float]:
    """Extract (reusing records) and compile one challenger under ``validated/<alias>/``."""
    config_path, root = ctx["config_path"], ctx["workspace"].validated / alias
    inputs = resolve_command_config(
        command="extract",
        cli_values={"model": alias, "output": str(root / "extracted"), "skip_existing": True},
        config_data=ctx["cfg"],
    )
    plan = _prepare_extraction(inputs, config_path=str(config_path))
    calls, cost = 0, 0.0
    if plan["pending"]:
        models = ctx["cfg"].get("models") or {}
        notify(_estimate(alias, len(plan["pending"]), primary_usage(), models))
    if plan["pending"] or plan["withdrawn"]:
        result = _execute_extraction(plan)
        if result.manifest_path:
            costs = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))["costs"]
            calls, cost = costs["llm_calls"], costs["total_cost_usd"] or 0.0
    if not compiled_table_fresh(
        root / "extracted", ctx["schema"], config_path=config_path, output_dir=root
    ):
        compiled = compile_extractions(
            root / "extracted",
            ctx["schema"],
            config_path=config_path,
            output_dir=root,
            report_format="csv",
        )
        calls += compiled.costs.get("llm_calls", 0)
        cost += compiled.costs.get("total_cost_usd", 0) or 0.0
        if compiled.status in {"partial_failure", "failed"}:
            raise ValueError(f"Challenger '{alias}' has unresolved evidence reconciliation")
    return root / ctx["table"].name, calls, cost


def _data_rows(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return max(sum(1 for _ in csv.reader(handle)) - 1, 0)


def run_validation(config_path: Path, *, fresh: bool = False, notify: Notify = _quiet) -> dict:
    """Score the primary against challengers and references; returns ``summary.json``.

    One run per domain at a time: the lock spans ``--fresh``, challenger work and staging.
    """
    config_path = Path(config_path)
    ctx = _preflight(config_path)
    validated = ctx["workspace"].validated
    with publication_lock(validated.parent / f".{validated.name}-publication.lock"):
        return _run(ctx, config_path, fresh=fresh, notify=notify)


def _run(ctx: dict, config_path: Path, *, fresh: bool, notify: Notify) -> dict:
    started, clock = datetime.now(UTC), time.monotonic()
    settings, metadata, cols = ctx["settings"], ctx["metadata"], ctx["columns"]
    validated, models = ctx["workspace"].validated, ctx["cfg"].get("models") or {}
    if fresh:
        shutil.rmtree(validated, ignore_errors=True)
    validated.mkdir(parents=True, exist_ok=True)
    primary_dir = Path(ctx["plan"]["output"])

    @cache
    def primary_usage() -> dict:
        """Record-only primary usage (no quote checks) to price pending challenger documents."""
        return model_quality({"primary": primary_dir}, metadata, quote_field=None, focus=())[
            "primary"
        ]

    primary_model = ctx["extract"].get("model")
    sources = [Source("primary", "primary", ctx["table"], primary_model)]
    provider = ctx["extract"].get("provider")
    contenders = {"primary": _identity(primary_model, models, provider)}
    run: dict = {"llm_calls": {}, "cost_usd": {}}
    for alias in settings["models"]:
        table, calls, cost = _challenger(ctx, alias, notify, primary_usage)
        sources.append(Source(alias, "challenger", table, alias))
        contenders[alias] = _identity(alias, models, provider)
        run["llm_calls"][alias], run["cost_usd"][alias] = calls, round(cost, 6)
    quality = model_quality(
        {"primary": primary_dir, **{a: validated / a / "extracted" for a in settings["models"]}},
        metadata,
        quote_field=settings["quote_field"],
        focus=cols.focus,
        ocr_corrections=ctx["extract"].get("ocr_corrections"),
    )
    sources += ctx["references"]
    judge_fn = judge_model = None
    if settings["judge"]:
        judge_model = judge_identity(settings["judge"], models=models)
        judge_fn = partial(
            judge_items,
            client=build_llm_client(settings["judge"], models=models),
            cache_path=validated / ".judge_cache.jsonl",
            workers=ctx["extract"].get("max_concurrent") or 1,
        )
    inputs = {
        "config_sha256": artifact_sha256(config_path),
        "schema_sha256": artifact_sha256(Path(ctx["schema"])),
        "contenders": contenders,
        "references": {
            s.label: {
                "path": s.path.as_posix(),
                "sha256": artifact_sha256(s.path),
                "rows": _data_rows(s.path),
            }
            for s in ctx["references"]
        },
        "fields": list(settings["fields"]),
        "key_fields": list(settings["key_fields"]),
        "quote_field": settings["quote_field"],
        "judge": settings["judge"],
    }
    roster = ctx["roster"]
    staging_path = validated / ".staging" / "validate.sqlite"
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    staging_path.unlink(missing_ok=True)
    staging = sqlite3.connect(staging_path)
    try:
        stats: dict = {}
        clusters = align(
            sources,
            metadata=metadata,
            roster=roster,
            key_fields=settings["key_fields"],
            quote_field=settings["quote_field"],
            columns=cols,
            staging=staging,
            stats=stats,
        )
        summary = validate_tables(
            clusters,
            sources=sources,
            columns=cols,
            judge_model=judge_model,
            judge_fn=judge_fn,
            staging=staging,
            out_dir=validated,
            quality=quality,
            align_stats=stats,
            inputs=inputs,
            run={
                "started_at": started.isoformat().replace("+00:00", "Z"),
                "elapsed_seconds": round(time.monotonic() - clock, 3),
                **run,
            },
            covered=covered_keys(ctx["plan"], roster) if roster else None,
            min_accuracy=settings["min_accuracy"],
        )
    finally:
        staging.close()
    shutil.rmtree(staging_path.parent)
    return summary


def _document_urls(plan: dict, roster: Roster) -> dict[tuple, set[str]]:
    urls: dict[tuple, set[str]] = {}
    for doc, ids in plan["anchors"].items():
        context = source_context(doc, plan["context"])
        if context and context.url:
            for t in ids:
                urls.setdefault(roster.key(roster.targets[t]), set()).add(context.url)
    return urls


def _sample(ctx: dict, n: int, seed: int, notify: Notify) -> list[list[str]]:
    """Header plus the drawn units' rows, in table order."""
    cols, roster, settings = ctx["columns"], ctx["roster"], ctx["settings"]
    titles = cols.titles
    with ctx["table"].open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        table = list(reader)
    if roster:
        known = {*titles.values(), *METADATA_COLUMNS}
        filled = [
            *(titles[f] for f in roster.fields),
            *(h for h in header if h not in known),
        ]
        blank = [*(titles[f] for f in cols.focus), "documents"]
    else:
        scope = [f for f in settings["key_fields"] if f in context_fields(ctx["metadata"])]
        filled = [titles[f] for f in scope]
        rest = [*settings["key_fields"], *cols.focus, *filter(None, [settings["quote_field"]])]
        blank = [titles[f] for f in rest if f not in scope]
    at = [header.index(h) for h in filled]
    units = list(dict.fromkeys(tuple(row[i] for i in at) for row in table))
    if n > len(units):
        notify(f"--sample {n} exceeds the {len(units)} available units; writing {len(units)}")
    chosen = set(
        random.Random(seed).sample(  # noqa: S311 - reproducible sampling, not crypto
            units, min(n, len(units))
        )
    )
    urls = _document_urls(ctx["plan"], roster) if roster else {}
    rows = [[*filled, *blank]]
    for unit in units:
        if unit in chosen:
            row = [*unit, *[""] * len(blank)]
            if roster:
                key = roster.key(dict(zip(roster.fields, unit[: len(roster.fields)], strict=True)))
                row[-1] = " | ".join(sorted(urls.get(key, ())))
            rows.append(row)
    return rows


def write_sample(config_path: Path, *, n: int, seed: int = 0, notify: Notify = _quiet) -> Path:
    """Write ``validated/sample_<n>.csv`` for blind labelling; no LLM call."""
    ctx = _preflight(Path(config_path))
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(_sample(ctx, n, seed, notify))
    path = ctx["workspace"].validated / f"sample_{n}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, buffer.getvalue())
    return path
