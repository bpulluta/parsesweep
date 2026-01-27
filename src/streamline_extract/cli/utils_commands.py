"""
Utility CLI commands for StreamlineExtract.

This module contains helper and setup commands:
- init: Interactive project setup wizard
- preview: Preview document before processing
- estimate: Estimate cost and time for batch processing
- validate_schema: Validate JSON schema files
- config: Show current configuration

For core workflow commands (process, consolidate), see commands.py
"""

import json
import os
from pathlib import Path
from typing import Optional

import click
from dotenv import load_dotenv

from streamline_extract.utils.config import get_config
from streamline_extract.extraction import load_schema
from streamline_extract.extraction.document_utils import (
    extract_text_from_document,
    is_supported_document,
    SUPPORTED_EXTENSIONS
)
from streamline_extract.cli.ui import (
    console,
    print_header,
    print_error,
    print_success,
    print_info,
    print_warning,
    print_cost_estimate,
    ask_choice,
    ask_confirm,
    ask_text,
    create_file_tree,
    create_config_table,
    display_json,
)


@click.command()
def init():
    """
    Interactive project setup wizard.
    
    Guides you through setting up your StreamlineExtract project including:
    - API credentials configuration
    - Document type selection
    - Schema selection
    - Directory structure creation
    
    \b
    EXAMPLE:
        streamline-extract init
    """
    print_header("🚀 StreamlineExtract Setup Wizard")
    
    console.print("Let's set up your project!\n")
    
    # Check if .env already exists
    env_path = Path.cwd() / '.env'
    if env_path.exists():
        if not ask_confirm(f".env file already exists at {env_path}. Overwrite?", default=False):
            console.print("[yellow]Keeping existing .env file[/yellow]\n")
            return
    
    # API Provider Selection
    console.print("[bold]Step 1: API Configuration[/bold]")
    provider = ask_choice(
        "Select API provider",
        choices=["azure", "openai"],
        default="azure"
    )
    
    env_content = []
    
    if provider == "azure":
        console.print("\n[cyan]Azure OpenAI Configuration[/cyan]")
        api_key = ask_text("Azure OpenAI API Key")
        endpoint = ask_text("Azure OpenAI Endpoint", default="https://your-endpoint.openai.azure.com/")
        deployment = ask_text("Model Deployment Name", default="gpt-4o-mini")
        
        env_content = [
            "# Azure OpenAI Configuration",
            f"AZURE_OPENAI_API_KEY={api_key}",
            f"AZURE_OPENAI_ENDPOINT={endpoint}",
            f"AZURE_OPENAI_MODEL={deployment}",
            "AZURE_OPENAI_API_VERSION=2025-04-01-preview",
        ]
    else:
        console.print("\n[cyan]OpenAI Configuration[/cyan]")
        api_key = ask_text("OpenAI API Key (starts with sk-)")
        
        env_content = [
            "# OpenAI Configuration",
            f"OPENAI_API_KEY={api_key}",
        ]
    
    # Write .env file
    with open(env_path, 'w') as f:
        f.write('\n'.join(env_content) + '\n')
    
    print_success(f"Created .env file at {env_path}")
    
    # Document Type Selection
    console.print("\n[bold]Step 2: Document Type[/bold]")
    doc_type = ask_choice(
        "What type of documents will you process?",
        choices=["tariffs", "ordinances", "permits", "custom"],
        default="tariffs"
    )
    
    # Create directory structure
    console.print("\n[bold]Step 3: Directory Structure[/bold]")
    
    project_root = Path.cwd()
    docs_dir = project_root / 'documents' / doc_type
    processed_dir = project_root / 'processed' / doc_type
    consolidated_dir = project_root / 'consolidated' / doc_type
    
    docs_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    consolidated_dir.mkdir(parents=True, exist_ok=True)
    
    print_success("Created directory structure:")
    console.print(create_file_tree(project_root / 'documents', "Project Structure"))
    
    # Schema selection
    console.print("\n[bold]Step 4: Schema Selection[/bold]")
    
    config = get_config()
    schemas = list(config.schema_dir.glob("*.json"))
    
    if schemas:
        schema_names = [s.stem for s in schemas]
        
        # Try to auto-detect based on doc_type
        suggested_schema = None
        if doc_type == "tariffs" and any("tariff" in s for s in schema_names):
            suggested_schema = next(s for s in schema_names if "tariff" in s)
        elif doc_type == "ordinances" and any("ordinance" in s for s in schema_names):
            suggested_schema = next(s for s in schema_names if "ordinance" in s)
        
        if suggested_schema:
            console.print(f"[dim]Suggested schema: [cyan]{suggested_schema}[/cyan][/dim]")
        
        if len(schema_names) > 1:
            console.print(f"\nAvailable schemas: {', '.join(schema_names)}")
    
    # Summary
    console.print("\n" + "="*80)
    print_success("Setup complete! 🎉")
    console.print("\n[bold]Next steps:[/bold]")
    console.print(f"  1. Add your documents to: [cyan]{docs_dir}[/cyan]")
    console.print(f"  2. Run extraction: [green]streamline-extract process {docs_dir}[/green]")
    console.print(f"  3. Consolidate results: [green]streamline-extract consolidate {processed_dir}[/green]")
    console.print()


