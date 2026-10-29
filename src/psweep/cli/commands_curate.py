"""Publish human discovery decisions with their provenance."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import click

from psweep.cli import commands
from psweep.cli.ui import print_error
from psweep.config import (
    RuntimeConfigError,
    load_runtime_config_file,
    resolve_command_config,
)
from psweep.discovery.publication import (
    PublicationError,
    csv_content,
    discovery_publication,
    publish_manual_curation,
    record_is_curated,
    resolve_publication_paths,
)


def _review_sidecar_update(record: dict) -> tuple[Path, str] | None:
    """Change only the human block of the full-filename review
    sidecar.
    """
    source = Path(str(record.get("path") or ""))
    sidecar = source.parent / ".review" / f"{source.name}.json"
    if not sidecar.exists():
        return None
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(
        payload.get("human", {}), dict
    ):
        message = f"Invalid review sidecar: {sidecar}"
        raise PublicationError(message)
    payload["human"] = {
        **payload.get("human", {}),
        "decision": record.get("human_decision") or None,
        "notes": record.get("human_notes") or None,
    }
    return sidecar, json.dumps(
        payload, indent=2, sort_keys=True, allow_nan=False
    )


def _manifest_path(value: object, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        message = f"Manifest is missing lineage.{field}"
        raise PublicationError(message)
    return Path(value).resolve()


def _read_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("lineage"), dict
    ):
        message = "Invalid discovery manifest"
        raise PublicationError(message)
    if not isinstance(manifest.get("input"), dict):
        message = "Invalid discovery manifest input"
        raise PublicationError(message)
    records = manifest.get("downloads")
    if not isinstance(records, list) or any(
        not isinstance(record, dict) for record in records
    ):
        message = "Manifest downloads must be a list of discovery records"
        raise PublicationError(message)
    return manifest


def _read_csv(path: Path, required: set[str]) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or ())
        if not required.issubset(fields) or len(fields) != len(set(fields)):
            message = f"Invalid curation ledger/index: {path}"
            raise PublicationError(message)
        rows = list(reader)
    if any(
        None in row or any(value is None for value in row.values())
        for row in rows
    ):
        message = f"Malformed curation ledger/index: {path}"
        raise PublicationError(message)
    return fields, rows


def _records_by_path(records: list[dict]) -> dict[str, dict]:
    by_path: dict[str, dict] = {}
    for record in records:
        relative = str(record.get("relative_path") or "")
        if not relative:
            continue
        if relative in by_path:
            message = f"Duplicate manifest document: {relative}"
            raise PublicationError(message)
        by_path[relative] = record
    return by_path


def _apply_human_decision(
    row: dict, record: dict, *, review_ran: bool
) -> bool:
    relative = row["relative_path"]
    if (
        row.get("path")
        and Path(row["path"]).resolve()
        != Path(str(record.get("path") or "")).resolve()
    ):
        message = f"Review ledger source disagrees with manifest: {relative}"
        raise PublicationError(message)
    decision = str(row.get("human_decision") or "").strip().lower()
    if decision not in {"", "keep", "reject"}:
        message = f"Invalid human decision for {relative}: {decision!r}"
        raise PublicationError(message)
    automated = {
        key: value for key, value in record.items() if key != "human_decision"
    }
    before = record_is_curated(automated, review_ran=review_ran)
    record["human_decision"] = decision or None
    record["human_notes"] = str(row.get("human_notes") or "").strip() or None
    effective = record_is_curated(record, review_ran=review_ran)
    return bool(decision and before != effective)


def _apply_ledger(rows: list[dict], records: list[dict]) -> tuple[dict, int]:
    by_path = _records_by_path(records)
    review_ran = any(
        record.get("review_selected") is not None for record in records
    )
    seen: set[str] = set()
    overridden = 0
    updates: dict[Path, str] = {}
    for row in rows:
        relative = row.get("relative_path") or ""
        if relative not in by_path or relative in seen:
            message = (
                f"Unknown or duplicate review ledger document: {relative}"
            )
            raise PublicationError(message)
        seen.add(relative)
        record = by_path[relative]
        overridden += _apply_human_decision(row, record, review_ran=review_ran)
        sidecar = _review_sidecar_update(record)
        if sidecar is not None:
            updates[sidecar[0]] = sidecar[1]
    for record in records:
        record["curated_selected"] = record_is_curated(
            record, review_ran=review_ran
        )
    return updates, overridden


def _updated_index(path: Path, records: list[dict]) -> str:
    fields, rows = _read_csv(path, {"relative_path"})
    human_fields = ("human_decision", "human_notes", "curated_selected")
    fields = list(dict.fromkeys([*fields, *human_fields]))
    by_path = _records_by_path(records)
    indexed: set[str] = set()
    for row in rows:
        relative = row.get("relative_path") or ""
        if relative not in by_path:
            continue
        if relative in indexed:
            message = f"Duplicate discovery index document: {relative}"
            raise PublicationError(message)
        indexed.add(relative)
        row.update({key: by_path[relative].get(key) for key in human_fields})
    selected = {
        key for key, record in by_path.items() if record["curated_selected"]
    }
    if not selected.issubset(indexed):
        message = "Selected documents are missing from the run download index"
        raise PublicationError(message)
    return csv_content(fields, rows)


def _partition_decisions(manifest: dict) -> tuple[set[Path], set[Path]]:
    records = _records_by_path(manifest["downloads"])
    attempted = {
        Path(key).parent
        for key, record in records.items()
        if record.get("status") == "downloaded"
    }
    blocked = {
        Path(key).parent
        for key, record in records.items()
        if record.get("review_failed")
        and record.get("human_decision") not in {"keep", "reject"}
    }
    values = manifest["lineage"].get("blocked_partitions", [])
    if not isinstance(values, list) or any(
        not isinstance(value, str) for value in values
    ):
        message = (
            "Manifest blocked_partitions must be a list of relative paths"
        )
        raise PublicationError(message)
    review_only = manifest["lineage"].get("review_only_blocked_partitions", [])
    if not isinstance(review_only, list) or not all(
        isinstance(value, str) for value in review_only
    ):
        message = (
            "Manifest review-only blocks must be a list of relative paths"
        )
        raise PublicationError(message)
    resolved_reviews = {
        Path(key).parent
        for key, record in records.items()
        if record.get("review_failed")
        and record.get("human_decision") in {"keep", "reject"}
    }
    released = {Path(value) for value in review_only} & resolved_reviews
    blocked.update(
        Path(value) for value in values if Path(value) not in released
    )
    return attempted, blocked


def _curate_run(
    run_dir: Path, *, manifest_path: Path | None = None
) -> tuple[Path, int, int, int]:
    # Derive corpus identity before reading potentially interrupted
    # files.
    domain_dir = (
        run_dir.parent.parent if run_dir.parent.name == "runs" else run_dir
    )
    with discovery_publication(domain_dir):
        actual_manifest = manifest_path or run_dir / "manifest.json"
        if not actual_manifest.exists():
            message = (
                f"Discovery manifest not found: {actual_manifest}. "
                "For a custom manifest filename, use --config <run.yaml>."
            )
            raise PublicationError(message)
        return _curate_locked(actual_manifest, domain_dir)


def _curate_locked(
    manifest_path: Path, domain_dir: Path
) -> tuple[Path, int, int, int]:
    manifest = _read_manifest(manifest_path)
    lineage = manifest["lineage"]
    paths = resolve_publication_paths(
        _manifest_path(lineage.get("documents_dir"), field="documents_dir"),
        manifest_path,
        manifest["input"].get("domain"),
        domain_dir=domain_dir,
        run_curated_dir=Path(lineage["curated_dir"])
        if lineage.get("curated_dir")
        else None,
    )
    run_index = _manifest_path(
        lineage.get("download_index_csv"), field="download_index_csv"
    )
    if paths.run_curated_dir == domain_dir / "curated":
        message = "Manifest must record a distinct run-local curated_dir"
        raise PublicationError(message)
    if run_index == domain_dir / "download_index.csv":
        message = (
            "Manifest must record a distinct run-local download_index_csv"
        )
        raise PublicationError(message)
    _, rows = _read_csv(
        _manifest_path(
            lineage.get("review_index_csv"), field="review_index_csv"
        ),
        {"relative_path", "human_decision"},
    )
    updates, overridden = _apply_ledger(rows, manifest["downloads"])
    lineage.update(
        {
            "domain_dir": domain_dir.as_posix(),
            "curated_dir": paths.run_curated_dir.as_posix(),
            "curated_count": sum(
                bool(record["curated_selected"])
                for record in manifest["downloads"]
            ),
            "human_curation": {
                "reviewed": len(rows),
                "human_overrides": overridden,
            },
        }
    )
    updates[manifest_path.resolve()] = json.dumps(
        manifest, indent=2, sort_keys=True, allow_nan=False
    )
    attempted, blocked = _partition_decisions(manifest)
    curated, count = publish_manual_curation(
        paths=paths,
        download_records=manifest["downloads"],
        run_index=run_index,
        run_index_content=_updated_index(run_index, manifest["downloads"]),
        metadata_updates=updates,
        attempted_partitions=attempted,
        blocked_partitions=blocked,
        _lock_held=True,
    )
    return curated, count, len(rows), overridden


def _latest_run(domain: str) -> Path:
    root = Path("discovered") / domain
    latest = root / "latest"
    if latest.exists():
        return latest.resolve()
    pointer = root / "latest.txt"
    if pointer.is_file():
        return Path(pointer.read_text(encoding="utf-8").strip()).resolve()
    message = f"No latest run found for domain: {latest} (run discover first)."
    raise PublicationError(message)


def _resolve_run(
    target: str | None, config_path: str | None, run_dir_opt: str | None
) -> tuple[Path, Path | None]:
    if run_dir_opt:
        return Path(run_dir_opt).resolve(), None
    resolved = {}
    if config_path:
        resolved = resolve_command_config(
            command="discover",
            config_data=load_runtime_config_file(Path(config_path)),
            strict=False,
            cli_values={},
        )
    if resolved.get("output_manifest"):
        manifest = Path(str(resolved["output_manifest"])).resolve()
        return manifest.parent, manifest
    domain = target or resolved.get("domain")
    if not domain:
        message = "Pass --config <run.yaml>, a domain name, or --run <dir>."
        raise PublicationError(message)
    return _latest_run(str(domain)), None


@click.command()
@click.argument("target", required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help=(
        "Path to runtime config (.yaml/.yml/.json) — "
        "locates the domain's latest run or explicit manifest"
    ),
)
@click.option(
    "--run",
    "run_dir_opt",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help="Path to a specific run directory (defaults to the domain's latest/)",
)
@click.option("--quiet", is_flag=True, help="Minimal output")
@click.option("--verbose", is_flag=True, help="Detailed output")
@click.option("--debug", is_flag=True, help="Debug output with tracebacks")
def curate(
    target: str | None,
    config_path: str | None,
    run_dir_opt: str | None,
    quiet: bool,
    verbose: bool,
    debug: bool,
) -> None:
    """Publish human ledger edits to local and consolidated curation."""
    view = commands.begin_run(
        "curate", quiet=quiet, verbose=verbose, debug=debug
    )
    try:
        run_dir, manifest = _resolve_run(target, config_path, run_dir_opt)
        view.header("CURATION")
        view.config({"Run folder": run_dir.as_posix()})
        curated_dir, count, reviewed, overridden = _curate_run(
            run_dir, manifest_path=manifest
        )
    except (OSError, ValueError, TypeError, RuntimeConfigError) as exc:
        print_error("Curation publication failed", str(exc))
        if debug:
            raise
        sys.exit(1)
    if view.is_quiet:
        click.echo(curated_dir.as_posix())
        return
    view.success(f"Curated {count} document(s)")
    view.summary(
        {
            "Reviewed": str(reviewed),
            "Kept": str(count),
            "Human overrides": str(overridden),
        },
        title="Curation Summary",
    )
    view.outputs({"Curated documents": curated_dir.as_posix()})
    view.next_steps(
        [
            f"Extract the curated set: pixi run psweep extract {curated_dir} "
            "--schema <schema>"
        ]
    )
