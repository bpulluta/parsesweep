"""``psweep validate``: score the primary against challengers and references."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import click


def _gate_failure(gate: dict) -> str:
    reasons = []
    for label, result in gate["references"].items():
        if result["passed"]:
            continue
        if count := result["out_of_scope"]:
            rows = "row" if count == 1 else "rows"
            reasons.append(f"{label}: {count} reference {rows} out of scope")
        else:
            accuracy = result["accuracy_j"]
            shown = "—" if accuracy is None else f"{accuracy:.2f}"
            reasons.append(
                f"primary accuracy {shown} < min_accuracy {gate['min_accuracy']:.2f} vs {label}"
            )
    return "; ".join(reasons)


@click.command()
@click.option(
    "--config",
    "config_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Domain run config with a validation: section.",
)
@click.option("--fresh", is_flag=True, help="Delete validated/ and re-run every challenger.")
@click.option(
    "--sample",
    type=click.IntRange(min=1),
    help="Write validated/sample_<N>.csv, a blind labelling template, and exit.",
)
@click.option("--seed", type=int, default=0, show_default=True, help="Seed for --sample.")
@click.option(
    "--validate-config",
    "validate_config_only",
    is_flag=True,
    help="Validate the validation: section and exit.",
)
@click.option("--quiet", "-q", is_flag=True, help="Minimal output.")
@click.option("--verbose", "-v", is_flag=True, help="Detailed output.")
@click.option("--debug", is_flag=True, help="Debug output.")
def validate(
    config_path: Path,
    fresh: bool,
    sample: int | None,
    seed: int,
    validate_config_only: bool,
    quiet: bool,
    verbose: bool,
    debug: bool,
) -> None:
    """Compare the primary extraction with challenger models and human references.

    Reuses records and judge verdicts; only missing work calls an LLM.

    \b
        psweep validate --config config/my_domain/run.yaml
        psweep validate --config config/my_domain/run.yaml --sample 100 --seed 1
    """
    from psweep.cli.commands import begin_run
    from psweep.config import (
        RuntimeConfigError,
        load_runtime_config_file,
        resolve_command_config,
    )
    from psweep.exceptions import ParseSweepError

    if sample is not None and fresh:
        raise click.UsageError("--sample writes a template only; drop --fresh")
    view = begin_run("validate", quiet=quiet, verbose=verbose, debug=debug)
    try:
        if validate_config_only:
            config_data = load_runtime_config_file(config_path)
            if not config_data.get("validation"):
                raise RuntimeConfigError(f"{config_path} has no validation: section")
            resolve_command_config(command="validate", cli_values={}, config_data=config_data)
            view.success("Runtime config validation passed for validate command")
            return
        from psweep.validation import run_validation, write_sample
        from psweep.validation.tables import leaderboard

        if sample is not None:
            path = write_sample(config_path, n=sample, seed=seed, notify=view.warning)
            view.success(f"Wrote {path.as_posix()}")
            view.next_steps(
                [
                    (
                        "Fill it blind (blank = not present; rows mode: use the schema's key "
                        "vocabulary and paste the quote) and delete unlabelled rows"
                    ),
                    (
                        f"Save it under {config_path.parent.as_posix()}/samples/ and add it to "
                        "validation.references"
                    ),
                ]
            )
            return
        summary = run_validation(config_path, fresh=fresh, notify=view.info)
    except (ValueError, OSError, sqlite3.Error, ParseSweepError) as exc:
        view.error("Validation failed", str(exc))
        raise click.exceptions.Exit(1) from exc
    domain = load_runtime_config_file(config_path)["domain"]
    if not view.is_quiet:
        view.console.print()
        for line in leaderboard(summary, f"validate {domain}"):
            view.console.print(line, markup=False, highlight=False)
    gate = summary.get("gate")
    if gate and not gate["passed"]:
        view.error("Validation gate failed", _gate_failure(gate))
        raise click.exceptions.Exit(1)
