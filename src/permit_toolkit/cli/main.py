"""Command-line interface for the permit toolkit."""

import click

from permit_toolkit.cli.commands import extract, validate, consolidate


@click.group()
@click.version_option(version='0.1.0', prog_name='permit-toolkit')
def cli():
    """
    Air Quality Permit Toolkit
    
    Extract structured data from air quality permits for backup generator analysis.
    
    Examples:
    
        # Extract single permit
        permit-toolkit extract data/permits/Virginia/11790_DC_Permit.pdf
        
        # Extract directory of permits
        permit-toolkit extract data/permits/Virginia -n 5
        
        # Validate extraction result
        permit-toolkit validate data/extracted/Virginia/11790_DC_Permit.json
        
        # Consolidate extractions into CSV
        permit-toolkit consolidate data/Virginia/extracted data/Virginia/dataset.csv
    """
    pass


# Register commands
cli.add_command(extract)
cli.add_command(validate)
cli.add_command(consolidate)


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == '__main__':
    main()

