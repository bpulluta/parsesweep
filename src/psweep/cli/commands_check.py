"""`check` command."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from jsonschema import (
    FormatChecker,
    SchemaError,
    ValidationError,
    validate as json_validate,
)

from psweep.exceptions import (
    ConfigurationError,
    SchemaError as ExtractionSchemaError,
)

from psweep.extraction import load_schema
from psweep.utils.config import get_config


@click.command()
@click.argument("extraction_file", type=click.Path(exists=True))
@click.option(
    "--schema",
    type=click.Path(exists=True, dir_okay=False),
    help="Schema to check against (defaults to the record's lineage schema).",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.option(
    "--show-data",
    is_flag=True,
    help="Display extracted data with syntax highlighting",
)
def check(
    extraction_file: str,
    schema: str | None,
    verbose: bool,
    show_data: bool,
) -> None:
    """
    Check an extraction result against the schema.

    \b
    EXAMPLES:
        psweep check extracted/data/doc.json
        psweep check extracted/data/doc.json --show-data
    """
    from psweep.cli.commands import begin_run
    from psweep.cli.ui import console, display_json

    extraction_file_path = Path(extraction_file)
    view = begin_run("check", verbose=verbose)
    view.header(f"Validation: {extraction_file_path.name}")

    try:
        with extraction_file_path.open() as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        view.error("Invalid JSON file", str(exc))
        sys.exit(1)

    if not isinstance(data, dict):
        view.error("Invalid extraction record", "Expected a JSON object")
        sys.exit(1)
    lineage = data.get("lineage")
    recorded_schema = (
        lineage.get("schema_id") if isinstance(lineage, dict) else None
    )
    schema_ref = schema or recorded_schema or get_config().default_schema
    schema_path = Path(schema_ref)
    if not schema_path.exists():
        view.error(
            "Schema not found",
            f"{schema_path}; supply --schema for this record",
        )
        sys.exit(1)
    try:
        loaded_schema = load_schema(schema_path)
    except (ConfigurationError, ExtractionSchemaError) as exc:
        view.error("Cannot load schema", str(exc))
        sys.exit(1)

    try:
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise ValidationError(
                "Extraction file must use canonical extraction-record format with a 'payload' object"
            )
        json_validate(
            instance=payload,
            schema=loaded_schema,
            format_checker=FormatChecker(),
        )
        view.status("success", "Schema validation passed")
    except (SchemaError, ValidationError) as exc:
        view.error("Schema validation failed", exc.message)
        sys.exit(1)

    if verbose or show_data:
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        if "item_count" in data:
            item_count = data.get("item_count", 0)
        else:
            item_count = 0
            for value in payload.values():
                if isinstance(value, list):
                    item_count = max(item_count, len(value))

        cost = data.get("processing_metrics", {}).get("cost_usd")
        metadata = {
            "Source File": data.get("document", {}).get(
                "source_filename", "N/A"
            ),
            "Extraction Date": data.get("lineage", {}).get(
                "extracted_at", "N/A"
            ),
            "Model": data.get("lineage", {}).get("model", "N/A"),
            "Cost": "unknown" if cost is None else f"${cost:.4f}",
            "Processing Time": f"{data.get('processing_metrics', {}).get('duration_seconds', 0):.1f}s",
            "Item Count": str(item_count),
        }
        view.summary(metadata, title="Extraction Metadata")

    if show_data and "payload" in data:
        console.print()
        display_json(data.get("payload"), title="Extracted Data")

    view.next_steps(
        [
            f"Compile the folder: pixi run psweep compile {extraction_file_path.parent} --schema <schema>",
        ]
    )
