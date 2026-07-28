"""
Command-line interface for ParseSweep.

Organizes commands into two categories:
1. Core workflow (commands.py): extract, check, compile, discover, curate, compare, benchmark
2. Utilities (utils_commands.py): init, preview, estimate, config, check-schema, init-domain-schema
"""

import click

from psweep import __version__
from psweep.cli.commands import (
    discover,
    curate,
    extract,
    check,
    compile,
    compare,
    benchmark,
)
from psweep.cli.utils_commands import (
    init,
    init_domain_schema_cmd,
    preview,
    estimate,
    check_schema_cmd,
    config,
)


@click.group()
@click.version_option(version=__version__, prog_name="psweep")
def cli():
    """
    📄 ParseSweep

    AI-powered extraction of structured data from documents.

    This tool helps you convert any PDF document into structured data
    that can be analyzed in spreadsheets or databases.

    \b
    QUICK START:
        1. Put your documents in a folder (e.g., documents/my_domain/)
        2. Create a config: config/my_domain/run.yaml (references your schema)
        3. Extract:  psweep extract --config config/my_domain/run.yaml
        4. Compile:  psweep compile --config config/my_domain/run.yaml
        5. Open the CSV or Excel file!

    \b
    COMMON WORKFLOWS:

        Full pipeline with config (RECOMMENDED):
        $ psweep extract --config config/my_domain/run.yaml
        $ psweep compile --config config/my_domain/run.yaml

        Discovery + extraction + compilation:
        $ psweep discover --config config/my_domain/run.yaml
        $ psweep extract --config config/my_domain/run.yaml
        $ psweep compile --config config/my_domain/run.yaml

    \b
    REQUIREMENTS:
        • Python 3.12 or later
        • Azure OpenAI or OpenAI API key (run: psweep init)
        • Domain config YAML (references schema, sets page targeting, etc.)

    \b
    NEED HELP?
        • Initialize setup: psweep init
        • See command help: psweep extract --help
        • Documentation: https://github.com/bpulluta/parsesweep
        • Issues: Create an issue on GitHub
    """


# Register commands
cli.add_command(init)
cli.add_command(init_domain_schema_cmd)
cli.add_command(discover)
cli.add_command(curate)
cli.add_command(extract)
cli.add_command(preview)
cli.add_command(estimate)
cli.add_command(check)
cli.add_command(check_schema_cmd)
cli.add_command(compile)
cli.add_command(compare)
cli.add_command(benchmark)
cli.add_command(config)


