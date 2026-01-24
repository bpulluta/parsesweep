"""Command-line interface for StreamlineExtract."""

import click

from streamline_extract.cli.commands import extract, validate, consolidate
from streamline_extract.cli.new_commands import (
    init,
    preview,
    estimate,
    validate_schema_cmd,
    config,
)


@click.group()
@click.version_option(version="0.1.0", prog_name="streamline-extract")
def cli():
    """
    📄 StreamlineExtract
    
    AI-powered extraction of structured data from PDF documents.
    
    This tool helps you convert any PDF document into structured data
    that can be analyzed in spreadsheets or databases.
    
    \b
    QUICK START:
        1. Put your PDF documents in a folder (e.g., documents/Category/)
        2. Extract the data: streamline-extract extract documents/Category
        3. Consolidate into a spreadsheet: streamline-extract consolidate extracted/Category
        4. Open the CSV file in Excel or Google Sheets!
    
    \b
    COMMON WORKFLOWS:
    
        Extract a single document:
        $ streamline-extract extract documents/Category/doc1.pdf
        
        Extract all documents in a folder:
        $ streamline-extract extract documents/Category
        
        Test with just 5 documents first:
        $ streamline-extract extract documents/Category -n 5
        
        Consolidate extracted data into a spreadsheet:
        $ streamline-extract consolidate extracted/Category
        
        Get Excel format output:
        $ streamline-extract consolidate extracted/Category --format excel
    
    \b
    REQUIREMENTS:
        • Python 3.9 or later
        • OpenAI API key (add to .env file: OPENAI_API_KEY=sk-...)
        • PDF documents to process
    
    \b
    NEED HELP?
        • See command help: streamline-extract extract --help
        • Documentation: https://github.com/bpulluta/StreamlineExtract
        • Issues: Create an issue on GitHub
    """


# Register commands
cli.add_command(init)
cli.add_command(extract)
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