@click.command()
@click.argument('document_path', type=click.Path(exists=True))
def preview(document_path: str):
    """
    Preview a document before extraction.
    
    Shows document information, estimated costs, and sample content
    without performing the full extraction.
    
    \b
    EXAMPLES:
        streamline-extract preview documents/sample.pdf
        streamline-extract preview documents/tariffs/rate_schedule.docx
    """
    doc_path = Path(document_path)
    
    # Validate file type
    if not is_supported_document(doc_path):
        supported = ', '.join(SUPPORTED_EXTENSIONS)
        print_error(
            f"Unsupported file type: {doc_path.suffix}",
            f"Supported formats: {supported}"
        )
        return
    
    print_header(f"📄 Document Preview: {doc_path.name}")
    
    # Get file info
    file_size = doc_path.stat().st_size
    file_size_mb = file_size / (1024 * 1024)
    
    console.print("[bold]Document Info[/bold]")
    info_table = create_config_table("", {
        "File": doc_path.name,
        "Size": f"{file_size_mb:.2f} MB",
        "Type": doc_path.suffix.upper(),
    })
    console.print(info_table)
    
    # Extract text to analyze
    try:
        console.print("\n[dim]Analyzing document...[/dim]")
        text = extract_text_from_document(doc_path)
        text_length = len(text)
        
        # Estimate tokens (rough approximation: 4 chars per token)
        estimated_tokens = text_length // 4
        
        # Estimate cost for gpt-4o-mini
        # Input: $0.15/1M tokens, Output: $0.60/1M tokens
        # Assume output is ~10% of input
        input_cost = (estimated_tokens / 1_000_000) * 0.15
        output_cost = (estimated_tokens * 0.1 / 1_000_000) * 0.60
        total_cost = input_cost + output_cost
        
        # Estimate time (rough: 100 tokens per second)
        estimated_time = estimated_tokens / 100
        
        console.print("\n[bold]Content Analysis[/bold]")
        analysis_table = create_config_table("", {
            "Text Length": f"{text_length:,} characters",
            "Estimated Tokens": f"~{estimated_tokens:,}",
        })
        console.print(analysis_table)
        
        # Try to detect schema
        config = get_config()
        path_lower = str(doc_path).lower()
        matched_schema = None
        
        for candidate in config.schema_dir.glob("*.json"):
            candidate_name_lower = candidate.stem.lower()
            if any(keyword in path_lower for keyword in ['geothermal', 'ordinance']) and 'geothermal' in candidate_name_lower:
                matched_schema = candidate
                break
            elif any(keyword in path_lower for keyword in ['tariff', 'rate', 'electric']) and 'tariff' in candidate_name_lower:
                matched_schema = candidate
                break
        
        if matched_schema:
            console.print(f"\n[bold]Schema Detection[/bold]")
            console.print(f"  ✓ Matched schema: [cyan]{matched_schema.name}[/cyan]")
            
            # Load and show field count
            schema = load_schema(matched_schema)
            if 'properties' in schema:
                field_count = len(schema['properties'])
                console.print(f"  [dim]Expected fields: {field_count}[/dim]")
        
        # Cost estimate
        print_cost_estimate(
            total_docs=1,
            estimated_tokens=estimated_tokens,
            estimated_cost=total_cost,
            estimated_time=estimated_time / 60,  # Convert to minutes
            model="gpt-4o-mini"
        )
        
        # Show sample content
        console.print("[bold]Sample Content Preview[/bold]")
        sample = text[:500].replace('\n', ' ')
        console.print(f"[dim]{sample}...[/dim]\n")
        
        console.print("[bold green]Ready to extract?[/bold green]")
        console.print(f"Run: [cyan]streamline-extract process {doc_path}[/cyan]\n")
        
    except Exception as e:
        print_error("Failed to analyze document", str(e))


