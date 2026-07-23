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
        1. Put your documents in a folder (e.g., documents/Category/)
        2. Extract with your schema:
           psweep extract documents/Category --schema schemas/your_schema.json
        3. Compile into spreadsheet:
           psweep compile extracted/Category --schema schemas/your_schema.json
        4. Open the CSV or Excel file!

    \b
    COMMON WORKFLOWS:

        Extract with a custom schema (REQUIRED for production):
        $ psweep extract documents/Category --schema schemas/your_schema.json

        Extract a single document:
        $ psweep extract documents/Category/doc1.pdf --schema schemas/your_schema.json

        Test with just 5 documents first:
        $ psweep extract documents/Category --schema schemas/your_schema.json -n 5

        Compile extracted data (use same schema):
        $ psweep compile extracted/Category --schema schemas/your_schema.json

    \b
    REQUIREMENTS:
        • Python 3.9 or later
        • Azure OpenAI or OpenAI API key (run: psweep init)
        • JSON schema defining your data structure
        • Documents to process (PDF, DOCX, TXT, XLSX, CSV)

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


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
