"""
Utility CLI commands for ParseSweep.

This module contains helper and setup commands:
- init: Interactive project setup wizard
- preview: Preview document before extraction
- estimate: Estimate cost and time for batch extraction
- check-schema: Check JSON schema files
- check-runtime: Check runtime onboarding readiness
- config: Show current configuration

For core workflow commands (extract, compile), see commands.py
"""

import json
import os
from pathlib import Path
from typing import Optional

import click
from dotenv import load_dotenv

from psweep.config import VARIABLE_CATALOG
from psweep.utils.config import get_config
from psweep.extraction import load_schema
from psweep.extraction.document_utils import (
    SUPPORTED_EXTENSIONS,
    extract_text_from_document,
    is_supported_document,
)
from psweep.extraction.llm_factory import DEFAULT_MODEL
from psweep.cli.ui import (
    ask_choice,
    ask_confirm,
    ask_text,
    console,
    create_file_tree,
    key_values,
    print_cost_estimate,
    print_error,
    print_header,
    print_info,
    print_next_steps,
    print_success,
    print_warning,
    rule,
    section,
    status_item,
)
from psweep.utils.page_range import load_pages_csv
from psweep.cli.cost_tracker import estimate_extraction_cost
from psweep.scaffolding import (
    ConfigValidationError,
    _available_main_array_fields,
    _build_schema_starter_from_reference,
    _default_schema_output_path,
    _display_cli_path,
    _write_schema_starter,
)


def _cli_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@click.command("init-domain-schema")
@click.option(
    "--interactive",
    is_flag=True,
    help="Prompt for missing starter-schema values",
)
@click.option(
    "--name", "schema_name", help="Base schema name (used for the output file)"
)
@click.option(
    "--reference-schema",
    type=click.Path(exists=True),
    help="Existing schema file used as the closest reference for a lean starter",
)
@click.option(
    "--domain", "domain_name", help="Override the metadata domain label"
)
@click.option(
    "--document-type", help="Override the human-readable document type"
)
@click.option(
    "--include-field",
    "include_fields",
    multiple=True,
    help="Limit the starter schema to these main-array item fields; repeat the option to keep multiple fields",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False),
    help="Output schema path (defaults to schemas/personal/<name>_schema.json)",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing schema file if it already exists",
)
@click.option(
    "--report-format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format for scaffold results",
)
def init_domain_schema_cmd(
    interactive: bool,
    schema_name: Optional[str],
    reference_schema: Optional[str],
    domain_name: Optional[str],
    document_type: Optional[str],
    include_fields: tuple[str, ...],
    output: Optional[str],
    force: bool,
    report_format: str,
):
    """
    Create a lean starter schema for a new domain.

    This command is the documents-first entry point for greenfield onboarding.
    It derives a compact starter schema from the closest existing reference schema,
    preserving only the minimal extraction contract and core row shape needed for
    the first smoke extraction pass.
    """
    repo_root = _cli_repo_root()

    try:
        if interactive and report_format == "json":
            raise ConfigValidationError(
                "--interactive only supports text output; omit --report-format json"
            )

        if interactive:
            section("Interactive Domain Schema Setup")
            console.print(
                "Answer the prompts to create a lean starter schema.\n"
            )

        if interactive and not schema_name:
            schema_name = ask_text("Schema name")
        if interactive and not reference_schema:
            reference_schema = ask_text("Reference schema path")
        if interactive and document_type is None:
            if ask_confirm(
                "Override the reference document type?", default=False
            ):
                document_type = ask_text("Document type")
        if interactive and domain_name is None:
            if ask_confirm(
                "Override the reference domain label?", default=False
            ):
                domain_name = ask_text("Domain label")

        if not schema_name:
            raise ConfigValidationError(
                "init-domain-schema requires --name unless --interactive is used"
            )
        if not reference_schema:
            raise ConfigValidationError(
                "init-domain-schema requires --reference-schema unless --interactive is used"
            )

        output_path = (
            Path(output).resolve()
            if output
            else _default_schema_output_path(repo_root, schema_name)
        )
        reference_schema_path = Path(reference_schema).resolve()
        selected_fields = list(include_fields)

        schema_data = _build_schema_starter_from_reference(
            reference_schema_path,
            schema_name=schema_name,
            domain_name=domain_name,
            document_type=document_type,
            include_fields=selected_fields or None,
        )
        _write_schema_starter(output_path, schema_data, force=force)

        main_item_fields = _available_main_array_fields(schema_data)
        result = {
            "status": "ready",
            "created": {
                "schema_name": schema_name,
                "schema_path": output_path.as_posix(),
                "reference_schema": reference_schema_path.as_posix(),
                "domain": schema_data["$metadata"]["domain"],
                "document_type": schema_data["$metadata"]["extraction"][
                    "document_type"
                ],
                "main_data_array": schema_data["$metadata"]["extraction"][
                    "main_data_array"
                ],
                "identifier_fields": schema_data["$metadata"]["extraction"][
                    "identifier_fields"
                ],
                "selected_fields": main_item_fields,
                "interactive": interactive,
                "force": force,
            },
            "next_steps": [
                {
                    "title": "Validate starter schema",
                    "command": f"pixi run psweep check-schema {_display_cli_path(output_path, repo_root)}",
                },
                {
                    "title": "Create runtime config from template",
                    "command": (
                        f"mkdir -p config/{schema_name.replace('_schema', '')} && "
                        f"cp config/TEMPLATE.yaml config/{schema_name.replace('_schema', '')}/run.yaml"
                    ),
                },
            ],
        }

        if report_format == "json":
            click.echo(json.dumps(result, indent=2))
            return

        print_header("Init Domain Schema")
        print_success("Lean starter schema created")
        console.print(
            key_values(
                {
                    "Schema Name": schema_name,
                    "Schema Path": output_path.as_posix(),
                    "Reference Schema": reference_schema_path.as_posix(),
                    "Domain": schema_data["$metadata"]["domain"],
                    "Document Type": schema_data["$metadata"]["extraction"][
                        "document_type"
                    ],
                    "Main Data Array": schema_data["$metadata"]["extraction"][
                        "main_data_array"
                    ],
                    "Selected Fields": ", ".join(main_item_fields),
                }
            )
        )
        print_next_steps(
            [
                f'{step["title"]}\n     {step["command"]}'
                for step in result["next_steps"]
            ]
        )
        console.print()
    except ConfigValidationError as exc:
        error_report = {
            "status": "error",
            "error": {
                "category": "domain_schema_init",
                "message": str(exc),
            },
            "target": {
                "schema_name": schema_name,
                "reference_schema": reference_schema,
                "output": output,
            },
        }
        if report_format == "json":
            click.echo(json.dumps(error_report, indent=2))
        else:
            print_header("Init Domain Schema")
            print_error("Schema scaffold failed", str(exc))
        raise click.exceptions.Exit(1)


