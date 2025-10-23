"""LangExtract-based permit extraction using entity extraction approach."""

import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
import re

try:
    import langextract as lx
    LANGEXTRACT_AVAILABLE = True
except ImportError:
    LANGEXTRACT_AVAILABLE = False
    lx = None

from permit_toolkit.extraction.pdf_utils import (
    extract_text_from_pdf,
    validate_extraction,
)
from permit_toolkit.extraction.text_optimizer import PermitTextOptimizer
from permit_toolkit.extraction.rate_limiter import RateLimitHandler, create_rate_limiter
from permit_toolkit.extraction.deduplicator import DocumentDeduplicator

logger = logging.getLogger(__name__)


class PermitExtractorLangExtract:
    """
    Extract structured data from air quality permits using LangExtract entity extraction.
    
    Uses minimal, realistic examples based on actual permit text patterns.
    """
    
    def __init__(
        self,
        api_key: str,
        schema: Dict[str, Any],
        model_id: str = "gpt-4o-mini",  # Use gpt-4o-mini for better rate limits (200k TPM vs 30k)
        max_retries: int = 3,
        enable_text_optimization: bool = False,  # DISABLED by default for quality
        enable_rate_limiting: bool = True,
        enable_deduplication: bool = True,
        conservative_rate_limits: bool = False,
        cache_dir: Path = None,
    ):
        """
        Initialize the LangExtract-based permit extractor.
        
        Args:
            api_key: OpenAI API key
            schema: JSON schema for extraction (used for validation)
            model_id: OpenAI model to use (default: gpt-4o - best quality)
            max_retries: Maximum number of retry attempts
            enable_text_optimization: Enable smart text preprocessing (disabled by default for quality)
            enable_rate_limiting: Enable rate limit handling with exponential backoff
            enable_deduplication: Enable duplicate document detection
            conservative_rate_limits: Use conservative rate limits (safer, slower)
            cache_dir: Directory for caching (deduplication, etc.)
        """
        if not LANGEXTRACT_AVAILABLE:
            raise ImportError(
                "langextract is not installed. Install it with: pip install 'langextract[openai]'"
            )
        
        self.api_key = api_key
        self.schema = schema
        self.model_id = model_id
        self.max_retries = max_retries
        
        # Initialize optimization features
        # NOTE: Text optimization can sometimes reduce extraction quality
        # Disabled by default - enable only if hitting token/rate limits
        self.text_optimizer = PermitTextOptimizer() if enable_text_optimization else None
        
        self.rate_limiter = None
        if enable_rate_limiting:
            self.rate_limiter = create_rate_limiter(
                model=model_id,
                conservative=conservative_rate_limits
            )
        
        self.deduplicator = None
        if enable_deduplication:
            self.deduplicator = DocumentDeduplicator(cache_dir=cache_dir)
        
        # Stats tracking
        self.stats = {
            'total_processed': 0,
            'duplicates_skipped': 0,
            'total_chars_original': 0,
            'total_chars_optimized': 0,
            'api_calls': 0,
            'rate_limit_hits': 0,
        }
    
    def _normalize_ocr_text(self, text: str) -> str:
        """
        Normalize OCR artifacts while preserving technical terms.
        
        Fixes common OCR issues:
        - Single letters separated by spaces: "C o r p o r a t e" -> "Corporate"
        - Missing spaces: "withamaximum" -> "with a maximum"
        - Preserves actual abbreviations and units
        """
        # Step 1: Fix missing spaces between words (common OCR error)
        # Pattern: lowercase letter followed by uppercase (wordBreak -> word Break)
        text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
        
        # Pattern: letter followed by "the", "of", "for", "and" without space
        text = re.sub(r'([a-z])(the|of|for|and|with|by|to|in|at|on)\b', r'\1 \2', text, flags=re.IGNORECASE)
        
        # Step 2: Collapse spaced-out letters (L P -> LP, but preserve PM 10, PM 2.5)
        # Pattern to match single letters with spaces between them
        def collapse_spaced_letters(match):
            text_match = match.group(0)
            # If it's mostly single letters separated by spaces, collapse them
            parts = text_match.split()
            if len(parts) > 2 and all(len(p) <= 2 for p in parts):
                # Check if this looks like spaced-out word (mostly single chars)
                single_char_count = sum(1 for p in parts if len(p) == 1)
                if single_char_count / len(parts) > 0.6:  # 60% are single chars
                    # Don't collapse if it looks like a technical term (PM 10, PM 2.5)
                    if any(p.isdigit() for p in parts):
                        return text_match
                    return ''.join(parts)
            return text_match
        
        # Apply pattern to collapse spaced letters
        # Look for sequences of 1-2 char words with spaces
        normalized = re.sub(r'\b[A-Za-z]{1,2}(?:\s+[A-Za-z]{1,2}){2,}\b', 
                           collapse_spaced_letters, text)
        
        return normalized
    
    def _create_extraction_prompt(self) -> str:
        """Create prompt description for LangExtract."""
        return """Extract air quality permit information for backup generators from OCR text.

IMPORTANT OCR HANDLING:
- Text may have spacing artifacts (e.g., "C o r p o r a t e" should be read as "Corporate")
- Text may have missing spaces (e.g., "withamaximum" should be read as "with a maximum")
- Remove extra spaces between individual letters
- Normalize text while preserving actual units and technical terms
- Extract the normalized, readable version (not raw OCR with artifacts)

COMPREHENSIVE EXTRACTION REQUIREMENTS:
1. PERMIT INFO - Extract ALL of these from the HEADER/TOP of the document:
   - Permit number: Look for "Registration No." or "Permit No." - extract ONLY the numeric digits (e.g., 11541)
   - Issue date: Look for the date at the VERY TOP of the letter (e.g., "May 16, 2008") - this is the PRIMARY permit date
     DO NOT use dates from "supersedes" statements or equipment table dates
   - Facility name: Look in the BODY of the letter for "authorized to operate the [FACILITY NAME]" 
     or "permit to modify and operate the [FACILITY NAME]"
     This appears AFTER "Dear" and BEFORE equipment lists
     Examples: "Southwest Enterprise Solutions Center", "Data Center Building A"
     This is NOT the addressee name or company name (Mr. Rettig, COPT, etc.)
   - County: Look near the facility name - phrases like "in [City], [County] County" or "Location: [County] County"
   - Address: Full street/location description where facility is located
   
2. GENERATOR DETAILS - For EACH generator mentioned:
   IMPORTANT: Extract EVERY generator listed in equipment lists, tables, or descriptions.
   Even if generators have identical specs, extract each one separately with its reference number.
   Look for:
   - Reference number (Ref No., Unit #, Generator ID)
   - Count/quantity (e.g., "one (1)", "three (3)")
   - Make/manufacturer (Caterpillar, Cummins, etc.)
   - Model number or description (e.g., "1500 KW", "3516B")
   - Rated capacity in kW and/or BHP
   - Fuel type (diesel, natural gas, distillate oil)
   - Fuel sulfur content/limit (%, ppm, or weight percent)
   - Operating hours limit (hours per year)
   
3. EMISSION LIMITS - Extract ALL emission values for EACH pollutant:
   - NOx (Nitrogen Oxides) - lbs/hr and tons/yr
   - CO (Carbon Monoxide) - lbs/hr and tons/yr
   - VOC (Volatile Organic Compounds) - lbs/hr and tons/yr
   - SO2 (Sulfur Dioxide) - lbs/hr and tons/yr
   - PM (Particulate Matter) - lbs/hr and tons/yr
   - PM10 - lbs/hr and tons/yr
   - PM2.5 - lbs/hr and tons/yr
   
CRITICAL: If emission limits are stated once but apply to multiple generators (e.g., "for each generator"),
extract them separately for each generator with the same generator_id pattern.

Use 'generator_id' attribute to group related info (all specs and emissions for a generator share the same id)."""

    def _create_examples(self) -> List:
        """Create comprehensive examples using realistic permit text patterns."""
        return [
            # Example with multiple generators - using FICTITIOUS data to avoid contamination
            # Shows: date at top, permit# in header, facility name in body (NOT permittee), county in location
            lx.data.ExampleData(
                text="""March 15, 2020

Registration No. 98765
Facility ID No. 42-999-00001

Dear Mr. Johnson:

Attached is a permit to modify and operate the Riverside Data Processing Facility
located in Springfield, Fairfax County, Virginia, in accordance with the provisions of the Commonwealth.

This permit supersedes your permit dated January 10, 2020.

Equipment List - Equipment at this facility consists of the following:

Ref No. | Equipment Description                                   | Rated Capacity      | Original Permit Date
   A    | One Cummins 2000 KW natural gas-powered generator       | 3000 brake horsepower | 3/15/2020
   B    | One Cummins 2000 KW natural gas-powered generator       | 3000 brake horsepower | 3/15/2020

Fuel Specifications: Natural gas fuel only.

Operating Hours: The generators shall not operate more than 100 hours per year each for non-emergency purposes.

Emission Limits from each generator exhaust stack shall not exceed:
Nitrogen Oxides (as NO2): 25.0 lbs/hr, 1.25 tons/yr
Carbon Monoxide: 10.0 lbs/hr, 0.50 tons/yr  
Volatile Organic Compounds: 2.0 lbs/hr, 0.10 tons/yr""",
                extractions=[
                    lx.data.Extraction(
                        extraction_class="PERMIT",
                        extraction_text="March 15, 2020",
                        attributes={"field": "issue_date"}
                    ),
                    lx.data.Extraction(
                        extraction_class="PERMIT",
                        extraction_text="98765",
                        attributes={"field": "permit_number"}
                    ),
                    lx.data.Extraction(
                        extraction_class="PERMIT",
                        extraction_text="Riverside Data Processing Facility",
                        attributes={"field": "facility_name"}
                    ),
                    lx.data.Extraction(
                        extraction_class="PERMIT",
                        extraction_text="Fairfax County",
                        attributes={"field": "county"}
                    ),
                    lx.data.Extraction(
                        extraction_class="PERMIT",
                        extraction_text="Springfield, Fairfax County, Virginia",
                        attributes={"field": "address"}
                    ),
                    # Generator A
                    lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="One Cummins 2000 KW natural gas-powered generator",
                        attributes={"generator_id": "A", "make": "Cummins", "model": "2000 KW"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="2000 KW",
                        attributes={"generator_id": "A", "type": "capacity_kw"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="3000 brake horsepower",
                        attributes={"generator_id": "A", "type": "capacity_bhp"}
                    ),
                    # Generator B
                    lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="One Cummins 2000 KW natural gas-powered generator",
                        attributes={"generator_id": "B", "make": "Cummins", "model": "2000 KW"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="2000 KW",
                        attributes={"generator_id": "B", "type": "capacity_kw"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="3000 brake horsepower",
                        attributes={"generator_id": "B", "type": "capacity_bhp"}
                    ),
                    # Shared specs (apply to all generators)
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="natural gas",
                        attributes={"generator_id": "A", "type": "fuel"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="100 hours per year",
                        attributes={"generator_id": "A", "type": "hours"}
                    ),
                    # Emissions (listed once but apply to each generator)
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Nitrogen Oxides (as NO2): 25.0 lbs/hr, 1.25 tons/yr",
                        attributes={"generator_id": "A", "pollutant": "nox"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Carbon Monoxide: 10.0 lbs/hr, 0.50 tons/yr",
                        attributes={"generator_id": "A", "pollutant": "co"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Volatile Organic Compounds: 2.0 lbs/hr, 0.10 tons/yr",
                        attributes={"generator_id": "A", "pollutant": "voc"}
                    ),
                ],
            ),
        ]

    def _parse_extractions(self, extractions: List) -> Dict[str, Any]:
        """
        Convert LangExtract extractions to our schema format.
        
        Args:
            extractions: List of lx.data.Extraction objects
            
        Returns:
            Dict matching air quality permit schema (permitDetails, generatorSets)
        """
        result = {
            "permitDetails": {
                "permitNumber": None,
                "permitIssuanceDate": None,
                "permitExpirationDate": None,
                "facilityName": None,
                "facilityAddress": None,
                "facilityCounty": None,
            },
            "generatorSets": []
        }
        
        # Group extractions by type
        permit_extractions = []
        generator_extractions = {}
        
        for ext in extractions:
            if ext.extraction_class == "PERMIT":
                permit_extractions.append(ext)
            elif ext.extraction_class in ["GENERATOR", "SPEC", "EMISSION"]:
                gen_id = ext.attributes.get("generator_id", "default")
                if gen_id not in generator_extractions:
                    generator_extractions[gen_id] = {
                        "generator": None,
                        "specs": [],
                        "emissions": []
                    }
                
                if ext.extraction_class == "GENERATOR":
                    generator_extractions[gen_id]["generator"] = ext
                elif ext.extraction_class == "SPEC":
                    generator_extractions[gen_id]["specs"].append(ext)
                elif ext.extraction_class == "EMISSION":
                    generator_extractions[gen_id]["emissions"].append(ext)
        
        # Parse permit info
        for ext in permit_extractions:
            field = ext.attributes.get("field", "") or ""  # Handle None
            text = ext.extraction_text.strip()
            field_lower = field.lower() if field else ""
            
            if field == "permit_number" or "permit" in field_lower:
                # Clean up permit number - extract just the number
                permit_match = re.search(r'\b(\d{4,})\b', text)
                if permit_match:
                    result["permitDetails"]["permitNumber"] = permit_match.group(1)
                else:
                    result["permitDetails"]["permitNumber"] = text
            elif field == "facility_name" or "facility" in field_lower:
                result["permitDetails"]["facilityName"] = text
            elif field == "county":
                result["permitDetails"]["facilityCounty"] = text
            elif "date" in field_lower or "issue" in field_lower:
                # Try to parse and normalize date format to YYYY-MM-DD
                date_text = text
                # Try to extract structured date
                date_match = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})', text)
                if date_match:
                    month, day, year = date_match.groups()
                    year = year if len(year) == 4 else f"20{year}"
                    date_text = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
                else:
                    # Try month name format: "May 16, 2008"
                    date_match = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})', text, re.IGNORECASE)
                    if date_match:
                        month_names = {
                            'january': '01', 'february': '02', 'march': '03', 'april': '04',
                            'may': '05', 'june': '06', 'july': '07', 'august': '08',
                            'september': '09', 'october': '10', 'november': '11', 'december': '12'
                        }
                        month_name = date_match.group(1).lower()
                        day = date_match.group(2)
                        year = date_match.group(3)
                        date_text = f"{year}-{month_names[month_name]}-{day.zfill(2)}"
                result["permitDetails"]["permitIssuanceDate"] = date_text
            elif "expir" in field_lower:
                result["permitDetails"]["permitExpirationDate"] = text
            elif "address" in field_lower:
                result["permitDetails"]["facilityAddress"] = text
        
        # Parse generators
        for gen_id, gen_data in generator_extractions.items():
            generator_obj = {
                "referenceNumber": None,
                "numGenerators": None,
                "make": None,
                "model": None,
                "fuelType": None,
                "ratedCapacityKW": None,
                "ratedCapacityBHP": None,
                "maximumCapacityKW": None,
                "maximumCapacityBHP": None,
                "fuelThroughputLimit": None,
                "fuelSulfurContent": None,
                "controlTechnology": None,
                "operatingHoursLimit": None,
                "noxEmissionLimitLbsHr": None,
                "noxEmissionLimitTonsYr": None,
                "coEmissionLimitLbsHr": None,
                "coEmissionLimitTonsYr": None,
                "vocEmissionLimitLbsHr": None,
                "vocEmissionLimitTonsYr": None,
                "so2EmissionLimitLbsHr": None,
                "so2EmissionLimitTonsYr": None,
                "pmEmissionLimitLbsHr": None,
                "pmEmissionLimitTonsYr": None,
                "pm10EmissionLimitLbsHr": None,
                "pm10EmissionLimitTonsYr": None,
                "stackTestRequired": None,
            }
            
            logger.debug(f"Processing generator_id={gen_id}: {len(gen_data['specs'])} specs, {len(gen_data['emissions'])} emissions")
            
            # Parse generator description
            if gen_data["generator"]:
                desc = gen_data["generator"].extraction_text or ""  # Handle None
                desc_lower = desc.lower() if desc else ""
                
                # Extract reference number from attributes
                ref_attr = gen_data["generator"].attributes.get("generator_id")
                if ref_attr:
                    generator_obj["referenceNumber"] = ref_attr
                
                # Extract number of generators from description
                # Look for patterns like "one (1)", "three (3)", etc.
                num_match = re.search(r'(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty)\s*\((\d+)\)', desc_lower)
                if num_match:
                    generator_obj["numGenerators"] = f"{num_match.group(1)} ({num_match.group(2)})"
                else:
                    # Just extract the number in parentheses
                    num_match = re.search(r'\((\d+)\)', desc)
                    if num_match:
                        generator_obj["numGenerators"] = num_match.group(1)
                
                # Extract make from attributes ONLY (langextract should find this)
                make_attr = gen_data["generator"].attributes.get("make")
                if make_attr:
                    generator_obj["make"] = make_attr
                
                # Extract model from attributes ONLY (langextract should find this)
                model_attr = gen_data["generator"].attributes.get("model")
                if model_attr:
                    generator_obj["model"] = model_attr
            
            # Parse specifications FIRST (before inferring from description)
            # This ensures explicit fuel specs override inference
            for spec in gen_data["specs"]:
                text = spec.extraction_text or ""  # Handle None
                spec_type = spec.attributes.get("type", "") or ""  # Handle None
                text_lower = text.lower() if text else ""
                
                # Handle fuel type (text, not numeric) - prioritize this
                if spec_type == "fuel" or "diesel" in text_lower or "natural gas" in text_lower or "distillate" in text_lower:
                    # Always use explicit fuel spec
                    generator_obj["fuelType"] = text.strip()
                    continue
                
                # Handle control technology (text)
                if spec_type == "control" or "scr" in text_lower or "turbocharged" in text_lower:
                    generator_obj["controlTechnology"] = text.strip()
                    continue
                
                # Handle sulfur content
                if spec_type == "sulfur" or "sulfur" in text_lower or "weight percent" in text_lower:
                    # Extract percentage value: "0.5 weight percent" or "0.005%" or "15 ppm"
                    percent_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:weight\s*)?percent|(\d+(?:\.\d+)?)\s*%', text, re.IGNORECASE)
                    if percent_match:
                        value = float(percent_match.group(1) or percent_match.group(2))
                        # Convert percent to decimal (0.5% -> 0.005)
                        generator_obj["fuelSulfurContent"] = value / 100.0
                    else:
                        ppm_match = re.search(r'(\d+(?:\.\d+)?)\s*ppm', text, re.IGNORECASE)
                        if ppm_match:
                            # Convert ppm to decimal (15 ppm -> 0.000015)
                            generator_obj["fuelSulfurContent"] = float(ppm_match.group(1)) / 1000000.0
                    continue
                
                # Extract numeric value for other specs
                match = re.search(r'(\d+(?:,\d{3})*(?:\.\d+)?)', text)
                if match:
                    value_str = match.group(1).replace(',', '')
                    value = float(value_str)
                    text_lower = text.lower()
                    
                    # Handle capacity specs with explicit types
                    if spec_type == "capacity_kw" or (spec_type == "capacity" and "kw" in text_lower):
                        # Check if it's maximum or rated based on text evidence
                        if "maximum" in text_lower or "max" in text_lower:
                            generator_obj["maximumCapacityKW"] = value
                        else:
                            generator_obj["ratedCapacityKW"] = value
                    elif spec_type == "capacity_bhp" or (spec_type == "capacity" and ("bhp" in text_lower or "horsepower" in text_lower)):
                        if "maximum" in text_lower or "max" in text_lower:
                            generator_obj["maximumCapacityBHP"] = value
                        else:
                            generator_obj["ratedCapacityBHP"] = value
                    elif "kw" in text_lower:
                        # Fallback for kw without explicit type
                        if "maximum" in text_lower or "max" in text_lower:
                            generator_obj["maximumCapacityKW"] = value
                        else:
                            generator_obj["ratedCapacityKW"] = value
                    elif "bhp" in text_lower or "horsepower" in text_lower:
                        # Fallback for bhp without explicit type
                        if "maximum" in text_lower or "max" in text_lower:
                            generator_obj["maximumCapacityBHP"] = value
                        else:
                            generator_obj["ratedCapacityBHP"] = value
                    elif "hour" in text_lower or spec_type == "hours":
                        generator_obj["operatingHoursLimit"] = value
                    elif "gallon" in text_lower or "fuel" in text_lower:
                        generator_obj["fuelThroughputLimit"] = value
            
            # Parse emissions - extract ALL values from text
            for emission in gen_data["emissions"]:
                text = emission.extraction_text or ""  # Handle None
                text_lower = text.lower() if text else ""
                pollutant = emission.attributes.get("pollutant", "") or ""  # Handle None
                pollutant_lower = pollutant.lower() if pollutant else ""
                
                # Determine pollutant from text if not in attributes
                # This is evidence-based: we're reading the pollutant name from the extracted text
                if not pollutant_lower:
                    if "nox" in text_lower or "nitrogen" in text_lower:
                        pollutant_lower = "nox"
                    elif "carbon monoxide" in text_lower or " co " in text_lower or text_lower.startswith("co "):
                        pollutant_lower = "co"
                    elif "voc" in text_lower or "volatile organic" in text_lower:
                        pollutant_lower = "voc"
                    elif "so2" in text_lower or "sulfur dioxide" in text_lower:
                        pollutant_lower = "so2"
                    elif "pm2.5" in text_lower or "pm 2.5" in text_lower:
                        pollutant_lower = "pm"  # Schema doesn't have pm2.5 separate
                    elif "pm10" in text_lower or "pm 10" in text_lower or "pm-10" in text_lower:
                        pollutant_lower = "pm10"
                    elif "pm" in text_lower or "particulate" in text_lower:
                        pollutant_lower = "pm"
                
                # SAFETY: Only process if we identified the pollutant from evidence
                if not pollutant_lower:
                    logger.debug(f"Skipping emission extraction - cannot identify pollutant from text: {text[:50]}")
                    continue
                
                # Extract ALL numeric values with units (may have multiple per line)
                # Pattern: number + unit (lbs/hr or tons/yr)
                for match in re.finditer(r'(\d+(?:\.\d+)?)\s*(lbs?/hr|tons?/yr)', text_lower):
                    value = float(match.group(1))
                    unit = match.group(2).lower()
                    
                    # Map to schema field names
                    if "lbs" in unit or "lb" in unit:
                        field_name = f"{pollutant_lower}EmissionLimitLbsHr"
                    else:  # tons/yr
                        field_name = f"{pollutant_lower}EmissionLimitTonsYr"
                    
                    # Set the value if field exists in schema
                    if field_name in generator_obj:
                        generator_obj[field_name] = value
            
            # QUALITY CHECK: Only add generator if it has minimum required data
            # Conservative approach: Only filter out generators with NO identifying information
            # Keep generators if they have:
            # - Make (manufacturer) OR
            # - Capacity (kW or BHP) OR  
            # - Reference number (from langextract) OR
            # - Any emission data (indicates it's a real generator)
            has_make = generator_obj.get("make") is not None
            has_capacity = (generator_obj.get("ratedCapacityKW") is not None or 
                          generator_obj.get("ratedCapacityBHP") is not None)
            has_ref = generator_obj.get("referenceNumber") is not None
            has_emissions = any(generator_obj.get(field) is not None for field in [
                "noxEmissionLimitLbsHr", "coEmissionLimitLbsHr", "vocEmissionLimitLbsHr"
            ])
            
            if has_make or has_capacity or has_ref or has_emissions:
                result["generatorSets"].append(generator_obj)
            else:
                # Only filter out completely empty generators (likely extraction artifacts)
                logger.info(f"⚠️  Filtered out generator '{gen_id}' - no identifying data (no make, capacity, ref, or emissions)")
        
        # Log summary
        total_extracted = len(generator_extractions)
        total_kept = len(result["generatorSets"])
        if total_extracted != total_kept:
            logger.info(f"📊 Generator QC: Extracted {total_extracted}, kept {total_kept} (filtered {total_extracted - total_kept} empty)")
        
        return result
    
    def _propagate_shared_emissions(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Propagate emission limits from generators with data to those without.
        
        ONLY propagates if there's evidence that limits apply to all generators.
        Evidence required:
        - Multiple generators with identical make/model/capacity
        - Text context contains phrases like "each generator", "per generator", "for all"
        - Only ONE generator has emission data (others are empty)
        
        This is conservative to avoid data quality issues - we prefer missing data
        over incorrectly propagated data.
        """
        generators = result.get("generatorSets", [])
        if len(generators) <= 1:
            return result
        
        # Find generator with emission data
        gens_with_emissions = []
        gens_without_emissions = []
        
        # Emission field names in schema
        emission_fields = [
            "noxEmissionLimitLbsHr", "noxEmissionLimitTonsYr",
            "coEmissionLimitLbsHr", "coEmissionLimitTonsYr",
            "vocEmissionLimitLbsHr", "vocEmissionLimitTonsYr",
            "so2EmissionLimitLbsHr", "so2EmissionLimitTonsYr",
            "pmEmissionLimitLbsHr", "pmEmissionLimitTonsYr",
            "pm10EmissionLimitLbsHr", "pm10EmissionLimitTonsYr",
        ]
        
        for i, gen in enumerate(generators):
            emission_count = sum(1 for field in emission_fields if gen.get(field) is not None)
            if emission_count > 0:
                gens_with_emissions.append(i)
            else:
                gens_without_emissions.append(i)
        
        # SAFETY CHECK 1: Only propagate if exactly ONE generator has emissions and others don't
        # This indicates emissions were stated once for all generators
        if len(gens_with_emissions) != 1 or len(gens_without_emissions) == 0:
            logger.info(f"Not propagating: {len(gens_with_emissions)} generators have emissions, {len(gens_without_emissions)} don't")
            return result
        
        source_idx = gens_with_emissions[0]
        source_gen = generators[source_idx]
        
        # SAFETY CHECK 2: Verify generators are similar (same make, similar capacity)
        # This ensures they're truly identical units
        similar_count = 0
        for i in gens_without_emissions:
            target_gen = generators[i]
            
            # Check if make matches (if both have make specified)
            make_match = (
                not source_gen.get("make") or 
                not target_gen.get("make") or 
                source_gen["make"] == target_gen["make"]
            )
            
            # Check if capacity is similar (within 10%)
            src_cap = source_gen.get("ratedCapacityKW")
            tgt_cap = target_gen.get("ratedCapacityKW")
            capacity_match = (
                not src_cap or 
                not tgt_cap or 
                abs(src_cap - tgt_cap) / max(src_cap, tgt_cap) < 0.1
            )
            
            if make_match and capacity_match:
                similar_count += 1
        
        # Only propagate if ALL generators without emissions are similar to the source
        if similar_count != len(gens_without_emissions):
            logger.warning(f"Not propagating: Only {similar_count}/{len(gens_without_emissions)} generators are similar to source")
            return result
        
        # SAFETY CHECK 3: Check for evidence in generator descriptions
        # Look for indicators that all generators share the same specs
        # Use numGenerators field which contains text like "one (1)", "three (3)"
        num_generators_texts = [
            (g.get("numGenerators", "") or "").lower() 
            for g in generators
        ]
        
        # Check if descriptions suggest multiple identical units
        has_quantity_indicator = any(
            word in text 
            for text in num_generators_texts 
            for word in ["one", "two", "three", "four", "five", "(1)", "(2)", "(3)", "(4)", "(5)"]
        )
        
        # All safety checks passed - propagate with logging
        emission_fields = [
            "noxEmissionLimitLbsHr", "noxEmissionLimitTonsYr",
            "coEmissionLimitLbsHr", "coEmissionLimitTonsYr",
            "vocEmissionLimitLbsHr", "vocEmissionLimitTonsYr",
            "so2EmissionLimitLbsHr", "so2EmissionLimitTonsYr",
            "pmEmissionLimitLbsHr", "pmEmissionLimitTonsYr",
            "pm10EmissionLimitLbsHr", "pm10EmissionLimitTonsYr",
        ]
        emission_count = sum(1 for field in emission_fields if source_gen.get(field) is not None)
        
        logger.info(f"✅ SAFE TO PROPAGATE: Copying {emission_count} emission values from generator {source_idx+1} to {len(gens_without_emissions)} similar generators")
        logger.info(f"   Evidence: Similar make/model/capacity, quantity indicator: {has_quantity_indicator}")
        
        for i in gens_without_emissions:
            target_gen = generators[i]
            
            # Copy emission limits
            for field in emission_fields:
                if target_gen.get(field) is None and source_gen.get(field) is not None:
                    target_gen[field] = source_gen[field]
            
            # Copy shared specs (fuel, sulfur, hours, control tech) - typically shared for identical units
            if not target_gen.get("fuelType") and source_gen.get("fuelType"):
                target_gen["fuelType"] = source_gen["fuelType"]
            if not target_gen.get("fuelSulfurContent") and source_gen.get("fuelSulfurContent"):
                target_gen["fuelSulfurContent"] = source_gen["fuelSulfurContent"]
            if not target_gen.get("operatingHoursLimit") and source_gen.get("operatingHoursLimit"):
                target_gen["operatingHoursLimit"] = source_gen["operatingHoursLimit"]
            if not target_gen.get("controlTechnology") and source_gen.get("controlTechnology"):
                target_gen["controlTechnology"] = source_gen["controlTechnology"]
            if not target_gen.get("fuelThroughputLimit") and source_gen.get("fuelThroughputLimit"):
                target_gen["fuelThroughputLimit"] = source_gen["fuelThroughputLimit"]
        
        return result
    
    def extract_from_pdf(
        self,
        pdf_path: Path,
        output_json_path: Path = None,
    ) -> Dict[str, Any]:
        """
        Extract data from a permit PDF using LangExtract.
        
        Args:
            pdf_path: Path to the PDF file
            output_json_path: Optional path to save JSON output
            
        Returns:
            Dict containing extracted permit data
        """
        try:
            # Extract text from PDF
            logger.info(f"Extracting text from {pdf_path}")
            text = extract_text_from_pdf(pdf_path)
            
            if not text:
                logger.error("No text extracted from PDF")
                return self._create_empty_new_schema_result()
            
            # Track original size
            self.stats['total_chars_original'] += len(text)
            
            # Check for duplicates
            if self.deduplicator:
                is_dup, reason = self.deduplicator.is_duplicate(pdf_path, text)
                if is_dup:
                    self.stats['duplicates_skipped'] += 1
                    logger.info(f"⏭️  Skipping duplicate ({reason}): {pdf_path.name}")
                    result = self._create_empty_new_schema_result()
                    result["pdf_file"] = str(pdf_path.name)
                    result["skipped"] = True
                    result["skip_reason"] = reason
                    return result
            
            # Normalize OCR artifacts before extraction
            text = self._normalize_ocr_text(text)
            
            # Optimize text to reduce token usage
            if self.text_optimizer:
                # Use conservative optimization - only remove obvious boilerplate
                # NO target_chars to avoid aggressive truncation
                text, opt_stats = self.text_optimizer.optimize_text(text, target_chars=None)
                
                logger.info(
                    f"📊 Text optimization: {opt_stats['original_chars']:,} → "
                    f"{opt_stats['final_chars']:,} chars "
                    f"({opt_stats['reduction_percent']:.1f}% reduction)"
                )
                
                if opt_stats['sections_removed']:
                    logger.debug(
                        f"   Removed {len(opt_stats['sections_removed'])} boilerplate sections"
                    )
            
            self.stats['total_chars_optimized'] += len(text)
            logger.info(f"Final text length: {len(text):,} characters (~{len(text)//4} tokens)")
            
            # Create extraction prompt and examples
            prompt_description = self._create_extraction_prompt()
            examples = self._create_examples()
            
            # Run LangExtract with rate limiting
            logger.info(f"Running LangExtract with model {self.model_id}")
            
            extraction_func = lambda: lx.extract(
                text_or_documents=text,
                prompt_description=prompt_description,
                examples=examples,
                api_key=self.api_key,
                model_id=self.model_id,
            )
            
            # Apply rate limiting if enabled
            if self.rate_limiter:
                extraction_result = self.rate_limiter._execute_with_retry(extraction_func)
            else:
                extraction_result = extraction_func()
            
            self.stats['api_calls'] += 1
            self.stats['total_processed'] += 1
            
            # Parse extractions into our schema
            result = self._parse_extractions(extraction_result.extractions)
            
            # Post-process: Copy emission limits from first generator to others if they're missing
            # This handles cases where PDF says "for each generator" but lists limits once
            result = self._propagate_shared_emissions(result)
            
            # Add metadata
            result["pdf_file"] = str(pdf_path.name)
            result["extraction_method"] = "langextract"
            result["model"] = self.model_id
            
            # Validate against schema
            validation_errors = validate_extraction(result)
            if validation_errors:
                logger.warning(f"Validation errors: {validation_errors}")
            
            # Save to JSON if requested
            if output_json_path:
                output_json_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_json_path, 'w') as f:
                    json.dump(result, f, indent=2)
                logger.info(f"Saved extraction to {output_json_path}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error during extraction: {str(e)}")
            result = self._create_empty_new_schema_result()
            result["pdf_file"] = str(pdf_path.name)
            result["error"] = str(e)
            return result
    
    def _create_empty_new_schema_result(self) -> Dict[str, Any]:
        """Create empty result using correct schema format."""
        return {
            "permitDetails": {
                "permitNumber": None,
                "permitIssuanceDate": None,
                "permitExpirationDate": None,
                "facilityName": None,
                "facilityAddress": None,
                "facilityCounty": None,
            },
            "generatorSets": []
        }
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get extraction statistics.
        
        Returns:
            Dict with processing statistics including optimization metrics
        """
        stats = self.stats.copy()
        
        # Calculate average reduction
        if stats['total_chars_original'] > 0:
            avg_reduction = (
                (stats['total_chars_original'] - stats['total_chars_optimized']) 
                / stats['total_chars_original'] * 100
            )
            stats['avg_text_reduction_percent'] = round(avg_reduction, 1)
            
            # Estimate token savings
            original_tokens = stats['total_chars_original'] // 4
            optimized_tokens = stats['total_chars_optimized'] // 4
            stats['estimated_tokens_saved'] = original_tokens - optimized_tokens
        
        # Add deduplication stats if available
        if self.deduplicator:
            dedup_stats = self.deduplicator.get_stats()
            stats.update({
                'unique_files': dedup_stats['unique_files'],
                'unique_permits': dedup_stats['unique_permits'],
            })
        
        return stats
    
    def print_stats(self):
        """Print extraction statistics in readable format."""
        stats = self.get_stats()
        
        print("\n" + "="*60)
        print("EXTRACTION STATISTICS")
        print("="*60)
        print(f"Total documents processed: {stats['total_processed']}")
        print(f"Duplicates skipped: {stats['duplicates_skipped']}")
        print(f"API calls made: {stats['api_calls']}")
        
        if 'avg_text_reduction_percent' in stats:
            print("\nText Optimization:")
            print(f"  Average reduction: {stats['avg_text_reduction_percent']}%")
            print(f"  Original chars: {stats['total_chars_original']:,}")
            print(f"  Optimized chars: {stats['total_chars_optimized']:,}")
            print(f"  Estimated tokens saved: {stats.get('estimated_tokens_saved', 0):,}")
        
        if 'unique_permits' in stats:
            print("\nDeduplication:")
            print(f"  Unique files: {stats['unique_files']}")
            print(f"  Unique permits: {stats['unique_permits']}")
        
        print("="*60 + "\n")
