"""
Command-line interface for StreamlineExtract.

Organizes commands into two categories:
1. Core workflow (commands.py): process, validate, consolidate
2. Utilities (utils_commands.py): init, preview, estimate, config, validate-schema
"""

import click

from streamline_extract import __version__
from streamline_extract.cli.commands import (
    acquire,
    curate,
    process,
    validate,
    consolidate,
    compare,
    benchmark,
)
from streamline_extract.cli.utils_commands import (
    init,
    init_domain_schema_cmd,
    init_domain_pack_cmd,
    preview,
    estimate,
    validate_schema_cmd,
    validate_runtime_cmd,
    config,
)


@click.group()
@click.version_option(version=__version__, prog_name="streamline-extract")
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
cli.add_command(init_domain_schema_cmd)
cli.add_command(init_domain_pack_cmd)
cli.add_command(acquire)
cli.add_command(curate)
cli.add_command(process)
cli.add_command(preview)
cli.add_command(estimate)
cli.add_command(validate)
cli.add_command(validate_schema_cmd)
cli.add_command(validate_runtime_cmd)
cli.add_command(consolidate)
cli.add_command(compare)
cli.add_command(benchmark)
cli.add_command(config)


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
