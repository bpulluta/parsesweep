"""
Helper script to filter and analyze Virginia DEQ permits

Use this to:
1. List only latest versions
2. List only historical versions  
3. Get statistics about the downloaded permits
4. Create filtered file lists for extraction pipelines

Examples:
    # List only latest versions
    python filter_permits.py --latest
    
    # List only historical versions
    python filter_permits.py --historical
    
    # Count files
    python filter_permits.py --stats
    
    # Create file list for extraction (latest only)
    python filter_permits.py --latest --output latest_permits.txt
"""

import argparse
import json
from pathlib import Path
from collections import defaultdict


def get_permit_files(directory="03_permit_documents/by_state/Documents/Virginia"):
    """Get all permit files categorized by type"""
    permit_dir = Path(directory)
    
    all_pdfs = list(permit_dir.glob("*.pdf"))
    
    # Categorize files
    latest = []
    historical = []
    
    for pdf in all_pdfs:
        if "_v" in pdf.stem:  # Historical version (has _v{date} in name)
            historical.append(pdf)
        else:  # Latest version (no version marker)
            latest.append(pdf)
    
    return {
        'latest': sorted(latest),
        'historical': sorted(historical),
        'all': sorted(all_pdfs)
    }


def load_manifest(directory="03_permit_documents/by_state/Documents/Virginia"):
    """Load download manifest if it exists"""
    manifest_path = Path(directory) / "DOWNLOAD_MANIFEST.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            return json.load(f)
    return None


def print_stats(files, manifest=None):
    """Print statistics about the downloaded permits"""
    print("\n" + "=" * 70)
    print("VIRGINIA DEQ PERMIT STATISTICS")
    print("=" * 70)
    print(f"Total PDFs:         {len(files['all'])}")
    print(f"Latest versions:    {len(files['latest'])}")
    print(f"Historical versions: {len(files['historical'])}")
    print("=" * 70)
    
    if manifest:
        print(f"\nLast download: {manifest['download_date']}")
        print(f"Mode: {manifest['mode']}")
        print(f"Newly downloaded: {manifest['newly_downloaded']}")
        print(f"Failed: {manifest['failed']}")
    
    # Group historical by registration number
    if files['historical']:
        historical_by_reg = defaultdict(list)
        for f in files['historical']:
            # Extract registration number (before _v)
            reg_no = f.stem.split('_v')[0]
            historical_by_reg[reg_no].append(f)
        
        print(f"\nFacilities with historical versions: {len(historical_by_reg)}")
        print("\nHistorical versions by facility:")
        for reg_no, versions in sorted(historical_by_reg.items()):
            print(f"  {reg_no}: {len(versions)} historical version(s)")


def list_files(files, file_type='latest', output=None):
    """List files of a specific type"""
    file_list = files[file_type]
    
    if output:
        # Write to file
        with open(output, 'w') as f:
            for file_path in file_list:
                f.write(str(file_path.absolute()) + '\n')
        print(f"\nWrote {len(file_list)} file paths to {output}")
    else:
        # Print to console
        print(f"\n{file_type.upper()} VERSIONS ({len(file_list)} files):")
        print("-" * 70)
        for file_path in file_list:
            size_kb = file_path.stat().st_size / 1024
            print(f"{file_path.name:<50} {size_kb:>8.0f} KB")


def main():
    parser = argparse.ArgumentParser(description='Filter and analyze Virginia DEQ permits')
    parser.add_argument('--latest', action='store_true', help='Show only latest versions')
    parser.add_argument('--historical', action='store_true', help='Show only historical versions')
    parser.add_argument('--all', action='store_true', help='Show all versions')
    parser.add_argument('--stats', action='store_true', help='Show statistics only')
    parser.add_argument('--output', '-o', help='Output file path for file list')
    parser.add_argument('--directory', '-d', default='03_permit_documents/by_state/Documents/Virginia',
                       help='Directory containing permits')
    
    args = parser.parse_args()
    
    # Get files
    files = get_permit_files(args.directory)
    manifest = load_manifest(args.directory)
    
    # Default to stats if no option specified
    if not any([args.latest, args.historical, args.all, args.stats]):
        args.stats = True
    
    # Show stats
    if args.stats:
        print_stats(files, manifest)
    
    # Show latest
    if args.latest:
        list_files(files, 'latest', args.output)
    
    # Show historical
    if args.historical:
        list_files(files, 'historical', args.output)
    
    # Show all
    if args.all:
        list_files(files, 'all', args.output)


if __name__ == "__main__":
    main()