@cli.command()
@click.option("--config", "config_path", required=True, help="Path to run.yaml config file")
@click.option("--reprocess", is_flag=True, help="Re-extract already-processed documents")
@click.option("--fresh", is_flag=True, help="Ignore all caches — reprocess everything from scratch")
@click.option("--skip-discover", is_flag=True, help="Skip discovery (use existing curated docs)")
@click.option("--skip-extract", is_flag=True, help="Skip extraction (compile from existing)")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("-q", "--quiet", is_flag=True, help="Minimal output")
def run(config_path: str, reprocess: bool, fresh: bool, skip_discover: bool, skip_extract: bool, yes: bool, quiet: bool):
    """Run the full pipeline: discover → extract → compile.

    Shows what will be reused from previous runs and what's new, then asks
    for confirmation before proceeding. Use --fresh to ignore all caches.

    \b
    Usage:
        psweep run --config config/my_domain/run.yaml

    \b
    Adding new targets:
        1. Add rows to config/<domain>/targets.csv
        2. Run: psweep run --config config/<domain>/run.yaml
        3. Only new targets are processed; previous results are preserved.
    """
    import csv
    import json
    import subprocess
    import sys
    from pathlib import Path

    import yaml

    config = Path(config_path)
    if not config.exists():
        click.echo(f"✗ Config not found: {config_path}", err=True)
        sys.exit(1)

    with config.open() as f:
        cfg = yaml.safe_load(f)

    domain = cfg.get("domain", config.parent.name)
    discovery_cfg = cfg.get("discovery", {})
    targets_csv = config.parent / discovery_cfg.get("targets_csv", "targets.csv")

    # --- Build run plan ---
    total_targets = 0
    target_labels: list[str] = []
    if targets_csv.exists():
        with targets_csv.open() as f:
            target_labels = [row.get("label", "") for row in csv.DictReader(f)]
        total_targets = len(target_labels)

    checkpoint_path = Path(f"discovered/{domain}/checkpoint.json")
    checkpointed: list[str] = []
    if checkpoint_path.exists() and not fresh:
        try:
            checkpointed = list(json.loads(checkpoint_path.read_text()).get("entries", {}).keys())
        except Exception:
            pass

    new_targets = [t for t in target_labels if t not in checkpointed]
    curated_dir = Path(f"discovered/{domain}/curated")
    curated_dir = Path(f"discovered/{domain}/curated")
    curated_count = sum(1 for f in curated_dir.rglob("*") if f.is_file() and ".text" not in str(f) and f.suffix != ".json") if curated_dir.exists() else 0

    extraction_dir = Path(cfg.get("extraction", {}).get("output_dir", f"extracted/{domain}"))
    extracted_count = 0
    if extraction_dir.exists():
        manifests_dir = extraction_dir / "run_manifests"
        all_json = sum(1 for _ in extraction_dir.rglob("*.json"))
        manifest_json = sum(1 for _ in manifests_dir.rglob("*.json")) if manifests_dir.exists() else 0
        extracted_count = all_json - manifest_json

    # --- Show plan ---
    if not quiet:
        click.echo(f"\n{'─' * 60}")
        click.echo(f"  Pipeline: {domain}")
        click.echo(f"{'─' * 60}")
        click.echo(f"  Targets:    {total_targets} total", nl=False)
        if checkpointed and not fresh:
            click.echo(f" ({len(checkpointed)} cached, {len(new_targets)} new)")
        else:
            click.echo(f" (all {'fresh' if fresh else 'new'})")

        if new_targets and len(new_targets) <= 10:
            for t in new_targets[:5]:
                click.echo(f"    → {t}")
            if len(new_targets) > 5:
                click.echo(f"    ... and {len(new_targets) - 5} more")
        elif new_targets:
            click.echo(f"    ({len(new_targets)} new targets to process)")

        if curated_count and not fresh:
            click.echo(f"  Curated:    {curated_count} docs (preserved from previous runs)")
        if extracted_count and not fresh and not reprocess:
            click.echo(f"  Extracted:  {extracted_count} docs (will skip existing)")
        if fresh:
            click.echo(f"  Mode:       FRESH (all caches cleared)")
        click.echo(f"{'─' * 60}")

    # --- Confirm ---
    if not yes and not quiet:
        if not click.confirm("  Proceed?", default=True):
            click.echo("  Cancelled.")
            sys.exit(0)

    # --- Fresh mode: clear caches ---
    if fresh:
        if checkpoint_path.exists():
            checkpoint_path.unlink()
        if not quiet:
            click.echo("  ✓ Checkpoint cleared")
        reprocess = True  # force re-extraction

    # --- Run stages ---
    base_cmd = ["pixi", "run", "psweep"]
    flags = ["-q"] if quiet else []

    stages = []
    if not skip_discover:
        stages.append(("discover", base_cmd + ["discover", "--config", config_path] + flags))
    if not skip_extract:
        extract_flags = flags + (["--reprocess"] if reprocess else [])
        stages.append(("extract", base_cmd + ["extract", "--config", config_path] + extract_flags))
    stages.append(("compile", base_cmd + ["compile", "--config", config_path] + flags))

    for stage_name, cmd in stages:
        if not quiet:
            click.echo(f"\n{'─' * 60}")
            click.echo(f"  Stage: {stage_name}")
            click.echo(f"{'─' * 60}\n")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            click.echo(f"\n✗ Stage '{stage_name}' failed (exit {result.returncode})", err=True)
            sys.exit(result.returncode)

    if not quiet:
        click.echo(f"\n{'─' * 60}")
        click.echo("  ✓ Pipeline complete: discover → extract → compile")
        click.echo(f"{'─' * 60}\n")


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