@click.command()
def init():
    """
    Interactive project setup wizard.

    Guides you through setting up your ParseSweep project including:
    - API credentials configuration
    - Document type selection
    - Schema selection
    - Directory structure creation

    \b
    EXAMPLE:
        psweep init
    """
    print_header("ParseSweep Setup Wizard")

    console.print("Let's set up your project!\n")

    # Check if .env already exists
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        if not ask_confirm(
            f".env file already exists at {env_path}. Overwrite?",
            default=False,
        ):
            print_info("Keeping existing .env file")
            return

    # API Provider Selection
    section("Step 1: API Configuration")
    provider = ask_choice(
        "Select API provider", choices=["azure", "openai"], default="azure"
    )

    env_content = []

    if provider == "azure":
        section("Azure OpenAI Configuration")
        api_key = ask_text("Azure OpenAI API Key")
        endpoint = ask_text(
            "Azure OpenAI Endpoint",
            default="https://your-endpoint.openai.azure.com/",
        )
        deployment = ask_text("Model Deployment Name", default=DEFAULT_MODEL)

        env_content = [
            "# Azure OpenAI Configuration",
            f"AZURE_OPENAI_API_KEY={api_key}",
            f"AZURE_OPENAI_ENDPOINT={endpoint}",
            f"AZURE_OPENAI_MODEL={deployment}",
            "AZURE_OPENAI_API_VERSION=2025-04-01-preview",
        ]
    else:
        section("OpenAI Configuration")
        api_key = ask_text("OpenAI API Key (starts with sk-)")

        env_content = [
            "# OpenAI Configuration",
            f"OPENAI_API_KEY={api_key}",
        ]

    # Write .env file
    with open(env_path, "w") as f:
        f.write("\n".join(env_content) + "\n")

    print_success(f"Created .env file at {env_path}")

    # Document Type Selection
    section("Step 2: Document Type")
    doc_type = ask_choice(
        "What type of documents will you process?",
        choices=["tariffs", "ordinances", "permits", "custom"],
        default="tariffs",
    )

    # Create directory structure
    section("Step 3: Directory Structure")

    project_root = Path.cwd()
    docs_dir = project_root / "documents" / doc_type
    processed_dir = project_root / "extracted" / doc_type
    compiled_dir = project_root / "compiled" / doc_type

    docs_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    compiled_dir.mkdir(parents=True, exist_ok=True)

    print_success("Created directory structure:")
    console.print(
        create_file_tree(project_root / "documents", "Project Structure")
    )

    # Schema selection
    section("Step 4: Schema Selection")

    config = get_config()
    schemas = list(config.schema_dir.glob("*.json"))

    if schemas:
        schema_names = [s.stem for s in schemas]

        # Try to auto-detect based on doc_type via fuzzy stem matching
        import difflib

        suggested_schema = None
        close = difflib.get_close_matches(doc_type, schema_names, n=1, cutoff=0.3)
        if close:
            suggested_schema = close[0]

        if suggested_schema:
            print_info(f"Suggested schema: {suggested_schema}")

        if len(schema_names) > 1:
            console.print(f"\nAvailable schemas: {', '.join(schema_names)}")

    # Summary
    rule()
    print_success("Setup complete!")
    print_next_steps(
        [
            f"Add your documents to: {docs_dir}",
            f"Run extraction: psweep extract {docs_dir}",
            f"Compile results: psweep compile {processed_dir}",
        ]
    )
    console.print()