@click.command()
@click.argument('documents_path', type=click.Path(exists=True))
@click.option('--workers', '-w', type=int, default=4, help='Number of parallel workers')
def estimate(documents_path: str, workers: int):
    """
    Estimate cost and time for batch document extraction.
    
    Analyzes all documents in a directory and provides detailed
    cost and time estimates before you commit to processing.
    
    \b
    EXAMPLES:
        streamline-extract estimate documents/tariffs/
        streamline-extract estimate documents/permits/ --workers 8
    """
    docs_path = Path(documents_path)
    
    if not docs_path.exists():
        print_error(f"Path not found: {docs_path}")
        return
    
    print_header("💰 Cost & Time Estimation")
    
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
            doc_files.extend(sorted(docs_path.glob(f"*{ext}")))
        
        if not doc_files:
            supported = ', '.join(SUPPORTED_EXTENSIONS)
            print_error(
                f"No supported documents found in {docs_path}",
                f"Supported formats: {supported}"
            )
            return
    
    console.print(f"[dim]Analyzing {len(doc_files)} document(s)...[/dim]\n")
    
    # Analyze first few files to estimate average
    sample_size = min(5, len(doc_files))
    total_chars = 0
    
    for doc in doc_files[:sample_size]:
        try:
            text = extract_text_from_document(doc)
            total_chars += len(text)
        except Exception:
            pass
    
    if total_chars == 0:
        print_warning("Could not analyze documents for estimation")
        return
    
    # Calculate averages and estimates
    avg_chars = total_chars / sample_size
    estimated_total_chars = avg_chars * len(doc_files)
    estimated_tokens = int(estimated_total_chars / 4)
    
    # Cost calculation (gpt-4o-mini rates)
    input_cost = (estimated_tokens / 1_000_000) * 0.15
    output_tokens = estimated_tokens * 0.1
    output_cost = (output_tokens / 1_000_000) * 0.60
    total_cost = input_cost + output_cost
    
    # Time calculation (assuming 100 tokens/sec with workers)
    total_seconds = estimated_tokens / (100 * workers)
    estimated_minutes = total_seconds / 60
    
    # Display analysis
    console.print("[bold]Document Analysis[/bold]")
    doc_table = create_config_table("", {
        "Total Documents": str(len(doc_files)),
        "Sample Analyzed": f"{sample_size} files",
        "Avg Characters/Doc": f"{avg_chars:,.0f}",
        "Estimated Total Tokens": f"~{estimated_tokens:,}",
    })
    console.print(doc_table)
    
    # Display cost breakdown
    console.print("\n[bold]Cost Breakdown (gpt-4o-mini)[/bold]")
    cost_table = create_config_table("", {
        "Input Tokens": f"~{estimated_tokens:,} @ $0.15/1M",
        "Output Tokens": f"~{int(output_tokens):,} @ $0.60/1M",
        "Total Estimated Cost": f"[magenta bold]${total_cost:.2f}[/magenta bold]",
    })
    console.print(cost_table)
    
    # Display time estimate
    console.print("\n[bold]Time Estimate[/bold]")
    time_table = create_config_table("", {
        "Workers": str(workers),
        "Processing Time": f"~{estimated_minutes:.1f} minutes",
    })
    console.print(time_table)
    
    # Confirmation prompt
    console.print()
    if ask_confirm("Proceed with extraction?", default=False):
        console.print(f"\n[green]Run:[/green] [cyan]streamline-extract process {docs_path}[/cyan]\n")
    else:
        console.print("[yellow]Operation cancelled[/yellow]\n")


