"""Generate langextract extractions for comparison files."""

import json
import time
from pathlib import Path
from permit_toolkit.extraction.extractor_langextract import PermitExtractorLangExtract
from permit_toolkit.extraction import load_schema
from permit_toolkit.utils import get_config

def main():
    # Load configuration
    config = get_config()
    
    if not config.openai_api_key:
        print("ERROR: OPENAI_API_KEY not found!")
        print("Please create a .env file with: OPENAI_API_KEY=your_key_here")
        return
    
    # Load schema
    schema_path = Path("schemas/air_quality_permits_schema.json")
    schema = load_schema(schema_path)
    
    # Create langextract extractor
    print("Initializing LangExtract extractor...")
    extractor = PermitExtractorLangExtract(
        api_key=config.openai_api_key,
        schema=schema,
        model_id="gpt-4o-mini",  # Using mini for higher rate limits (200k TPM vs 30k)
        enable_text_optimization=True,  # Enable 75% token reduction
        enable_rate_limiting=True,
        enable_deduplication=False,
        conservative_rate_limits=True,  # Use conservative limits for safety
    )
    
    # Files to process
    permit_ids = ["11541", "11790", "41064", "51232", "52173"]
    
    permits_dir = Path("data/permits/Virginia")
    output_dir = Path("data/comparison/Virginia/langextract")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\nExtracting data from {len(permit_ids)} permits using LangExtract...")
    print("Model: gpt-4o-mini (higher rate limits + text optimization)")
    print(f"Output: {output_dir}\n")
    
    # Process each PDF
    for i, permit_id in enumerate(permit_ids, 1):
        pdf_path = permits_dir / f"{permit_id}_DC_Permit.pdf"
        output_path = output_dir / f"{permit_id}.json"
        
        print(f"\n[{i}/{len(permit_ids)}] Processing {permit_id}")
        
        if not pdf_path.exists():
            print(f"✗ PDF not found: {pdf_path}")
            continue
        
        try:
            # Extract data
            result = extractor.extract_from_pdf(pdf_path)
            
            # Save result
            with open(output_path, 'w') as f:
                json.dump(result, f, indent=2)
            
            print(f"✓ Saved to: {output_path.name}")
            
            # Add small delay between extractions to avoid rate limits
            if i < len(permit_ids):  # Don't wait after the last one
                print("   Waiting 15s before next extraction...")
                time.sleep(15)
            
        except Exception as e:
            print(f"✗ Error: {e}")
            import traceback
            traceback.print_exc()
    
    # Print statistics
    print("\n" + "="*60)
    print("EXTRACTION STATISTICS")
    print("="*60)
    print(f"Total processed:     {extractor.stats['total_processed']}")
    print(f"API calls:           {extractor.stats['api_calls']}")
    print(f"Rate limit hits:     {extractor.stats['rate_limit_hits']}")
    
    print("\n✓ LangExtract comparison extractions complete!")
    print("\nComparison files are ready in:")
    print("  - LlamaExtract: data/comparison/Virginia/llamaextract/")
    print("  - OpenAI:       data/comparison/Virginia/openai/")
    print("  - LangExtract:  data/comparison/Virginia/langextract/")


if __name__ == "__main__":
    main()