@click.command()
@click.argument("document_path", type=click.Path(exists=True))
def preview(document_path: str):
    """
    Preview a document before extraction.

    Shows document information, estimated costs, and sample content
    without performing the full extraction.

    \b
    EXAMPLES:
        psweep preview documents/sample.pdf
        psweep preview documents/tariffs/rate_schedule.docx
    """
    doc_path = Path(document_path)

    # Validate file type
    if not is_supported_document(doc_path):
        supported = ", ".join(SUPPORTED_EXTENSIONS)
        print_error(
            f"Unsupported file type: {doc_path.suffix}",
            f"Supported formats: {supported}",
        )
        return

    print_header(f"Document Preview: {doc_path.name}")

    # Get file info
    file_size = doc_path.stat().st_size
    file_size_mb = file_size / (1024 * 1024)

    section("Document Info")
    info_table = key_values(
        {
            "File": doc_path.name,
            "Size": f"{file_size_mb:.2f} MB",
            "Type": doc_path.suffix.upper(),
        }
    )
    console.print(info_table)

    # Extract text to analyze
    try:
        print_info("Analyzing document...")
        text = extract_text_from_document(doc_path)
        text_length = len(text)

        # Estimate cost/time using the configured model's pricing (shared helper).
        config = get_config()
        model_name = config.llm_config.get("model", DEFAULT_MODEL)
        est = estimate_extraction_cost(text_length, model_name)
        estimated_tokens = est.input_tokens
        total_cost = est.total_cost

        section("Content Analysis")
        analysis_table = key_values(
            {
                "Text Length": f"{text_length:,} characters",
                "Estimated Tokens": f"~{estimated_tokens:,}",
            }
        )
        console.print(analysis_table)

        # Try to detect schema via fuzzy stem matching
        matched_schema = None

        candidates = sorted(config.schema_dir.glob("*.json"), key=lambda p: p.stem)
        if candidates:
            import difflib

            stems = [c.stem for c in candidates]
            close = difflib.get_close_matches(
                Path(doc_path).stem, stems, n=1, cutoff=0.3
            )
            if close:
                matched_schema = next(c for c in candidates if c.stem == close[0])
            else:
                matched_schema = candidates[0]

        if matched_schema:
            section("Schema Detection")
            status_item("success", f"Matched schema: {matched_schema.name}")

            # Load and show field count
            schema = load_schema(matched_schema)
            if "properties" in schema:
                field_count = len(schema["properties"])
                status_item("info", f"Expected fields: {field_count}")

        # Cost estimate
        print_cost_estimate(
            total_docs=1,
            estimated_tokens=estimated_tokens,
            estimated_cost=total_cost,
            estimated_time=est.estimated_minutes,
            model=model_name,
        )
        section("Sample Content Preview")
        sample = text[:500].replace("\n", " ")
        console.print(f"{sample}...\n")

        section("Ready to Extract")
        console.print(
            f"Run: psweep extract {doc_path} --schema schemas/example_utility_rate_schema.json\n"
        )

    except Exception as e:
        print_error("Failed to analyze document", str(e))