@click.command('validate-schema')
@click.argument('schema_path', type=click.Path(exists=True))
def validate_schema_cmd(schema_path: str):
    """
    Validate a JSON schema file.
    
    Checks schema syntax, structure, and StreamlineExtract-specific
    metadata requirements.
    
    \b
    EXAMPLES:
        streamline-extract validate-schema schemas/my_schema.json
        streamline-extract validate-schema schemas/example_utility_rate_schema.json
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
    if '$schema' in schema:
        print_success("Has $schema declaration")
    else:
        warnings.append("Missing $schema declaration (recommended)")
    
    if 'type' in schema:
        print_success(f"Schema type: {schema['type']}")
    else:
        issues.append("Missing 'type' field (required)")
    
    if 'properties' in schema:
        field_count = len(schema['properties'])
        print_success(f"Has {field_count} properties defined")
    else:
        issues.append("Missing 'properties' field (required)")
    
    # Check StreamlineExtract metadata
    console.print("\n[bold]StreamlineExtract Metadata[/bold]")
    
    if '$metadata' in schema:
        metadata = schema['$metadata']
        print_success("Has $metadata section")
        
        if 'identifier_fields' in metadata:
            id_fields = metadata['identifier_fields']
            console.print(f"  [dim]Identifier fields: {', '.join(id_fields)}[/dim]")
        else:
            warnings.append("$metadata missing 'identifier_fields' (recommended for deduplication)")
        
        if 'main_data_array' in metadata:
            main_array = metadata['main_data_array']
            console.print(f"  [dim]Main data array: {main_array}[/dim]")
        else:
            warnings.append("$metadata missing 'main_data_array' (recommended for consolidation)")
        
        if 'deduplication' in metadata:
            console.print(f"  [dim]Deduplication configured[/dim]")
        else:
            console.print(f"  [dim]No deduplication config (optional)[/dim]")
    else:
        warnings.append("Missing $metadata section (recommended for better consolidation)")
    
    # Summary
    console.print("\n" + "="*80)
    
    if issues:
        console.print("\n[bold red]❌ Issues Found:[/bold red]")
        for issue in issues:
            console.print(f"  • {issue}")
        console.print()
    elif warnings:
        console.print("\n[bold yellow]⚠ Recommendations:[/bold yellow]")
        for warning in warnings:
            console.print(f"  • {warning}")
        console.print()
        print_success("\nSchema is valid but could be improved! ✨")
    else:
        print_success("\nSchema is perfect! ✨")
    
    console.print()


@click.command()
def config():
    """
    Show current configuration.
    
    Displays environment settings, paths, and available schemas.
    
    \b
    EXAMPLE:
        streamline-extract config
    """
    load_dotenv()
    
    print_header("Configuration")
    
    # API Configuration
    console.print("[bold]API Configuration[/bold]")
    
    azure_key = os.getenv('AZURE_OPENAI_API_KEY')
    azure_endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
    openai_key = os.getenv('OPENAI_API_KEY')
    
    if azure_key and azure_endpoint:
        console.print("  Provider: [magenta]Azure OpenAI[/magenta]")
        console.print(f"  Endpoint: [dim]{azure_endpoint}[/dim]")
        console.print(f"  Model: [dim]{os.getenv('AZURE_OPENAI_MODEL', 'Not set')}[/dim]")
        console.print(f"  API Key: [dim]{'*' * 20}...{azure_key[-4:]}[/dim]")
    elif openai_key:
        console.print("  Provider: [cyan]OpenAI[/cyan]")
        console.print(f"  API Key: [dim]{'*' * 20}...{openai_key[-4:]}[/dim]")
    else:
        console.print("  [yellow]No API credentials configured[/yellow]")
        console.print("  [dim]Run 'streamline-extract init' to set up[/dim]")
    
    # Paths
    config_obj = get_config()
    console.print("\n[bold]Paths[/bold]")
    console.print(f"  Project Root: [dim]{config_obj.project_root}[/dim]")
    console.print(f"  Schemas: [dim]{config_obj.schema_dir}[/dim]")
    
    # Available schemas
    schemas = list(config_obj.schema_dir.glob("*.json"))
    if schemas:
        console.print("\n[bold]Available Schemas[/bold]")
        for schema in schemas:
            console.print(f"  • {schema.name}")
    
    console.print()
