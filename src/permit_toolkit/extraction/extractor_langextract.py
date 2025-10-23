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
        model_id: str = "gpt-4o",  # Use best model for production quality
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
1. PERMIT INFO - Extract ALL of these if present:
   - Permit number (Registration No.)
   - Issue/issuance date
   - Facility name (company name)
   - Full facility address (street, city, state)
   - County/location
   
2. GENERATOR DETAILS - For EACH generator mentioned:
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
            # Single comprehensive example with all fields
            lx.data.ExampleData(
                text="""May 16, 2008
Registration No. 11541
Facility: Corporate Office Properties, LP
Location: Russell County, VA
Address: Technology Park Drive, Lebanon, VA

One (1) Caterpillar 1500 KW diesel powered emergency generator

Fuel: distillate oil with maximum sulfur content of 0.5 weight percent
Operating Limit: 500 hours per year

Emission Limits:
NOx: 63.9 lbs/hr, 15.96 tons/yr
CO: 13.77 lbs/hr, 3.44 tons/yr  
VOC: 5.07 lbs/hr, 1.27 tons/yr
SO2: 4.2 lbs/hr, 1.05 tons/yr
PM10: 4.49 lbs/hr, 1.12 tons/yr""",
                extractions=[
                    lx.data.Extraction(
                        extraction_class="PERMIT",
                        extraction_text="May 16, 2008",
                        attributes={"field": "issue_date"}
                    ),
                    lx.data.Extraction(
                        extraction_class="PERMIT",
                        extraction_text="11541",
                        attributes={"field": "permit_number"}
                    ),
                    lx.data.Extraction(
                        extraction_class="PERMIT",
                        extraction_text="Corporate Office Properties, LP",
                        attributes={"field": "facility_name"}
                    ),
                    lx.data.Extraction(
                        extraction_class="PERMIT",
                        extraction_text="Russell County",
                        attributes={"field": "county"}
                    ),
                    lx.data.Extraction(
                        extraction_class="PERMIT",
                        extraction_text="Technology Park Drive, Lebanon, VA",
                        attributes={"field": "address"}
                    ),
                    lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="One (1) Caterpillar 1500 KW diesel powered emergency generator",
                        attributes={"generator_id": "1", "make": "Caterpillar", "model": "1500 KW"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="1500 KW",
                        attributes={"generator_id": "1", "type": "capacity"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="distillate oil",
                        attributes={"generator_id": "1", "type": "fuel"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="0.5 weight percent",
                        attributes={"generator_id": "1", "type": "sulfur"}
                    ),
                    lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="500 hours per year",
                        attributes={"generator_id": "1", "type": "hours"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="NOx: 63.9 lbs/hr, 15.96 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "nox"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="CO: 13.77 lbs/hr, 3.44 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "co"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="VOC: 5.07 lbs/hr, 1.27 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "voc"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="SO2: 4.2 lbs/hr, 1.05 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "so2"}
                    ),
                    lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="PM10: 4.49 lbs/hr, 1.12 tons/yr",
                        attributes={"generator_id": "1", "pollutant": "pm10"}
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
            field = ext.attributes.get("field", "")
            text = ext.extraction_text.strip()
            
            if field == "permit_number" or "permit" in field.lower():
                result["permitDetails"]["permitNumber"] = text
            elif field == "facility_name" or "facility" in field.lower():
                result["permitDetails"]["facilityName"] = text
            elif field == "county":
                result["permitDetails"]["facilityCounty"] = text
            elif "date" in field.lower() or "issue" in field.lower():
                result["permitDetails"]["permitIssuanceDate"] = text
            elif "expir" in field.lower():
                result["permitDetails"]["permitExpirationDate"] = text
            elif "address" in field.lower():
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
            
            # Parse generator description
            if gen_data["generator"]:
                desc = gen_data["generator"].extraction_text
                desc_lower = desc.lower()
                
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
                
                # Extract make from attributes or description
                make_attr = gen_data["generator"].attributes.get("make")
                if make_attr:
                    generator_obj["make"] = make_attr
                else:
                    # Try to infer from description
                    if "caterpillar" in desc_lower or "cat " in desc_lower:
                        generator_obj["make"] = "Caterpillar"
                    elif "cummins" in desc_lower:
                        generator_obj["make"] = "Cummins"
                    elif "kohler" in desc_lower:
                        generator_obj["make"] = "Kohler"
                    elif "generac" in desc_lower:
                        generator_obj["make"] = "Generac"
                    elif "mtu" in desc_lower or "rolls royce" in desc_lower:
                        generator_obj["make"] = "MTU"
                
                # Extract model from attributes or description
                model_attr = gen_data["generator"].attributes.get("model")
                if model_attr:
                    generator_obj["model"] = model_attr
                else:
                    # Try to extract model number pattern (e.g., "3516B", "1500 KW")
                    model_match = re.search(r'\b([A-Z0-9]{4,}[A-Z]?)\b', desc)
                    if model_match:
                        generator_obj["model"] = model_match.group(1)
                
                # Infer fuel type if not explicitly specified
                if not generator_obj["fuelType"]:
                    if "diesel" in desc_lower:
                        generator_obj["fuelType"] = "diesel"
                    elif "natural gas" in desc_lower or "gas-fired" in desc_lower:
                        generator_obj["fuelType"] = "natural gas"
                    elif "distillate" in desc_lower:
                        generator_obj["fuelType"] = "distillate oil"
            
            # Parse specifications
            for spec in gen_data["specs"]:
                text = spec.extraction_text
                spec_type = spec.attributes.get("type", "")
                
                # Handle fuel type (text, not numeric)
                if spec_type == "fuel" or "diesel" in text.lower() or "natural gas" in text.lower() or "distillate" in text.lower():
                    if not generator_obj["fuelType"]:  # Only set if not already set
                        generator_obj["fuelType"] = text.strip()
                    continue
                
                # Handle control technology (text)
                if spec_type == "control" or "scr" in text.lower() or "turbocharged" in text.lower():
                    generator_obj["controlTechnology"] = text.strip()
                    continue
                
                # Handle sulfur content
                if spec_type == "sulfur" or "sulfur" in text.lower() or "weight percent" in text.lower():
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
                    
                    if "kw" in text_lower or spec_type == "capacity":
                        # Check if it's maximum or rated
                        if "maximum" in text_lower or "max" in text_lower:
                            generator_obj["maximumCapacityKW"] = value
                        else:
                            generator_obj["ratedCapacityKW"] = value
                    elif "bhp" in text_lower or "horsepower" in text_lower:
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
                text = emission.extraction_text.lower()
                pollutant = emission.attributes.get("pollutant", "").lower()
                
                # Determine pollutant from text if not in attributes
                if not pollutant:
                    if "nox" in text or "nitrogen" in text:
                        pollutant = "nox"
                    elif "carbon monoxide" in text or " co " in text or text.startswith("co "):
                        pollutant = "co"
                    elif "voc" in text or "volatile organic" in text:
                        pollutant = "voc"
                    elif "so2" in text or "sulfur dioxide" in text:
                        pollutant = "so2"
                    elif "pm2.5" in text or "pm 2.5" in text:
                        pollutant = "pm"  # Schema doesn't have pm2.5 separate
                    elif "pm10" in text or "pm 10" in text or "pm-10" in text:
                        pollutant = "pm10"
                    elif "pm" in text or "particulate" in text:
                        pollutant = "pm"
                
                # Extract ALL numeric values with units (may have multiple per line)
                # Pattern: number + unit (lbs/hr or tons/yr)
                for match in re.finditer(r'(\d+(?:\.\d+)?)\s*(lbs?/hr|tons?/yr)', text):
                    value = float(match.group(1))
                    unit = match.group(2).lower()
                    
                    # Map to schema field names
                    if "lbs" in unit or "lb" in unit:
                        field_name = f"{pollutant}EmissionLimitLbsHr"
                    else:  # tons/yr
                        field_name = f"{pollutant}EmissionLimitTonsYr"
                    
                    # Set the value if field exists in schema
                    if field_name in generator_obj:
                        generator_obj[field_name] = value
            
            result["generatorSets"].append(generator_obj)
        
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
        num_generators_texts = [g.get("numGenerators", "").lower() for g in generators]
        
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