@click.command()
@click.argument("documents_path", type=click.Path(exists=True))
@click.option(
    "--workers", "-w", type=int, default=4, help="Number of parallel workers"
)
@click.option(
    "--pages-csv",
    type=click.Path(exists=True),
    default=None,
    help="CSV file mapping documents to page ranges",
)
def estimate(documents_path: str, workers: int, pages_csv: str):
    """
    Estimate cost and time for batch document extraction.

    Analyzes all documents in a directory and provides detailed
    cost and time estimates before you commit to processing.

    \b
    EXAMPLES:
        psweep estimate documents/tariffs/
        psweep estimate documents/permits/ --workers 8
        psweep estimate documents/tariffs/ --pages-csv config/tariffs/page_ranges.csv
    """
    docs_path = Path(documents_path)

    if not docs_path.exists():
        print_error(f"Path not found: {docs_path}")
        return

    print_header("Cost & Time Estimation")

    # Gather all documents
    if docs_path.is_file():
        if is_supported_document(docs_path):
            doc_files = [docs_path]
        else:
            print_error(f"Unsupported file type: {docs_path.suffix}")
            return
    else:
        doc_files = []
        for ext in SUPPORTED_EXTENSIONS:
            doc_files.extend(sorted(docs_path.rglob(f"*{ext}")))

        if not doc_files:
            supported = ", ".join(SUPPORTED_EXTENSIONS)
            print_error(
                f"No supported documents found in {docs_path}",
                f"Supported formats: {supported}",
            )
            return

    print_info(f"Analyzing {len(doc_files)} document(s)...")

    # Load page ranges if provided
    page_range_map = {}
    if pages_csv:
        from psweep.utils.page_range import load_pages_csv

        try:
            page_mappings = load_pages_csv(Path(pages_csv))

            # Match file names from doc_files to mappings
            for doc in doc_files:
                # Try exact match first
                if str(doc) in page_mappings:
                    page_range_map[doc] = page_mappings[str(doc)]
                elif doc.name in page_mappings:
                    page_range_map[doc] = page_mappings[doc.name]

            mapped_count = sum(
                1 for v in page_range_map.values() if v is not None
            )
            print_info(
                f"Loaded page ranges for {mapped_count} file(s) from CSV"
            )
        except Exception as e:
            print_error(f"Failed to load page ranges CSV: {e}")
            return

    # Analyze first few files to estimate average
    sample_size = min(5, len(doc_files))
    total_chars = 0

    for doc in doc_files[:sample_size]:
        try:
            page_range = page_range_map.get(doc) if pages_csv else None
            text = extract_text_from_document(doc, page_range=page_range)
            total_chars += len(text)
        except Exception:
            pass

    if total_chars == 0:
        print_warning("Could not analyze documents for estimation")
        return

    # Calculate averages and estimates (shared estimator, configured model).
    avg_chars = total_chars / sample_size
    estimated_total_chars = avg_chars * len(doc_files)

    config = get_config()
    model_name = config.llm_config.get("model", DEFAULT_MODEL)

    est = estimate_extraction_cost(
        estimated_total_chars, model_name, workers=workers
    )
    estimated_tokens = est.input_tokens
    input_cost_per_1m = est.input_rate
    output_cost_per_1m = est.output_rate
    output_tokens = est.output_tokens
    total_cost = est.total_cost
    estimated_minutes = est.estimated_minutes

    # Display analysis
    section("Document Analysis")
    doc_info = {
        "Total Documents": str(len(doc_files)),
        "Sample Analyzed": f"{sample_size} files",
        "Avg Characters/Doc": f"{avg_chars:,.0f}",
        "Estimated Total Tokens": f"~{estimated_tokens:,}",
    }

    # Add page range info if applicable
    if pages_csv and page_range_map:
        files_with_ranges = sum(
            1 for v in page_range_map.values() if v is not None
        )
        if files_with_ranges > 0:
            doc_info["Page Ranges"] = (
                f"{files_with_ranges} file(s) with specific ranges"
            )

    doc_table = key_values(doc_info)
    console.print(doc_table)

    # Display cost breakdown
    section(f"Cost Breakdown ({model_name})")
    cost_table = key_values(
        {
            "Input Tokens": f"~{estimated_tokens:,} @ ${input_cost_per_1m:.2f}/1M",
            "Output Tokens": f"~{int(output_tokens):,} @ ${output_cost_per_1m:.2f}/1M",
            "Total Estimated Cost": f"${total_cost:.2f}",
        }
    )
    console.print(cost_table)

    # Display time estimate
    section("Time Estimate")
    time_table = key_values(
        {
            "Workers": str(workers),
            "Processing Time": f"~{estimated_minutes:.1f} minutes",
        }
    )
    console.print(time_table)

    # Confirmation prompt
    console.print()
    if ask_confirm("Proceed with extraction?", default=False):
        console.print(
            f"\nRun: psweep extract {docs_path} --schema schemas/example_utility_rate_schema.json\n"
        )
    else:
        print_info("Operation cancelled")


