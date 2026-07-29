"""`curate` command extracted from the legacy CLI monolith."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Optional

import click

from psweep.config import RuntimeConfigError
from psweep.discovery import DiscoveryEngine


def _update_review_sidecar(row: dict) -> None:
    """Write human_decision/notes from a review.csv row into its .review JSON."""
    path_str = row.get("path")
    if not path_str:
        return

    src = Path(str(path_str))
    sidecar = src.parent / ".review" / f"{src.stem}.json"
    if not sidecar.exists():
        return

    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return

    payload.setdefault("human", {})
    payload["human"]["decision"] = str(row.get("human_decision") or "").strip() or None
    payload["human"]["notes"] = str(row.get("human_notes") or "").strip() or None

    try:
        sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        return


@click.command()
@click.argument("target", required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Path to runtime config file (.yaml/.yml/.json) — used to locate the domain's latest run",
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
    target: Optional[str],
    config_path: Optional[str],
    run_dir_opt: Optional[str],
    quiet: bool,
    verbose: bool,
    debug: bool,
) -> None:
    """Rebuild a run's curated/ set from human edits in review.csv."""
    from psweep.cli.commands import begin_run, _resolve_runtime_command_inputs
    from psweep.cli.ui import print_error

    view = begin_run("curate", quiet=quiet, verbose=verbose, debug=debug)

    run_dir: Optional[Path] = None
    if run_dir_opt:
        run_dir = Path(run_dir_opt)
    else:
        domain = target
        if not domain and config_path:
            try:
                resolved = _resolve_runtime_command_inputs(
                    command_name="discover",
                    config_path=config_path,
                    strict=False,
                    cli_values={},
                )
                domain = resolved.get("domain")
            except RuntimeConfigError:
                domain = None

        if not domain:
            print_error(
                "Could not determine which run to curate",
                "Pass --config <run.yaml>, a domain name, or --run <dir>.",
            )
            sys.exit(1)

        latest = Path("discovered") / str(domain) / "latest"
        if not latest.exists():
            print_error(
                "No latest run found for domain",
                f"Expected {latest.as_posix()} (run `discover` first).",
            )
            sys.exit(1)
        run_dir = latest.resolve()

    review_csv = run_dir / "review.csv"
    documents_dir = run_dir / "documents"
    if not review_csv.exists():
        print_error(
            "review.csv not found in run",
            f"Expected {review_csv.as_posix()}.",
        )
        sys.exit(1)

    view.header("CURATION")
    view.config(
        {
            "Run folder": run_dir.as_posix(),
            "Review ledger": review_csv.name,
            "Documents": documents_dir.name,
        }
    )

    with review_csv.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    def _truthy(value: Optional[str]) -> bool:
        return str(value or "").strip().lower() in {"true", "1", "yes", "keep"}

    records: list[dict[str, object]] = []
    kept = overridden = 0
    for row in rows:
        decision = str(row.get("human_decision") or "").strip().lower()
        llm_keep = _truthy(row.get("llm_selected"))
        if decision in {"keep", "reject"}:
            effective = decision == "keep"
            if effective != llm_keep:
                overridden += 1
        else:
            effective = llm_keep
        if effective:
            kept += 1
        records.append(
            {
                "path": row.get("path"),
                "relative_path": row.get("relative_path"),
                "review_selected": effective,
            }
        )
        _update_review_sidecar(row)

    curated_dir, count = DiscoveryEngine._materialize_curated(
        documents_dir=documents_dir,
        download_records=records,
    )

    if view.is_quiet:
        click.echo(curated_dir.as_posix())
        return

    view.success(f"Curated {count} document(s)")
    view.summary(
        {
            "Reviewed": str(len(records)),
            "Kept": str(kept),
            "Rejected": str(len(records) - kept),
            "Human overrides": str(overridden),
        },
        title="Curation Summary",
    )
    view.outputs({"Curated documents": curated_dir.as_posix()})
    view.next_steps(
        [
            f"Extract the curated set: pixi run psweep extract {curated_dir} --schema <schema>",
        ]
    )
