"""OpenAI-based permit extraction."""

import json
import logging
import time
from pathlib import Path
from typing import Dict, Any

from openai import OpenAI

from permit_toolkit.extraction.pdf_utils import (
    extract_text_from_pdf,
    truncate_text,
    create_empty_result,
    validate_extraction,
)

logger = logging.getLogger(__name__)


class PermitExtractor:
    """
    Extract structured data from air quality permits using OpenAI.
    
    Uses GPT-4 to extract permit details, generator specifications,
    emission limits, and compliance requirements according to a JSON schema.
    """
    
    def __init__(
        self,
        api_key: str,
        schema: Dict[str, Any],
        model_id: str = "gpt-4o",
        max_retries: int = 3,
    ):
        """
        Initialize the permit extractor.
        
        Args:
            api_key: OpenAI API key
            schema: JSON schema for extraction
            model_id: OpenAI model to use
            max_retries: Maximum number of retry attempts
        """
        self.client = OpenAI(api_key=api_key)
        self.schema = schema
        self.model_id = model_id
        self.max_retries = max_retries
        
    def _create_system_prompt(self) -> str:
        """Create the system prompt for extraction."""
        return f"""You are an expert at extracting structured data from air quality permit documents for data center backup generators across multiple US states.

Extract information according to this JSON schema:
{json.dumps(self.schema, indent=2)}

OBJECTIVE: Create a curated dataset with these KEY METRICS for analysis:
- Generator capacity (MW/kW) - CRITICAL for power analysis
- Fuel type (Diesel vs Natural Gas) - CRITICAL for emissions
- Permitted operating hours per year - CRITICAL for utilization analysis
- Make/Model - for equipment tracking
- Emission limits - for regulatory compliance

EXTRACTION INSTRUCTIONS:

1. PERMIT DETAILS:
   - permitNumber: Extract exactly as shown (may have prefixes like "PRO-", state codes, etc.)
   - Dates: Convert to YYYY-MM-DD format (e.g., "August 15, 2018" → "2018-08-15")
   - facilityName: Full legal name of data center
   - facilityAddress: Complete address
   - facilityCounty: County/jurisdiction

2. GENERATOR SETS (CRITICAL - one object per unique generator configuration):
   - If document lists "EG-1 through EG-10" → ONE entry with referenceNumber "EG-1 through EG-10"
   - If document lists "EGA1, EGA2, EGA3" separately with different specs → THREE entries
   - numGenerators: Extract text like "fifteen (15)" or "1" - represents quantity in that set
   - make: Manufacturer (Caterpillar, Cummins, Generac, etc.)
   - model: Actual model number (C3000D6EB, 3516C, not just capacity)
   - ratedCapacityKW: PRIMARY capacity metric in kilowatts (if only MW given, convert: 2.5 MW = 2500 kW)
   - ratedCapacityBHP: In brake horsepower if available
   - fuelType: "diesel", "natural gas", "distillate oil", etc. (extract exact wording)
   - operatingHoursLimit: CRITICAL - max hours per year (e.g., 100, 500, unlimited)
   
3. FUEL SULFUR CONTENT (CRITICAL for emissions calculations):
   - "15 ppm" → 0.000015 (15 parts per million = 15/1,000,000)
   - "0.5%" or "0.5 weight percent" → 0.005 (0.5/100)
   - "0.0015%" → 0.000015
   - "ULSD" or "ultra-low sulfur diesel" → 0.000015 (15 ppm standard)

4. EMISSION LIMITS:
   - Extract EXACT numbers with units specified in permit
   - Note if limits are "per generator" vs "facility-wide"
   - Common pollutants: NOx, CO, VOC, PM, PM-10, PM-2.5, SO2
   - Units: lbs/hr (pounds per hour) and tons/yr (tons per year)

5. COMPLIANCE/RECORD KEEPING/NOTIFICATIONS:
   - Extract complete requirement sentences
   - Include specific reporting frequencies, deadlines
   - Capture testing/monitoring requirements

6. MULTI-STATE ROBUSTNESS:
   - Handle varying permit formats (Virginia, Illinois, Pennsylvania, etc.)
   - Adapt to different terminology ("Emergency Generator" vs "Standby Generator" vs "Backup Generator")
   - Handle both detailed technical permits and simplified registrations

7. DATA QUALITY:
   - Use null for missing/unavailable values (do NOT guess)
   - For numeric fields, extract actual numbers (not strings)
   - Extract exact text from document - no paraphrasing

OUTPUT: Return ONLY valid JSON matching the schema. No markdown, no code blocks, no explanations."""
    
    def extract(self, pdf_path: Path) -> Dict[str, Any]:
        """
        Extract structured data from a PDF permit.
        
        Args:
            pdf_path: Path to the PDF file
            
        Returns:
            Extracted data dictionary matching the schema
        """
        logger.info(f"Processing: {pdf_path.name}")
        
        # Extract text
        pdf_text = extract_text_from_pdf(pdf_path)
        
        if len(pdf_text) < 100:
            logger.warning(f"PDF appears to be empty or unreadable: {pdf_path.name}")
            return create_empty_result()
        
        # Truncate if necessary
        pdf_text = truncate_text(pdf_text)
        
        # Create prompt
        system_prompt = self._create_system_prompt()
        
        # Call OpenAI API with retry logic
        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model_id,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {
                            "role": "user",
                            "content": f"Extract data from this air quality permit document:\n\n{pdf_text}"
                        }
                    ],
                    temperature=0,
                    response_format={"type": "json_object"}
                )
                
                # Parse response
                result_text = response.choices[0].message.content
                result = json.loads(result_text)
                
                # Validate
                warnings = validate_extraction(result)
                
                logger.info(f"✓ Successfully extracted data from {pdf_path.name}")
                permit_num = result.get('permitDetails', {}).get('permitNumber', 'N/A')
                facility = result.get('permitDetails', {}).get('facilityName', 'N/A')
                num_gens = len(result.get('generatorSets', []))
                logger.info(f"  - Permit: {permit_num}")
                logger.info(f"  - Facility: {facility}")
                logger.info(f"  - Generator sets: {num_gens}")
                
                if warnings:
                    logger.warning(f"  ⚠ Warnings: {', '.join(warnings)}")
                
                return result
                
            except json.JSONDecodeError as e:
                logger.error(f"Attempt {attempt+1}/{self.max_retries}: Failed to parse JSON: {e}")
                if attempt < self.max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"  Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"  All retries exhausted")
                    return create_empty_result()
                    
            except Exception as e:
                logger.error(f"Attempt {attempt+1}/{self.max_retries}: Error: {e}")
                if attempt < self.max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"  Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    logger.error("  All retries exhausted")
                    return create_empty_result()
        
        return create_empty_result()
    
    def save_result(
        self,
        result: Dict[str, Any],
        pdf_path: Path,
        output_dir: Path,
        prefix: str = "langextract-"
    ) -> Path:
        """
        Save extraction result to JSON file.
        
        Args:
            result: Extraction result dictionary
            pdf_path: Original PDF path
            output_dir: Output directory
            prefix: Filename prefix
            
        Returns:
            Path to the saved JSON file
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Create output filename based on permit number
        permit_number = pdf_path.stem.replace("_DC_Permit", "").replace("_DC_TV_Permit", "")
        output_file = output_dir / f"{prefix}{permit_number}.json"
        
        with open(output_file, 'w') as f:
            json.dump(result, f, indent=2)
        
        logger.info(f"✓ Saved to: {output_file.name}")
        return output_file