@click.command("check-schema")
@click.argument("schema_path", type=click.Path(exists=True))
def check_schema_cmd(schema_path: str):
    """
    Check a JSON schema file.

    Checks schema syntax, structure, and ParseSweep-specific
    metadata requirements.

    \b
    EXAMPLES:
        psweep check-schema schemas/my_schema.json
        psweep check-schema schemas/example_utility_rate_schema.json
    """
    schema_file = Path(schema_path)

    print_header(f"Validating Schema: {schema_file.name}")

    # Load schema
    try:
        with open(schema_file) as f:
            schema = json.load(f)
        print_success("Valid JSON syntax")
    except json.JSONDecodeError as e:
        print_error("Invalid JSON syntax", str(e))
        return

    issues = []
    warnings = []

    # Check JSON Schema structure
    if "$schema" in schema:
        print_success("Has $schema declaration")
    else:
        warnings.append("Missing $schema declaration (recommended)")

    if "type" in schema:
        print_success(f"Schema type: {schema['type']}")
    else:
        issues.append("Missing 'type' field (required)")

    if "properties" in schema:
        field_count = len(schema["properties"])
        print_success(f"Has {field_count} properties defined")
    else:
        issues.append("Missing 'properties' field (required)")

    # Check ParseSweep metadata
    section("ParseSweep Metadata")

    if "$metadata" in schema:
        metadata = schema["$metadata"]
        extraction_metadata = (
            metadata.get("extraction")
            if isinstance(metadata.get("extraction"), dict)
            else {}
        )
        identity_metadata = (
            metadata.get("identity")
            if isinstance(metadata.get("identity"), dict)
            else {}
        )
        compilation_metadata = (
            metadata.get("compilation")
            if isinstance(metadata.get("compilation"), dict)
            else {}
        )
        deduplication_metadata = (
            identity_metadata.get("deduplication")
            if isinstance(identity_metadata.get("deduplication"), dict)
            else {}
        )
        print_success("Has $metadata section")

        if extraction_metadata.get("identifier_fields"):
            id_fields = extraction_metadata["identifier_fields"]
            status_item(
                "info", f"Identifier fields: {', '.join(id_fields)}"
            )
        else:
            warnings.append(
                "$metadata.extraction missing 'identifier_fields' (required for runtime document identification)"
            )

        if extraction_metadata.get("main_data_array"):
            main_array = extraction_metadata["main_data_array"]
            status_item("info", f"Main data array: {main_array}")
        else:
            warnings.append(
                "$metadata.extraction missing 'main_data_array' (required for extraction row generation)"
            )

        if deduplication_metadata:
            key_fields = deduplication_metadata.get("key_fields") or []
            if isinstance(key_fields, list) and key_fields:
                status_item(
                    "info",
                    f"Deduplication key fields: {', '.join(key_fields)}",
                )
            else:
                status_item("info", "Deduplication configured")
        else:
            status_item(
                "info", "No deduplication config (optional in starter schemas)"
            )

        if isinstance(compilation_metadata.get("deduplication"), dict):
            issues.append(
                "$metadata.compilation.deduplication is deprecated; move it to $metadata.identity.deduplication"
            )

        # Enforce a clean ownership boundary to reduce user confusion.
        if isinstance(compilation_metadata.get("output"), dict):
            warnings.append(
                "$metadata.compilation.output is runtime-owned and should be moved to config/<domain>/run.yaml under compilation.output"
            )
        if isinstance(compilation_metadata.get("normalization"), dict):
            warnings.append(
                "$metadata.compilation.normalization is runtime-owned and should be moved to config/<domain>/run.yaml under compilation.normalization"
            )
        if isinstance(compilation_metadata.get("value_typing"), dict):
            warnings.append(
                "$metadata.compilation.value_typing is deprecated; keep vocabularies in schema properties[*].enum"
            )
        if isinstance(metadata.get("validation"), dict):
            warnings.append(
                "$metadata.validation is deprecated in this runtime; use schema-level JSON Schema constraints (required/type/enum) and runtime QA/QC config"
            )
        if isinstance(metadata.get("qa_qc"), dict):
            warnings.append(
                "$metadata.qa_qc is deprecated for active workflows; define QA/QC settings in config/<domain>/run.yaml under 'validation'"
            )
    else:
        issues.append(
            "Missing $metadata section (required for the modernized runtime)"
        )

    # Summary
    rule()

    if issues:
        section("Issues Found")
        for issue in issues:
            status_item("error", issue)
        console.print()
    elif warnings:
        section("Recommendations")
        for warning in warnings:
            status_item("warning", warning)
        console.print()
        print_success("Schema is valid but could be improved!")
    else:
        print_success("Schema is perfect!")

    console.print()


