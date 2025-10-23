"""
Example: Scraping permits from Virginia DEQ

This example demonstrates how to use the Virginia scraper
to download air quality permits.
"""

from pathlib import Path
from permit_toolkit.scrapers.virginia import VirginiaScraper

def main():
    # Configure scraper
    output_dir = Path("data/permits/Virginia")
    
    # Create scraper instance
    scraper = VirginiaScraper(
        output_dir=output_dir,
        test_mode=True,          # Test with limited number
        test_count=10,           # Download 10 permits
        latest_only=True,        # Only latest versions
        resume=True              # Skip existing files
    )
    
    print("Starting Virginia DEQ scraper...")
    print(f"Output directory: {output_dir}")
    print(f"Mode: Test (10 permits, latest only)")
    print()
    
    # Run the scraper
    scraper.run()
    
    print("\n✓ Scraping complete!")
    print(f"Check {output_dir} for downloaded PDFs")


if __name__ == "__main__":
    main()
