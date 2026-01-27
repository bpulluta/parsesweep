"""
Command-line interface for StreamlineExtract.

Organizes commands into two categories:
1. Core workflow (commands.py): process, validate, consolidate
2. Utilities (utils_commands.py): init, preview, estimate, config, validate-schema
"""

import click

from streamline_extract.cli.commands import process, validate, consolidate
from streamline_extract.cli.utils_commands import (
    init,
    preview,
    estimate,
    validate_schema_cmd,
    config,
)


@click.group()
@click.version_option(version="2.0.1", prog_name="streamline-extract")
def cli():
    """
    📄 StreamlineExtract
    
    AI-powered extraction of structured data from documents.
    
    This tool helps you convert any PDF document into structured data
    that can be analyzed in spreadsheets or databases.
    
    \b
    QUICK START:
        1. Put your documents in a folder (e.g., documents/Category/)
        2. Process with your schema: 
           streamline-extract process documents/Category --schema schemas/your_schema.json
        3. Consolidate into spreadsheet: 
           streamline-extract consolidate processed/Category --schema schemas/your_schema.json
        4. Open the CSV or Excel file!
    
    \b
    COMMON WORKFLOWS:
    
        Process with a custom schema (REQUIRED for production):
        $ streamline-extract process documents/Category --schema schemas/your_schema.json
        
        Process a single document:
        $ streamline-extract process documents/Category/doc1.pdf --schema schemas/your_schema.json
        
        Test with just 5 documents first:
        $ streamline-extract process documents/Category --schema schemas/your_schema.json -n 5
        
        Consolidate processed data (use same schema):
        $ streamline-extract consolidate processed/Category --schema schemas/your_schema.json
    
    \b
    REQUIREMENTS:
        • Python 3.9 or later
        • Azure OpenAI or OpenAI API key (run: streamline-extract init)
        • JSON schema defining your data structure
        • Documents to process (PDF, DOCX, TXT, XLSX, CSV)
    
    \b
    NEED HELP?
        • Initialize setup: streamline-extract init
        • See command help: streamline-extract process --help
        • Documentation: https://github.com/bpulluta/StreamlineExtract
        • Issues: Create an issue on GitHub
    """


# Register commands
cli.add_command(init)
cli.add_command(process)
cli.add_command(preview)
cli.add_command(estimate)
cli.add_command(validate)
cli.add_command(validate_schema_cmd)
cli.add_command(consolidate)
cli.add_command(config)

# Lazy load dtree to avoid heavy dependencies unless needed
@cli.command(name='dtree-extract')
@click.pass_context
def dtree_extract_wrapper(ctx, *args, **kwargs):
    """Extract using decision trees (lazy loaded to avoid heavy imports)."""
    from streamline_extract.cli.dtree import dtree_extract
    ctx.invoke(dtree_extract, *args, **kwargs)


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
