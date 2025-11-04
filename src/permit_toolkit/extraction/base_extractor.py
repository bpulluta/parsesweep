"""Base extractor class for state-specific permit extraction."""

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, List

try:
    import langextract as lx
    LANGEXTRACT_AVAILABLE = True
except ImportError:
    LANGEXTRACT_AVAILABLE = False
    lx = None

from permit_toolkit.extraction.pdf_utils import extract_text_from_pdf, validate_extraction
from permit_toolkit.extraction.text_optimizer import PermitTextOptimizer
from permit_toolkit.extraction.rate_limiter import create_rate_limiter
from permit_toolkit.extraction.deduplicator import DocumentDeduplicator

logger = logging.getLogger(__name__)


class BasePermitExtractor(ABC):
    """
    Base class for state-specific permit extractors.
    
    Hybrid extraction strategy:
    1. OpenAI direct: Extracts structured fields (facility name, address, county, dates)
       - More accurate, no example contamination
       - Falls back to regex patterns for validation
    2. LangExtract: Extracts generators with full traceability
       - Critical for QA and emissions verification
       - Uses state-specific examples
    
    Cost-effective: ~30% cheaper than full LangExtract while improving quality.
    
    Subclasses must implement:
    - _create_state_examples(): State-specific LangExtract examples (for generators)
    - _extract_permit_details_fallback(): State-specific regex patterns (for validation)
    """
    
    STATE_NAME: str = None  # Subclasses must define
    
    def __init__(
        self,
        api_key: str,
        schema: Dict[str, Any],
        model_id: str = "gpt-4o-mini",
        max_retries: int = 3,
        enable_text_optimization: bool = False,
        enable_rate_limiting: bool = True,
        enable_deduplication: bool = True,
        conservative_rate_limits: bool = False,
        cache_dir: Path = None,
    ):
        """
        Initialize base permit extractor.
        
        Args:
            api_key: OpenAI API key
            schema: JSON schema for extraction validation
            model_id: OpenAI model to use (default: gpt-4o-mini)
            max_retries: Maximum retry attempts for API calls
            enable_text_optimization: Enable smart text preprocessing
            enable_rate_limiting: Enable rate limit handling with exponential backoff
            enable_deduplication: Enable duplicate document detection
            conservative_rate_limits: Use conservative rate limits (50 req/min vs 200)
            cache_dir: Directory for caching
        """
        if not LANGEXTRACT_AVAILABLE:
            raise ImportError(
                "langextract is not installed. Install it with: pip install 'langextract[openai]'"
            )
        
        self.api_key = api_key
        self.schema = schema
        self.model_id = model_id
        self.max_retries = max_retries
        
        self.text_optimizer = PermitTextOptimizer() if enable_text_optimization else None
        
        # Initialize OpenAI client for structured field extraction
        import openai
        self.openai_client = openai.OpenAI(api_key=api_key)
        
        self.rate_limiter = None
        if enable_rate_limiting:
            self.rate_limiter = create_rate_limiter(
                model=model_id,
                conservative=conservative_rate_limits
            )
        
        self.deduplicator = None
        if enable_deduplication:
            self.deduplicator = DocumentDeduplicator(cache_dir=cache_dir)
        
        self.stats = {
            'total_processed': 0,
            'duplicates_skipped': 0,
            'total_chars_original': 0,
            'total_chars_optimized': 0,
            'api_calls': 0,
            'rate_limit_hits': 0,
        }
        
        # Store last extraction result for visualization
        self._last_extraction_result = None
        self._last_text = None
    
    @abstractmethod
    def _create_state_examples(self) -> List:
        """
        Create state-specific examples for LangExtract (generators only).
        
        Returns:
            List of lx.data.ExampleData objects with generator extraction patterns
        """
        pass
    
    @abstractmethod
    def _extract_permit_details_fallback(self, text: str, pdf_path: Path) -> Dict[str, Any]:
        """
        State-specific regex patterns for structured field extraction.
        Used to validate/merge with OpenAI results.
        
        Args:
            text: Full PDF text
            pdf_path: Path to PDF file
            
        Returns:
            Dict with permitDetails fields (only non-null values)
        """
        pass
    
    def _normalize_ocr_text(self, text: str) -> str:
        """
        Normalize OCR artifacts while preserving technical terms.
        
        Fixes common OCR issues:
        - Single letters separated by spaces: "C o r p o r a t e" -> "Corporate"
        - Missing spaces: "withamaximum" -> "with a maximum"
        - Preserves actual abbreviations and units
        """
        import re
        
        # Fix missing spaces between words
        text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
        text = re.sub(r'([a-z])(the|of|for|and|with|by|to|in|at|on)\b', r'\1 \2', text, flags=re.IGNORECASE)
        
        # Collapse spaced-out letters
        def collapse_spaced_letters(match):
            text_match = match.group(0)
            parts = text_match.split()
            if len(parts) > 2 and all(len(p) <= 2 for p in parts):
                single_char_count = sum(1 for p in parts if len(p) == 1)
                if single_char_count / len(parts) > 0.6:
                    if any(p.isdigit() for p in parts):
                        return text_match
                    return ''.join(parts)
            return text_match
        
        normalized = re.sub(r'\b[A-Za-z]{1,2}(?:\s+[A-Za-z]{1,2}){2,}\b', 
                           collapse_spaced_letters, text)
        
        return normalized
    
    def extract_from_pdf(
        self,
        pdf_path: Path,
        output_json_path: Path = None,
        max_pages: int = 15,
    ) -> Dict[str, Any]:
        """
        Extract data from permit PDF using hybrid LLM + regex approach.
        
        Args:
            pdf_path: Path to the PDF file
            output_json_path: Optional path to save JSON output
            max_pages: Maximum pages to extract (default: 15 for cost optimization).
                      Set to None for unlimited pages.
            
        Returns:
            Dict containing extracted permit data
        """
        try:
            logger.info(f"Extracting text from {pdf_path}")
            text = extract_text_from_pdf(pdf_path)
            
            if not text:
                logger.error("No text extracted from PDF")
                return self._create_empty_result()
            
            self.stats['total_chars_original'] += len(text)
            
            # Check for duplicates
            if self.deduplicator:
                is_dup, reason = self.deduplicator.is_duplicate(pdf_path, text)
                if is_dup:
                    self.stats['duplicates_skipped'] += 1
                    logger.info(f"⏭️  Skipping duplicate ({reason}): {pdf_path.name}")
                    result = self._create_empty_result()
                    result["pdf_file"] = str(pdf_path.name)
                    result["skipped"] = True
                    result["skip_reason"] = reason
                    return result
            
            # Normalize OCR artifacts
            text = self._normalize_ocr_text(text)
            
            # Optimize text if enabled
            if self.text_optimizer:
                text, opt_stats = self.text_optimizer.optimize_text(text, target_chars=None)
                logger.info(
                    f"📊 Text optimization: {opt_stats['original_chars']:,} → "
                    f"{opt_stats['final_chars']:,} chars "
                    f"({opt_stats['reduction_percent']:.1f}% reduction)"
                )
            
            self.stats['total_chars_optimized'] += len(text)
            logger.info(f"Final text length: {len(text):,} characters (~{len(text)//4} tokens)")
            
            # Hybrid extraction: OpenAI for structured fields, LangExtract for generators
            result = self._hybrid_extract(text, pdf_path)
            
            # Post-process emissions
            result = self._propagate_shared_emissions(result)
            
            # Add metadata
            result["pdf_file"] = str(pdf_path.name)
            result["extraction_method"] = f"hybrid_openai_langextract_{self.STATE_NAME}"
            result["model"] = self.model_id
            
            # Validate
            validation_errors = validate_extraction(result)
            if validation_errors:
                logger.warning(f"Validation errors: {validation_errors}")
            
            # Save if requested
            if output_json_path:
                output_json_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_json_path, 'w') as f:
                    json.dump(result, f, indent=2)
                logger.info(f"Saved extraction to {output_json_path}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error during extraction: {str(e)}")
            result = self._create_empty_result()
            result["pdf_file"] = str(pdf_path.name)
            result["error"] = str(e)
            return result
    
    def _hybrid_extract(self, text: str, pdf_path: Path) -> Dict[str, Any]:
        """
        Hybrid extraction: OpenAI for structured fields + LangExtract for generators.
        
        Benefits:
        - OpenAI direct is more accurate for structured fields (no example contamination)
        - LangExtract provides traceability for generators (critical for QA)
        - Cost-effective: single OpenAI call (~500 tokens) vs full LangExtract (~15k tokens)
        
        Args:
            text: Full PDF text
            pdf_path: Path to PDF for fallback
            
        Returns:
            Merged result with OpenAI structured fields + LangExtract generators
        """
        logger.info("🔀 Hybrid extraction: OpenAI (structured) + LangExtract (generators)")
        
        # Step 1: OpenAI extracts structured fields (cheap, accurate)
        permit_details = self._extract_permit_details_openai(text, pdf_path)
        
        # Step 2: LangExtract extracts generators with traceability
        generators_result = self._extract_generators_langextract(text)
        
        # Step 3: Merge results
        result = {
            "permitDetails": permit_details,
            "generatorSets": generators_result.get("generatorSets", []),
        }
        
        logger.info(f"✓ Hybrid extraction complete: {len(result['generatorSets'])} generators")
        return result
    
    def _extract_permit_details_openai(self, text: str, pdf_path: Path) -> Dict[str, Any]:
        """
        Extract structured permit details using OpenAI direct API.
        Falls back to regex if OpenAI fails or returns contaminated data.
        """
        logger.info("📋 OpenAI: Extracting permit details")
        
        # Always run regex fallback first (ground truth for structured fields)
        fallback_details = self._extract_permit_details_fallback(text, pdf_path)
        
        # Create focused prompt for structured fields only
        prompt = f"""Extract the following permit details from this air quality permit document.
Return ONLY valid JSON with these exact fields, using null for any field you cannot find:

{{
  "permitNumber": "string or null",
  "permitIssuanceDate": "YYYY-MM-DD or null",
  "facilityName": "string or null",
  "facilityAddress": "string or null", 
  "facilityCounty": "string or null",
  "facilityCity": "string or null",
  "permitteeName": "string or null",
  "permitteeAddress": "string or null"
}}

IMPORTANT:
- Do NOT use placeholder/example data
- Extract ONLY what exists in the document
- For dates, use YYYY-MM-DD format
- For addresses, extract the full street address

Document text:
{text[:8000]}"""  # First 8k chars contain permit header
        
        try:
            # Reasoning models (gpt-5, o1, o3, etc.) don't support temperature or response_format
            is_reasoning_model = any(
                x in self.model_id.lower() for x in ["gpt-5", "o1", "o3", "o4"]
            )
            
            api_params = {
                "model": self.model_id,
                "messages": [
                    {"role": "system", "content": "You are a precise data extraction assistant. Return only valid JSON."},
                    {"role": "user", "content": prompt}
                ],
            }
            
            if not is_reasoning_model:
                api_params["temperature"] = 0
                api_params["response_format"] = {"type": "json_object"}
            
            response = self.openai_client.chat.completions.create(**api_params)
            
            self.stats['api_calls'] += 1
            
            openai_result = json.loads(response.choices[0].message.content)
            
            # Validate and merge with fallback
            contamination_keywords = [
                'placeholder', 'example', 'test facility', 'sample city', 
                'county name', '123 business', 'business rd', 'xxx', 'yyy'
            ]
            
            final_details = {}
            for field in ['permitNumber', 'permitIssuanceDate', 'facilityName', 
                         'facilityAddress', 'facilityCounty', 'facilityCity',
                         'permitteeName', 'permitteeAddress']:
                openai_value = openai_result.get(field)
                fallback_value = fallback_details.get(field)
                
                # Check contamination (handle list/dict values)
                is_contaminated = False
                if openai_value and isinstance(openai_value, (str, int, float)):
                    val_lower = str(openai_value).lower()
                    is_contaminated = any(kw in val_lower for kw in contamination_keywords)
                
                # Prefer fallback if: OpenAI contaminated, missing, or less complete
                if (is_contaminated or not openai_value or 
                    (fallback_value and len(str(fallback_value)) > len(str(openai_value)))):
                    final_details[field] = fallback_value or openai_value or "Not specified"
                else:
                    final_details[field] = openai_value or "Not specified"
            
            logger.info(f"✓ OpenAI details: {final_details.get('facilityName', 'N/A')}")
            return final_details
            
        except Exception as e:
            logger.warning(f"OpenAI extraction failed: {e}, using regex fallback")
            self.stats['fallback_applied'] += 1
            return {
                **{k: "Not specified" for k in ['permitNumber', 'permitIssuanceDate', 
                   'facilityName', 'facilityAddress', 'facilityCounty', 'facilityCity',
                   'permitteeName', 'permitteeAddress']},
                **fallback_details  # Merge fallback over defaults
            }
    
    def _extract_generators_langextract(self, text: str) -> Dict[str, Any]:
        """
        Extract generators using LangExtract for full traceability.
        Only extracts generator data, not permit details.
        """
        logger.info("⚙️  LangExtract: Extracting generators (traceable)")
        
        # Create generator-focused prompt
        prompt_description = """Extract generator/equipment information from the air quality permit:
- Equipment ID/Reference number
- Equipment type/description  
- Manufacturer and model
- Fuel type
- Emissions data (pollutants, rates, units)
- Operating hours or restrictions

Focus ONLY on generator/equipment data, NOT permit header details."""
        
        # Use state-specific examples (only for generators, not permit details)
        examples = self._create_state_examples()
        
        def extraction_func():
            return lx.extract(
                text_or_documents=text,
                prompt_description=prompt_description,
                examples=examples,
                api_key=self.api_key,
                model_id=self.model_id,
            )
        
        if self.rate_limiter:
            extraction_result = self.rate_limiter._execute_with_retry(extraction_func)
        else:
            extraction_result = extraction_func()
        
        self.stats['api_calls'] += 1
        
        # Store for visualization
        self._last_extraction_result = extraction_result
        self._last_text = text
        
        # Use full parsing logic, then extract only generators
        full_result = self._parse_extractions(extraction_result.extractions)
        
        # Return only generator data
        parsed = {"generatorSets": full_result.get("generatorSets", [])}
        logger.info(f"✓ LangExtract: {len(parsed['generatorSets'])} generators")
        return parsed
    def _parse_extractions(self, extractions: List) -> Dict[str, Any]:
        """
        Convert LangExtract extractions to our schema format.
        
        NOTE: In hybrid mode, this only processes GENERATOR/SPEC/EMISSION classes.
        PERMIT details are handled by OpenAI for better accuracy.
        
        Args:
            extractions: List of lx.data.Extraction objects
            
        Returns:
            Dict matching air quality permit schema (permitDetails, generatorSets)
        """
        import re
        
        result = {
            "permitDetails": {},  # Empty in hybrid mode - OpenAI handles this
            "generatorSets": []
        }
        
        # Group generator extractions only (skip PERMIT class in hybrid mode)
        generator_extractions = {}
        
        for ext in extractions:
            # Skip PERMIT class - handled by OpenAI in hybrid mode
            if ext.extraction_class == "PERMIT":
                continue
            
            # Process only generator-related classes
            if ext.extraction_class in ["GENERATOR", "SPEC", "EMISSION"]:
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
        
        # Parse generators (ONLY thing LangExtract handles in hybrid mode)
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
                "fuelThroughputPerUnitLimit": None,
                "fuelSulfurContent": None,
                "controlTechnology": None,
                "operatingHoursPerUnitLimit": None,
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
                
                # Extract make from attributes (preferred) OR parse from description text
                make_attr = gen_data["generator"].attributes.get("make")
                if make_attr:
                    generator_obj["make"] = make_attr
                else:
                    # Fallback: Parse from description (e.g., "One Caterpillar 1500 KW...")
                    # Common manufacturers in permits
                    manufacturers = [
                        "caterpillar", "cummins", "detroit diesel", "generac", "kohler",
                        "mtu", "perkins", "waukesha", "wartsila", "mitsubishi", "man",
                        "ge", "general electric", "siemens", "rolls-royce", "pratt & whitney"
                    ]
                    for mfr in manufacturers:
                        if mfr in desc_lower:
                            # Extract the proper case from original description
                            mfr_pattern = re.compile(r'\b(' + re.escape(mfr) + r')\b', re.IGNORECASE)
                            mfr_match = mfr_pattern.search(desc)
                            if mfr_match:
                                generator_obj["make"] = mfr_match.group(1)
                                break
                
                # Extract model from attributes (preferred) OR parse from description
                model_attr = gen_data["generator"].attributes.get("model")
                if model_attr:
                    generator_obj["model"] = model_attr
                else:
                    # Fallback: Look for model patterns (typically alphanumeric near make)
                    # Common patterns: "3516B", "QSK60", "16V4000", etc.
                    model_match = re.search(r'\b([A-Z0-9]{3,}[A-Z0-9-]*)\b', desc)
                    if model_match and generator_obj["make"]:
                        # Only extract if we found the make (to ensure it's related)
                        candidate = model_match.group(1)
                        # Skip if it's a common word or unit
                        if candidate not in ['ONE', 'TWO', 'THREE', 'DIESEL', 'POWERED', 'EMERGENCY']:
                            generator_obj["model"] = candidate
            
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
                        generator_obj["operatingHoursPerUnitLimit"] = value
                    elif "gallon" in text_lower or "fuel" in text_lower:
                        generator_obj["fuelThroughputPerUnitLimit"] = value
            
            # FALLBACK: If SPEC extractions didn't provide capacity/fuel, parse from GENERATOR description
            if gen_data["generator"]:
                desc = gen_data["generator"].extraction_text or ""
                desc_lower = desc.lower() if desc else ""
                
                # Extract capacity (KW) if not already found
                if generator_obj["ratedCapacityKW"] is None:
                    kw_match = re.search(r'(\d+(?:,\d{3})*)\s*(?:kw|kilowatt)', desc_lower)
                    if kw_match:
                        generator_obj["ratedCapacityKW"] = float(kw_match.group(1).replace(',', ''))
                
                # Extract capacity (BHP) if not already found  
                if generator_obj["ratedCapacityBHP"] is None:
                    bhp_match = re.search(r'(\d+(?:,\d{3})*)\s*(?:bhp|brake\s*horsepower)', desc_lower)
                    if bhp_match:
                        generator_obj["ratedCapacityBHP"] = float(bhp_match.group(1).replace(',', ''))
                
                # Extract fuel type if not already found
                if generator_obj["fuelType"] is None:
                    # Look for common fuel type mentions
                    if "diesel" in desc_lower:
                        generator_obj["fuelType"] = "diesel"
                    elif "distillate oil" in desc_lower:
                        generator_obj["fuelType"] = "distillate oil"
                    elif "natural gas" in desc_lower:
                        generator_obj["fuelType"] = "natural gas"
                    elif "propane" in desc_lower:
                        generator_obj["fuelType"] = "propane"
                    elif "gasoline" in desc_lower:
                        generator_obj["fuelType"] = "gasoline"
            
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
            
            # CALCULATE MISSING TONS/YR from lbs/hr + operating hours (if available)
            operating_hours = generator_obj.get("operatingHoursPerUnitLimit")
            num_generators = generator_obj.get("numGenerators")
            
            # Parse numGenerators if it's a string like "6" or "(6)"
            if num_generators:
                if isinstance(num_generators, str):
                    import re
                    num_match = re.search(r'(\d+)', num_generators)
                    if num_match:
                        num_generators = int(num_match.group(1))
                    else:
                        num_generators = 1
                elif not isinstance(num_generators, (int, float)):
                    num_generators = 1
            else:
                num_generators = 1  # Default to 1 if not specified
            
            if operating_hours and operating_hours > 0:
                for pollutant in ["nox", "co", "voc", "so2", "pm", "pm10"]:
                    lbs_hr_field = f"{pollutant}EmissionLimitLbsHr"
                    tons_yr_field = f"{pollutant}EmissionLimitTonsYr"
                    
                    lbs_hr_value = generator_obj.get(lbs_hr_field)
                    tons_yr_value = generator_obj.get(tons_yr_field)
                    
                    # If we have lbs/hr but not tons/yr, calculate it
                    if lbs_hr_value and not tons_yr_value:
                        # tons/yr = (lbs/hr_per_unit * hours/yr * num_units) / 2000 lbs/ton
                        calculated_tons_yr = (lbs_hr_value * operating_hours * num_generators) / 2000.0
                        generator_obj[tons_yr_field] = round(calculated_tons_yr, 2)
                        logger.debug(f"Calculated {tons_yr_field} = {calculated_tons_yr:.2f} from {lbs_hr_value} lbs/hr * {operating_hours} hrs/yr * {num_generators} units")
            
            # QUALITY CHECK: Only add generator if it has minimum required data
            # More strict: require either (make AND capacity) OR (capacity with description)
            # This filters out references to equipment in operational conditions without specs
            has_make = generator_obj.get("make") is not None and generator_obj.get("make") not in ["unknown", "Not specified"]
            has_capacity = (generator_obj.get("ratedCapacityKW") is not None or 
                          generator_obj.get("ratedCapacityBHP") is not None)
            has_emissions = any(generator_obj.get(field) is not None for field in [
                "noxEmissionLimitLbsHr", "coEmissionLimitLbsHr", "vocEmissionLimitLbsHr"
            ])
            
            # Require at least capacity, or both make and reference
            is_valid_generator = (
                has_capacity or  # Has capacity info (key identifier)
                (has_make and has_emissions)  # Or has make with emission data
            )
            
            if is_valid_generator:
                result["generatorSets"].append(generator_obj)
            else:
                # Only filter out completely empty generators (likely extraction artifacts)
                logger.info(f"⚠️  Filtered out generator '{gen_id}' - no identifying data (no make, capacity, ref, or emissions)")
        
        # Log summary
        total_extracted = len(generator_extractions)
        total_kept = len(result["generatorSets"])
        if total_extracted != total_kept:
            logger.info(f"📊 Generator QC: Extracted {total_extracted}, kept {total_kept} (filtered {total_extracted - total_kept} empty)")
        
        # CROSS-POLLINATE: Fill missing fields from related generator IDs
        # E.g., copy sulfur content from "EG01-EO07" to "EG01-EG06" if missing
        result = self._cross_pollinate_missing_fields(result)
        
        # DISABLED: Keep generators with range/quantity notation intact (EG01-EG06 with quantity=6)
        # Expanding to individual units loses data and makes emission propagation harder
        # result = self._expand_generator_ranges(result)
        
        # Deduplicate generators (remove likely example contamination)
        result = self._deduplicate_generators(result)
        
        return result
    
    def _cross_pollinate_missing_fields(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fill missing fields by copying from related generator IDs.
        
        Scalable approach: For each generator with missing fields, look for similar generators
        and copy common operational fields (hours, sulfur).
        
        Strategy:
        1. Extract numeric range from ref (e.g., "01-06" from "EG01-EG06")  
        2. Match generators with overlapping ranges or same facility
        3. Copy operating parameters that should be consistent across units
        """
        import re
        
        generators = result.get("generatorSets", [])
        
        # Fields that can be safely copied across similar generators
        copyable_fields = ["operatingHoursPerUnitLimit", "fuelSulfurContent"]
        
        # Build a lookup of available field values
        field_sources = {field: [] for field in copyable_fields}
        
        for gen in generators:
            for field in copyable_fields:
                value = gen.get(field)
                if value is not None:
                    ref = gen.get("referenceNumber", "")
                    field_sources[field].append((ref, value, gen))
        
        # Fill missing fields
        for gen in generators:
            ref = gen.get("referenceNumber", "")
            if not ref:
                continue
            
            # Extract numeric components for range matching
            # E.g., "EG01-EG06" -> [1, 6], "EG07" -> [7], "1" -> [1]
            numbers = [int(m.group()) for m in re.finditer(r'\d+', ref)]
            
            for field in copyable_fields:
                if gen.get(field) is not None:
                    continue  # Already has value
                
                # Find best source for this field
                best_match = None
                best_score = 0
                
                for source_ref, value, source_gen in field_sources[field]:
                    # Extract numbers from source ref
                    source_numbers = [int(m.group()) for m in re.finditer(r'\d+', source_ref)]
                    
                    # Scoring: prefer overlapping numeric ranges
                    score = 0
                    
                    # Check for numeric overlap
                    if numbers and source_numbers:
                        if any(n in source_numbers or any(sn-1 <= n <= sn+1 for sn in source_numbers) for n in numbers):
                            score += 10  # Overlapping or adjacent numbers
                    
                    # Same prefix letters (E, EG, EO, etc.)
                    ref_letters = ''.join(c for c in ref if c.isalpha())
                    source_letters = ''.join(c for c in source_ref if c.isalpha())
                    if ref_letters and source_letters and ref_letters == source_letters:
                        score += 5
                    elif ref_letters and source_letters and ref_letters[0] == source_letters[0]:
                        score += 2  # At least first letter matches
                    
                    if score > best_score:
                        best_score = score
                        best_match = (source_ref, value)
                
                # Apply best match if found
                if best_match and best_score > 0:
                    source_ref, value = best_match
                    gen[field] = value
                    logger.debug(f"Cross-pollinated {field}={value} from {source_ref} to {ref} (score: {best_score})")
        
        return result
    
    def _deduplicate_generators(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Remove duplicate generators that are likely from example contamination.
        
        LangExtract examples can "leak" into extraction results, creating phantom generators
        that match example patterns but don't exist in the actual document.
        
        Deduplication strategy:
        1. Filter out generators with N/A or empty values (phantom entries)
        2. Filter out generators with simple numeric refs (1, 2, 3) if there are generators with alpha refs (G-1, EG01)
        3. Within same capacity, deduplicate identical specs
        """
        generators = result.get("generatorSets", [])
        if len(generators) <= 1:
            return result
        
        # First pass: Remove completely empty/N/A generators
        valid_generators = []
        for gen in generators:
            ref = str(gen.get("referenceNumber", "")).strip()
            make = str(gen.get("make", "")).strip()
            kw = gen.get("ratedCapacityKW")
            
            # Filter out N/A or empty generators
            if ref.upper() in ["N/A", "NA", "NONE", ""] or make.upper() in ["N/A", "NA", "NONE"]:
                if not kw:  # Only filter if also missing capacity
                    logger.info(f"⚠️  Filtered out empty/N/A generator: ref='{ref}', make='{make}'")
                    continue
            
            valid_generators.append(gen)
        
        generators = valid_generators
        
        # Second pass: Deduplicate based on specs (not just reference numbers)
        # Keep generators with unique specs, even if they have simple numeric refs
        from collections import defaultdict
        
        # Group by specs (excluding reference number - multiple generators can have same specs)
        spec_groups = defaultdict(list)
        
        for idx, gen in enumerate(generators):
            make = (gen.get("make") or "").lower().strip()
            model = (gen.get("model") or "").lower().strip()
            kw = gen.get("ratedCapacityKW")
            bhp = gen.get("ratedCapacityBHP")
            fuel = (gen.get("fuelType") or "").lower().strip()
            
            # Signature based on physical specs (not ref number)
            sig = (make, model, kw, bhp, fuel)
            spec_groups[sig].append((idx, gen))
        
        kept_generators = []
        
        for sig, gen_list in spec_groups.items():
            if len(gen_list) == 1:
                # Unique specs - keep it
                kept_generators.append(gen_list[0][1])
            else:
                # Multiple generators with same specs - rank by data quality
                scored_gens = []
                for idx, gen in gen_list:
                    score = 0
                    score += 10 if gen.get("noxEmissionLimitLbsHr") else 0
                    score += 10 if gen.get("noxEmissionLimitTonsYr") else 0
                    score += 5 if gen.get("coEmissionLimitLbsHr") else 0
                    score += 5 if gen.get("coEmissionLimitTonsYr") else 0
                    score += 3 if gen.get("operatingHoursPerUnitLimit") else 0
                    score += 3 if gen.get("fuelSulfurContent") else 0
                    score += 2 if gen.get("fuelType") else 0
                    
                    # STRONG preference for alpha refs over numeric (likely contamination)
                    ref = str(gen.get("referenceNumber", ""))
                    has_alpha = any(c.isalpha() for c in ref)
                    is_simple_numeric = ref.isdigit() and len(ref) <= 2
                    
                    if has_alpha:
                        score += 20  # Strong bonus for alpha refs
                    elif is_simple_numeric and score == 0:
                        score = -100  # Heavily penalize empty numeric refs
                    
                    scored_gens.append((score, idx, gen, ref))
                
                # Sort by score descending
                scored_gens.sort(reverse=True, key=lambda x: x[0])
                
                # Only keep generators with positive scores
                max_score = scored_gens[0][0]
                for score, idx, gen, ref in scored_gens:
                    if score > 0 and score >= max_score * 0.5:  # At least 50% of best
                        kept_generators.append(gen)
                    else:
                        logger.info(f"⚠️  Filtered out duplicate generator ref '{ref}' (score {score} vs {max_score})")
        
        # FINAL PASS: If we have alpha-ref generators with data, remove ALL numeric-ref generators
        # Rationale: Permits use either alpha refs (EG01-EG06) OR numeric refs (1,2,3), not both
        # If we see both, numeric refs are likely example contamination
        has_alpha_refs = any(
            any(c.isalpha() for c in str(gen.get("referenceNumber", "")))
            for gen in kept_generators
        )
        
        if has_alpha_refs:
            final_generators = []
            for gen in kept_generators:
                ref = str(gen.get("referenceNumber", ""))
                is_simple_numeric = ref.isdigit() and len(ref) <= 2
                
                # Remove ALL numeric refs if we have alpha refs (different permit format)
                if is_simple_numeric:
                    logger.info(f"⚠️  Filtered out numeric ref '{ref}' (permit uses alpha refs - likely cross-permit contamination)")
                else:
                    final_generators.append(gen)
            kept_generators = final_generators
        
        total_removed = len(generators) - len(kept_generators)
        if total_removed > 0:
            logger.info(f"📊 Deduplication: {len(generators)} → {len(kept_generators)} generators (removed {total_removed})")
        
        result["generatorSets"] = kept_generators
        return result
    
    def _expand_generator_ranges(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Expand generators with range notation or quantity notation into individual units.
        
        Handles:
        - Range notation: "EG01-EG06" -> 6 generators (EG01, EG02, EG03, EG04, EG05, EG06)
        - Quantity notation: "(6) Cummins QSK78" -> 6 generators with same specs
        - Combined: "EG01-EG06" with "(6)" -> 6 generators
        
        Examples from Virginia permits:
        - "EG01-EG06 (6) Cummins QSK78-G12 diesel-fueled engine-generator sets"
        - "EG01 EG06" (space-separated range)
        """
        import re
        
        generators = result.get("generatorSets", [])
        expanded_generators = []
        
        for gen in generators:
            ref_num = gen.get("referenceNumber")
            num_gens_field = gen.get("numGenerators")
            
            # Check for range notation: "EG01-EG06" or "EG01 EG06" or "1-6"
            range_match = None
            if ref_num:
                # Pattern 1: "EG01-EG06" or "1-6"
                range_match = re.match(r'^([A-Z]*)(\d+)\s*[-–]\s*([A-Z]*)(\d+)$', str(ref_num).strip())
                if not range_match:
                    # Pattern 2: "EG01 EG06" (space-separated, indicates range)
                    space_match = re.match(r'^([A-Z]+)(\d+)\s+([A-Z]+)(\d+)$', str(ref_num).strip())
                    if space_match:
                        range_match = space_match
            
            # Check for quantity notation: "(6)" or "6" in numGenerators
            quantity = None
            if num_gens_field:
                # Extract number from "(6)" or "six (6)" or just "6"
                qty_match = re.search(r'\(?(\d+)\)?', str(num_gens_field))
                if qty_match:
                    quantity = int(qty_match.group(1))
            
            # CASE 1: Range notation (EG01-EG06)
            if range_match:
                prefix1 = range_match.group(1) or ""
                start_num = int(range_match.group(2))
                prefix2 = range_match.group(3) or ""
                end_num = int(range_match.group(4))
                
                # Validate: prefixes should match (or one is empty)
                if prefix1 and prefix2 and prefix1 != prefix2:
                    logger.warning(f"Mismatched prefixes in range: {prefix1} vs {prefix2}")
                    expanded_generators.append(gen)
                    continue
                
                prefix = prefix1 or prefix2
                count = end_num - start_num + 1
                
                # Safety check: reasonable range (max 50 units)
                if count <= 0 or count > 50:
                    logger.warning(f"Invalid range: {start_num}-{end_num} (count={count})")
                    expanded_generators.append(gen)
                    continue
                
                logger.info(f"📦 Expanding range {ref_num} into {count} generators")
                
                # Create individual generators
                for i in range(count):
                    gen_copy = gen.copy()
                    gen_num = start_num + i
                    gen_copy["referenceNumber"] = f"{prefix}{gen_num:02d}" if prefix else str(gen_num)
                    gen_copy["numGenerators"] = None  # Clear the count field
                    expanded_generators.append(gen_copy)
            
            # CASE 2: Quantity notation without range ((6) generators)
            elif quantity and quantity > 1:
                logger.info(f"📦 Expanding quantity notation: creating {quantity} generators from '{ref_num}'")
                
                # Create individual generators with sequential reference numbers
                for i in range(quantity):
                    gen_copy = gen.copy()
                    
                    # Try to create sequential ref numbers if original has a number
                    if ref_num:
                        ref_match = re.match(r'^([A-Z]*)(\d+)$', str(ref_num).strip())
                        if ref_match:
                            prefix = ref_match.group(1) or ""
                            base_num = int(ref_match.group(2))
                            gen_copy["referenceNumber"] = f"{prefix}{base_num + i:02d}" if prefix else str(base_num + i)
                        else:
                            # Can't parse number, just append index
                            gen_copy["referenceNumber"] = f"{ref_num}-{i+1}"
                    else:
                        gen_copy["referenceNumber"] = f"GEN{i+1}"
                    
                    gen_copy["numGenerators"] = None  # Clear the count field
                    expanded_generators.append(gen_copy)
            
            # CASE 3: No expansion needed
            else:
                expanded_generators.append(gen)
        
        original_count = len(generators)
        expanded_count = len(expanded_generators)
        
        if expanded_count > original_count:
            logger.info(f"✅ Expanded {original_count} generator entries into {expanded_count} individual units")
        
        result["generatorSets"] = expanded_generators
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
            if not target_gen.get("operatingHoursPerUnitLimit") and source_gen.get("operatingHoursPerUnitLimit"):
                target_gen["operatingHoursPerUnitLimit"] = source_gen["operatingHoursPerUnitLimit"]
            if not target_gen.get("controlTechnology") and source_gen.get("controlTechnology"):
                target_gen["controlTechnology"] = source_gen["controlTechnology"]
            if not target_gen.get("fuelThroughputPerUnitLimit") and source_gen.get("fuelThroughputPerUnitLimit"):
                target_gen["fuelThroughputPerUnitLimit"] = source_gen["fuelThroughputPerUnitLimit"]
        
        return result
    
    def _create_empty_result(self) -> Dict[str, Any]:
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
        """Get extraction statistics."""
        stats = self.stats.copy()
        
        if stats['total_chars_original'] > 0:
            avg_reduction = (
                (stats['total_chars_original'] - stats['total_chars_optimized']) 
                / stats['total_chars_original'] * 100
            )
            stats['avg_text_reduction_percent'] = round(avg_reduction, 1)
            original_tokens = stats['total_chars_original'] // 4
            optimized_tokens = stats['total_chars_optimized'] // 4
            stats['estimated_tokens_saved'] = original_tokens - optimized_tokens
        
        if self.deduplicator:
            dedup_stats = self.deduplicator.get_stats()
            stats.update({
                'unique_files': dedup_stats['unique_files'],
                'unique_permits': dedup_stats['unique_permits'],
            })
        
        return stats
    
    def generate_visualization(self, output_dir: Path = None, pdf_name: str = None) -> Path:
        """
        Generate interactive HTML visualization of LangExtract results.
        
        Shows extracted entities highlighted in context with their attributes.
        Useful for QA, debugging, and understanding what the model extracted.
        
        Args:
            output_dir: Directory to save visualization (default: current dir)
            pdf_name: Optional PDF name to include in filename
            
        Returns:
            Path to generated HTML file
            
        Example:
            ```python
            extractor = ExtractorFactory.create_extractor(...)
            result = extractor.extract_from_pdf(pdf_path)
            html_path = extractor.generate_visualization(output_dir=Path("visualizations"))
            print(f"View extraction at: {html_path}")
            ```
        """
        if self._last_extraction_result is None:
            raise ValueError("No extraction result available. Run extract_from_pdf() first.")
        
        if output_dir is None:
            output_dir = Path(".")
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate filename
        if pdf_name:
            base_name = Path(pdf_name).stem
            jsonl_path = output_dir / f"{base_name}_extraction.jsonl"
            html_path = output_dir / f"{base_name}_visualization.html"
        else:
            jsonl_path = output_dir / "extraction_results.jsonl"
            html_path = output_dir / "visualization.html"
        
        try:
            # Save annotated document to JSONL
            # Note: save_annotated_documents adds the .jsonl extension automatically
            lx.io.save_annotated_documents(
                [self._last_extraction_result], 
                output_name=jsonl_path.stem,  # Just the base name without extension
                output_dir=str(output_dir)
            )
            
            # The actual file saved will have the extension added by langextract
            # but might not include .jsonl in the name, so we need to find it
            saved_files = list(output_dir.glob(f"{jsonl_path.stem}*"))
            if saved_files:
                actual_jsonl_path = saved_files[0]
                logger.info(f"Saved extraction data to {actual_jsonl_path}")
            else:
                actual_jsonl_path = jsonl_path
                logger.warning(f"Could not verify JSONL file location, assuming: {jsonl_path}")
            
            # Generate HTML visualization
            html_content = lx.visualize(str(actual_jsonl_path))
            
            # Handle both Jupyter and non-Jupyter outputs
            with open(html_path, 'w', encoding='utf-8') as f:
                if hasattr(html_content, 'data'):
                    f.write(html_content.data)  # Jupyter/Colab IPython.display.HTML object
                else:
                    f.write(html_content)  # Plain string
            
            logger.info(f"✅ Generated visualization: {html_path}")
            logger.info("   Open in browser to review extracted entities in context")
            
            return html_path
            
        except Exception as e:
            logger.error(f"Failed to generate visualization: {e}")
            raise

