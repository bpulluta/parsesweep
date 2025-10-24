"""
Example: Basic extraction workflow

Demonstrates simple permit data extraction.
"""

from pathlib import Path
from permit_toolkit.extraction import ExtractorFactory, load_schema
from permit_toolkit.utils import get_config


def main():
    config = get_config()
    
    if not config.openai_api_key:
        print("❌ Set OPENAI_API_KEY in .env file")
        return
    
    schema = load_schema(config.default_schema)
    permits_dir = config.get_permits_dir("Virginia")
    
    if not permits_dir.exists():
        print(f"❌ No permits found at {permits_dir}")
        print("   Run: python examples/01_scrape_permits.py")
        return
    
    # Process only first 2 PDFs for quick demonstration
    pdf_files = list(permits_dir.glob("*.pdf"))[:2]
    if not pdf_files:
        print("❌ No PDF files found")
        return
    
    print(f"Processing {len(pdf_files)} permit(s) (demo mode)...\n")
    
    for pdf_path in pdf_files:
        print(f"• {pdf_path.name}")
        
        extractor = ExtractorFactory.create_extractor(
            pdf_path=pdf_path,
            api_key=config.openai_api_key,
            schema=schema,
            model_id="gpt-4o-mini"
        )
        
        result = extractor.extract_from_pdf(pdf_path)
        
        if result.get('generatorSets'):
            print(f"  ✓ {len(result['generatorSets'])} generators\n")
        else:
            print("  ⚠️  No generators\n")


if __name__ == "__main__":
    main()
