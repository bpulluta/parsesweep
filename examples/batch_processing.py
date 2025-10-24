"""
Example: Batch processing multiple states

This example shows how to process permits from multiple states
in a single workflow.
"""

from pathlib import Path
from permit_toolkit.extraction import PermitExtractor, load_schema
from permit_toolkit.consolidation import PermitConsolidator
from permit_toolkit.utils import get_config
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def extract_state_permits(state: str, config, extractor: PermitExtractor):
    """Extract permits for a single state."""
    logger.info(f"\n{'='*80}")
    logger.info(f"PROCESSING {state.upper()}")
    logger.info(f"{'='*80}")
    
    permits_dir = config.get_permits_dir(state)
    output_dir = config.get_extracted_dir(state)
    
    if not permits_dir.exists():
        logger.warning(f"Permits directory not found: {permits_dir}")
        return 0
    
    pdf_files = list(permits_dir.glob("*.pdf"))
    
    # Skip already processed
    pdf_files = [
        pdf for pdf in pdf_files
        if not (output_dir / f"langextract-{pdf.stem}.json").exists()
    ]
    
    if not pdf_files:
        logger.info(f"All {state} permits already processed")
        return 0
    
    logger.info(f"Processing {len(pdf_files)} permits from {state}")
    
    successful = 0
    for i, pdf_path in enumerate(pdf_files, 1):
        logger.info(f"[{i}/{len(pdf_files)}] {pdf_path.name}")
        
        try:
            # Create extractor using factory (auto-detects state)
            extractor = ExtractorFactory.create_extractor(
                pdf_path=pdf_path,
                api_key=api_key,
                schema=schema,
                model_id="gpt-4o-mini"
            )
            
            result = extractor.extract_from_pdf(pdf_path)
            
            # Save result
            import json
            output_file = output_dir / f"{pdf_path.stem}.json"
            output_file.write_text(json.dumps(result, indent=2))
            
            successful += 1
        except Exception as e:
            logger.error(f"Failed: {e}")
    
    logger.info(f"✓ Completed {successful}/{len(pdf_files)} for {state}")
    return successful


def main():
    """Batch process multiple states."""
    # Configuration
    config = get_config()
    config.setup_directories()
    
    if not config.openai_api_key:
        logger.error("OPENAI_API_KEY not found. Set it in .env file")
        return
    
    # States to process
    states = ["Virginia", "Illinois", "Pennsylvania", "Ohio"]
    
    # Load schema
    schema = load_schema(config.default_schema)
    )
    
    logger.info("="*80)
    logger.info("MULTI-STATE BATCH EXTRACTION")
    logger.info("="*80)
    logger.info(f"States: {', '.join(states)}")
    logger.info(f"Model: {extractor.model_id}")
    
    # Extract each state
    total_processed = 0
    for state in states:
        processed = extract_state_permits(state, config, extractor)
        total_processed += processed
    
    # Consolidate all states
    logger.info(f"\n{'='*80}")
    logger.info("CONSOLIDATING ALL STATES")
    logger.info(f"{'='*80}")
    
    consolidator = PermitConsolidator(extraction_dir=config.extracted_dir)
    output_path = config.outputs_dir / "all_states_generators.csv"
    
    df = consolidator.consolidate(output_path=output_path)
    
    if not df.empty:
        summary = consolidator.generate_summary(df)
        
        logger.info(f"\n✓ COMPLETE!")
        logger.info(f"  Total permits processed: {total_processed}")
        logger.info(f"  Total facilities: {summary['total_facilities']}")
        logger.info(f"  Total generators: {summary['total_generators']}")
        logger.info(f"  Total capacity: {summary['total_capacity_mw']:.2f} MW")
        logger.info(f"  Output: {output_path}")


if __name__ == "__main__":
    main()
