"""Command-line interface for the permit toolkit."""

import click

from permit_toolkit.cli.commands import extract, validate, consolidate, clean, visualize, visualize_excel
from permit_toolkit.cli.dtree import dtree_extract


@click.group()
@click.version_option(version="0.1.0", prog_name="permit-toolkit")
def cli():
    """
    🏭 Air Quality Permit Toolkit
    
    Extract and analyze data from backup generator air quality permits.
    
    This tool helps you convert PDF permit documents into structured data
    that can be analyzed in spreadsheets or databases.
    
    \b
    QUICK START:
        1. Put your PDF permits in a folder (e.g., permits/Virginia/)
        2. Extract the data: permit-toolkit extract permits/Virginia
        3. Consolidate into a spreadsheet: permit-toolkit consolidate extracted/Virginia
        4. Open the CSV file in Excel or Google Sheets!
    
    \b
    COMMON WORKFLOWS:
    
        Extract a single permit:
        $ permit-toolkit extract permits/Virginia/12345.pdf
        
        Extract all permits in a folder:
        $ permit-toolkit extract permits/Virginia
        
        Test with just 5 permits first:
        $ permit-toolkit extract permits/Virginia -n 5
        
        Consolidate extracted data into a spreadsheet:
        $ permit-toolkit consolidate extracted/Virginia
        
        Get Excel format output:
        $ permit-toolkit consolidate extracted/Virginia --format excel
    
    \b
    REQUIREMENTS:
        • Python 3.9 or later
        • OpenAI API key (add to .env file: OPENAI_API_KEY=sk-...)
        • PDF files containing air quality permits
    
    \b
    NEED HELP?
        • See command help: permit-toolkit extract --help
        • Documentation: https://github.com/your-repo/permit-toolkit
        • Issues: Create an issue on GitHub
    """


# Register commands
cli.add_command(extract)
cli.add_command(validate)
cli.add_command(consolidate)
cli.add_command(clean)
cli.add_command(visualize)
cli.add_command(visualize_excel)
cli.add_command(dtree_extract)


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
