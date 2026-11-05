"""`extract` command and helpers."""

from __future__ import annotations

import json
import sys
from typing import Optional

import click
from dotenv import load_dotenv

from psweep.config import RuntimeConfigError
from psweep.exceptions import ParseSweepError
from psweep.extraction.document_utils import SUPPORTED_EXTENSIONS
from psweep.extraction.llm_factory import DEFAULT_MAX_CONTEXT
from psweep.pipeline import _execute_extraction, _prepare_extraction


def _render_extraction(plan, view):
    """Keep progress display outside shared extraction execution."""

    def progress(doc_path, result):
        if view.is_quiet or result is None:
            return
        if result["success"]:
            view.success(f"{doc_path.name}: {result['items']} items")
        else:
            view.error(
                f"Extraction failed for {doc_path.name}", result["error"]
            )

    return _execute_extraction(plan, on_progress=progress)


@click.command(
    epilog=f"Supported file types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
)
@click.argument("path", type=click.Path(), required=False)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True),
    default=None,
    help="Domain run config file (RECOMMENDED — schema, model, and paths derive from it)",
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
    help="Validate resolved command inputs and exit without extracting",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output directory (default: outputs/<domain>/extracted)",
)
@click.option(
    "--schema",
    "-s",
    type=click.Path(exists=True),
    required=False,
    help="Schema file (for quick testing without a config YAML)",
)
@click.option(
    "--model",
    default=None,
    help="LLM model name, or an alias defined in the run.yaml models: block. "
    "For Azure, use your deployment name (e.g. gpt-4o).",
)
@click.option(
    "--provider",
    type=click.Choice(
        ["openai", "azure", "anthropic", "gemini", "auto"],
        case_sensitive=False,
    ),
    default="auto",
    show_default=True,
    help="LLM provider (auto-detects from .env)",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(min=1),
    help="Process only first N pending files",
)
@click.option(
    "--fresh",
    is_flag=True,
    default=False,
    show_default=True,
    help="Reset the entire resolved extraction output "
    "after successful preflight and re-extract the selected documents. "
    "Valid raw OCR (.text) and targeting (.pages/.sections) "
    "caches are reused.",
)
@click.option(
    "--max-context",
    type=click.IntRange(min=1),
    default=DEFAULT_MAX_CONTEXT,
    show_default=True,
    help="Max document characters to process",
)
@click.option(
    "--quiet", "-q", is_flag=True, help="Minimal output (machine-readable)"
)
@click.option(
    "--verbose", "-v", is_flag=True, help="Detailed output with statistics"
)
@click.option("--debug", is_flag=True, help="Debug mode with full logs")
@click.option(
    "--pages",
    type=str,
    default=None,
    help='Page range to extract (e.g., "615-759"). Only for single PDF files.',
)
@click.option(
    "--pages-csv",
    type=click.Path(exists=True),
    default=None,
    help="CSV file mapping documents to page ranges",
)
def extract(
    path: Optional[str],
    config_path: Optional[str],
    show_effective_config: bool,
    validate_config_only: bool,
    output: Optional[str],
    schema: Optional[str],
    model: str,
    provider: str,
    limit: Optional[int],
    fresh: bool,
    max_context: int,
    quiet: bool,
    verbose: bool,
    debug: bool,
    pages: Optional[str],
    pages_csv: Optional[str],
):
    """Extract structured data from documents using an LLM.

    Reads every supported file type (listed below; recordings are
    transcribed) from PATH (default: the domain's curated discovery
    directory) and writes one JSON file per document to the output directory.
    Already-processed files are skipped by default; pass --fresh to re-extract
    documents that already have output JSON (cached text/sidecars are still
    reused). Output defaults to ``outputs/<domain>/extracted/`` when --output is omitted;
    run ``psweep extract --help`` for every option and its default.

    Examples
    --------
    ::

        psweep extract --config config/my_domain/run.yaml --fresh
        psweep extract docs/ --schema config/my_domain/schema.json
        psweep extract docs/ --schema s.json --model gpt-4o --provider azure
    """
    from psweep.cli.commands import (
        _explicit_cli_overrides,
        _print_effective_config,
        _resolve_runtime_command_inputs,
        begin_run,
    )

    view = begin_run("extract", quiet=quiet, verbose=verbose, debug=debug)

    load_dotenv()
    cli_overrides = _explicit_cli_overrides(
        [
            "path",
            "schema",
            "output",
            "pages_csv",
            "pages",
            "provider",
            "model",
            "limit",
            "max_context",
            "fresh",
        ]
    )

    try:
        resolved_inputs = _resolve_runtime_command_inputs(
            command_name="extract",
            config_path=config_path,
            cli_values=cli_overrides,
        )
    except RuntimeConfigError as exc:
        view.error("Runtime config resolution failed", str(exc))
        sys.exit(1)

    if show_effective_config and not view.is_quiet:
        _print_effective_config("extract", resolved_inputs)
    resolved_inputs.setdefault("max_context", max_context)
    resolved_inputs.setdefault("provider", provider)
    try:
        plan = _prepare_extraction(
            resolved_inputs,
            fresh=fresh,
            config_path=config_path,
            validate_only=validate_config_only,
        )
        if validate_config_only:
            if view.is_quiet:
                click.echo(
                    json.dumps(
                        {
                            "command": "extract",
                            "status": "valid",
                            "resolved": {
                                k: v
                                for k, v in resolved_inputs.items()
                                if not k.startswith("_")
                            },
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                view.success(
                    "Runtime config validation passed for extract command"
                )
            return
        view.header("DOCUMENT EXTRACTION")
        view.config(
            {
                "Input": str(resolved_inputs["path"]),
                "Files": str(len(plan["documents"])),
                "Model": plan["llm"]["model"],
                "Output": str(plan["output"]),
            }
        )

        outcome = _render_extraction(plan, view)
    except (ParseSweepError, OSError, ValueError, TypeError) as exc:
        view.error("Extraction failed", str(exc))
        raise click.exceptions.Exit(1) from exc
    if view.is_quiet:
        click.echo(str(outcome.manifest_path))
    else:
        if outcome.status == "reused":
            view.success(
                f"All {outcome.successful} file(s) already processed "
                "(verified reuse)"
            )
        view.summary(
            {
                "Status": outcome.status,
                "Successful": str(outcome.successful),
                "Failed": str(outcome.failed),
                "Items": str(outcome.total_items),
                "Known Cost": f"${outcome.total_cost:.4f}",
                "Unknown Cost Calls": str(outcome.unknown_cost_calls),
            },
            title="Extraction Summary",
        )
        view.outputs(
            {
                "Extracted data": str(outcome.output_dir),
                "Run manifest": str(outcome.manifest_path),
            }
        )
        if outcome.exit_code == 0:
            compile_cmd = (
                f"pixi run psweep compile --config {config_path}"
                if config_path
                else (
                    f"pixi run psweep compile {outcome.output_dir} "
                    f"--schema {schema}"
                )
            )
            view.next_steps([f"Compile into a spreadsheet: {compile_cmd}"])
    raise click.exceptions.Exit(outcome.exit_code)