@click.command()
@click.option(
    "--show-runtime-catalog",
    is_flag=True,
    help="Display runtime variable catalog grouped by required/optional/advanced",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"], case_sensitive=False),
    default="text",
    show_default=True,
    help="Output format for catalog rendering",
)
def config(show_runtime_catalog: bool, output_format: str):
    """
    Show current configuration.

    Displays environment settings, paths, and available schemas.

    \b
    EXAMPLE:
        psweep config
        psweep config --show-runtime-catalog
    """
    if show_runtime_catalog and output_format == "json":
        click.echo(json.dumps(VARIABLE_CATALOG, indent=2, sort_keys=True))
        return

    load_dotenv()

    print_header("Configuration")

    # API Configuration
    section("API Configuration")

    azure_key = os.getenv("AZURE_OPENAI_API_KEY")
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    openai_key = os.getenv("OPENAI_API_KEY")

    if azure_key and azure_endpoint:
        console.print(
            key_values(
                {
                    "Provider": "Azure OpenAI",
                    "Endpoint": azure_endpoint,
                    "Model": os.getenv("AZURE_OPENAI_MODEL", "Not set"),
                    "API Key": f"{'*' * 20}...{azure_key[-4:]}",
                }
            )
        )
    elif openai_key:
        console.print(
            key_values(
                {
                    "Provider": "OpenAI",
                    "API Key": f"{'*' * 20}...{openai_key[-4:]}",
                }
            )
        )
    else:
        print_warning(
            "No API credentials configured",
            "Run 'psweep init' to set up",
        )

    # Paths
    config_obj = get_config()
    section("Paths")
    console.print(
        key_values(
            {
                "Project Root": str(config_obj.project_root),
                "Schemas": str(config_obj.schema_dir),
            }
        )
    )

    # Available schemas
    schemas = list(config_obj.schema_dir.glob("*.json"))
    if schemas:
        section("Available Schemas")
        for schema in schemas:
            status_item("info", schema.name)

    if show_runtime_catalog:
        section("Runtime Variable Catalog")
        for section_name in ("extraction", "compilation", "discovery"):
            entries = VARIABLE_CATALOG.get(section_name, [])
            if not entries:
                continue

            section(section_name.title())
            grouped = {"required": [], "optional": [], "advanced": []}
            for entry in entries:
                grouped.setdefault(entry.get("level", "optional"), []).append(
                    entry
                )

            for level in ("required", "optional", "advanced"):
                level_entries = grouped.get(level) or []
                if not level_entries:
                    continue
                section(level.title())
                for entry in level_entries:
                    status_item(
                        "info",
                        f"{entry['name']}: {entry['description']}",
                    )

    console.print()
