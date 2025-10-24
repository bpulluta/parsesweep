""""""

Example 2: Extract data from permit PDFs using hybrid extraction approach.Example 2: Extract data from permit PDFs

""""""



import jsonimport json

from pathlib import Pathfrom pathlib import Path

from permit_toolkit.extraction import ExtractorFactory, load_schemafrom permit_toolkit.extraction import ExtractorFactory, load_schema

from permit_toolkit.utils import get_configfrom permit_toolkit.utils import get_config



def main():

def main():    # Initialize

    # Initialize    config = get_config()

    config = get_config()    

        if not config.openai_api_key:

    if not config.openai_api_key:        print("Error: OPENAI_API_KEY not found in environment")

        print("Error: OPENAI_API_KEY not found in environment")        return

        return    

        # Load schema

    # Load schema    schema = load_schema(config.default_schema)

    schema = load_schema(config.default_schema)    

        # Setup directories

    # Setup directories    permits_dir = config.get_permits_dir("Virginia")

    permits_dir = config.get_permits_dir("Virginia")    output_dir = config.get_extracted_dir("Virginia")

    output_dir = config.get_extracted_dir("Virginia")    output_dir.mkdir(parents=True, exist_ok=True)

    output_dir.mkdir(parents=True, exist_ok=True)    

        # Get PDF files

    # Get PDF files (first 5 for demo)    pdf_files = list(permits_dir.glob("*.pdf"))

    pdf_files = sorted(permits_dir.glob("*.pdf"))[:5]    

        print(f"Found {len(pdf_files)} permit PDFs")

    if not pdf_files:    print(f"Extracting to: {output_dir}")

        print(f"No PDF files found in {permits_dir}")    print()

        return    

            # Process each PDF

    print(f"Found {len(pdf_files)} permit PDFs")    for i, pdf_path in enumerate(pdf_files, 1):

    print(f"Extracting to: {output_dir}")        print(f"[{i}/{len(pdf_files)}] Processing {pdf_path.name}...")

    print()        

            try:

    # Process each PDF            # Create extractor using factory (auto-detects Virginia)

    successful = 0            extractor = ExtractorFactory.create_extractor(

    for i, pdf_path in enumerate(pdf_files, 1):                pdf_path=pdf_path,

        print(f"[{i}/{len(pdf_files)}] Processing {pdf_path.name}...")                api_key=config.openai_api_key,

                        schema=schema,

        try:                model_id="gpt-4o-mini"

            # Create extractor using factory (auto-detects Virginia)            )

            extractor = ExtractorFactory.create_extractor(            

                pdf_path=pdf_path,            # Extract data

                api_key=config.openai_api_key,            result = extractor.extract_from_pdf(pdf_path)

                schema=schema,            

                model_id="gpt-4o-mini"            # Save result

            )            output_file = output_dir / f"{pdf_path.stem}.json"

                        output_file.write_text(json.dumps(result, indent=2))

            # Extract data            

            result = extractor.extract_from_pdf(pdf_path)            # Print summary

                        num_generators = len(result.get("generatorSets", []))

            # Save result            print(f"  ✓ Extracted {num_generators} generators")

            output_file = output_dir / f"{pdf_path.stem}.json"            

            output_file.write_text(json.dumps(result, indent=2))        except Exception as e:

                        print(f"  ✗ Error: {e}")

            # Print summary    

            num_generators = len(result.get("generatorSets", []))    print("\n✓ Extraction complete!")

            print(f"  ✓ Extracted {num_generators} generators")    print(f"Results saved to: {output_dir}")

            successful += 1

            

        except Exception as e:if __name__ == "__main__":

            print(f"  ✗ Error: {e}")    main()

    

    print(f"\n✓ Extraction complete! {successful}/{len(pdf_files)} successful")import os

    print(f"Results saved to: {output_dir}")from pathlib import Path

from permit_toolkit.extraction import PermitExtractor, load_schema

from permit_toolkit.utils import get_config

if __name__ == "__main__":

    main()def main():

    # Load configuration
    config = get_config()
    
    # Check for API key
    if not config.openai_api_key:
        print("ERROR: OPENAI_API_KEY not found!")
        print("Please create a .env file with: OPENAI_API_KEY=your_key_here")
        return
    
    # Load schema
    schema_path = Path("schemas/air_quality_permits_schema.json")
    schema = load_schema(schema_path)
    
    # Create extractor
    extractor = PermitExtractor(
        api_key=config.openai_api_key,
        schema=schema,
        model_id="gpt-4o"
    )
    
    # Get permits directory
    permits_dir = Path("data/permits/Virginia")
    output_dir = Path("data/extracted/Virginia")
    
    if not permits_dir.exists():
        print(f"ERROR: Permits directory not found: {permits_dir}")
        print("Please run the scraping example first!")
        return
    
    # Get all PDFs
    pdf_files = sorted(permits_dir.glob("*.pdf"))
    
    if not pdf_files:
        print(f"No PDF files found in {permits_dir}")
        return
    
    # Process first 5 for demonstration
    pdf_files = pdf_files[:5]
    
    print(f"Extracting data from {len(pdf_files)} permits...")
    print(f"Model: gpt-4o")
    print(f"Output: {output_dir}\n")
    
    # Process each PDF
    for i, pdf_path in enumerate(pdf_files, 1):
        print(f"\n[{i}/{len(pdf_files)}]")
        
        try:
            # Extract data
            result = extractor.extract(pdf_path)
            
            # Save result
            extractor.save_result(result, pdf_path, output_dir)
            
        except Exception as e:
            print(f"✗ Error: {e}")
    
    print("\n✓ Extraction complete!")
    print(f"Check {output_dir} for extracted JSON files")


if __name__ == "__main__":
    main()
