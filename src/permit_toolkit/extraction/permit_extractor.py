"""
Production permit extractor.

Architecture:
1. OpenAI Structured - Fast, accurate extraction with improved grouping
2. LangExtract QA/QC - Validates results and adds source citations for traceability

Cost: ~$0.002-0.004 per permit
Accuracy: 90%+ on validation set
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

import openai

from .qa_qc import QAQCValidator, ValidationReport

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Result from permit extraction."""
    data: Dict[str, Any]
    completeness_score: float  # 0-1 score based on fields populated
    cost: float
    processing_time: float
    validation_notes: List[str]
    langextract_result: Any = None
    validation_report: Optional[ValidationReport] = None


class PermitExtractor:
    """
    Production permit extractor with traceability.
    
    Two-stage approach:
    1. OpenAI Structured: Fast extraction with smart generator grouping
    2. LangExtract QA/QC: Validates critical fields and adds citations
    """
    
    def __init__(self, api_key: str, model: str = "gpt-4o-mini", enable_qa_qc_overrides: bool = True):
        self.api_key = api_key
        self.model = model
        self.client = openai.OpenAI(api_key=api_key)
        
        # Initialize QA/QC validator
        self.qa_qc_validator = QAQCValidator(enable_overrides=enable_qa_qc_overrides)
        
        # Try to import langextract for QA/QC
        try:
            import langextract as lx
            self.lx = lx
            self.langextract_available = True
        except ImportError:
            logger.warning("LangExtract not available - QA/QC citations will be limited")
            self.lx = None
            self.langextract_available = False
    
    def extract(
        self,
        text: str,
        schema: Dict[str, Any],
        enable_qa_qc: bool = True
    ) -> ExtractionResult:
        """
        Extract permit data with optional QA/QC validation.
        
        Args:
            text: Full permit text
            schema: JSON schema for validation
            enable_qa_qc: If True, run LangExtract QA/QC for traceability
            
        Returns:
            ExtractionResult with data, confidence, and optional LangExtract result for visualization
        """
        start_time = time.time()
        total_cost = 0.0
        validation_notes = []
        langextract_result = None
        validation_report = None
        
        # Stage 1: OpenAI Structured Extraction
        logger.info("🤖 Stage 1: OpenAI Structured Extraction")
        openai_result = self._extract_with_openai(text, schema)
        total_cost += openai_result['cost']
        validated_data = openai_result['data']
        
        # Stage 2: LangExtract QA/QC with universal examples (re-enabled)
        # Uses cross-state compatible examples (3 minimal formats)
        # Targets >30% coverage on both VA and IL
        if enable_qa_qc and self.langextract_available:
            logger.info("🔍 Stage 2: LangExtract QA/QC with Cross-Validation")
            qa_result = self._validate_with_langextract(text, openai_result['data'])
            
            # Get permit number for validation report
            permit_number = openai_result['data'].get('permitDetails', {}).get('permitNumber', 'unknown')
            
            # Run cross-validation and get detailed report
            if qa_result.get('extraction_result'):
                validated_data, validation_report = self.qa_qc_validator.validate(
                    openai_data=openai_result['data'],
                    langextract_result=qa_result['extraction_result'],
                    permit_number=permit_number
                )
                
                # Add report summary to validation notes
                validation_notes.append(f"Overall confidence: {validation_report.overall_confidence:.2%}")
                validation_notes.append(f"Fields validated: {validation_report.fields_validated}/{validation_report.total_fields}")
                if validation_report.overrides_applied > 0:
                    validation_notes.append(f"✓ {validation_report.overrides_applied} high-confidence override(s) applied")
                if validation_report.flags_for_review > 0:
                    validation_notes.append(f"⚠️  {validation_report.flags_for_review} field(s) flagged for review")
            else:
                validation_notes.extend(qa_result['validation_notes'])
            
            langextract_result = qa_result.get('extraction_result')
            total_cost += qa_result['cost']
        else:
            logger.info("⚠️  Skipping QA/QC (LangExtract not used or disabled)")
        
        # Stage 3: Post-extraction sanity checks and normalization
        validated_data = self._normalize_fuel_fields(validated_data)
        sanity_warnings = self._run_sanity_checks(validated_data)
        validation_notes.extend(sanity_warnings)
        
        processing_time = time.time() - start_time
        
        # Use validation report confidence if available, otherwise calculate completeness
        if validation_report:
            completeness = validation_report.overall_confidence
        else:
            completeness = self._calculate_completeness(
                validated_data,
                validation_notes
            )
        
        return ExtractionResult(
            data=validated_data,
            completeness_score=completeness,
            cost=total_cost,
            processing_time=processing_time,
            validation_notes=validation_notes,
            langextract_result=langextract_result,
            validation_report=validation_report
        )
    
    def _normalize_with_schema(self, data: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Ensure all schema fields are present in extracted data, filling missing fields with null.
        This prevents data loss when OpenAI omits optional fields.
        """
        # Get schema properties
        schema_props = schema.get('properties', {})
        
        # Normalize permitDetails
        if 'permitDetails' in schema_props and 'permitDetails' in data:
            permit_schema = schema_props['permitDetails'].get('properties', {})
            for field in permit_schema.keys():
                if field not in data['permitDetails']:
                    data['permitDetails'][field] = None
        
        # Normalize generatorSets
        if 'generatorSets' in schema_props and 'generatorSets' in data:
            generator_schema = schema_props['generatorSets'].get('items', {}).get('properties', {})
            for generator in data.get('generatorSets', []):
                for field in generator_schema.keys():
                    if field not in generator:
                        generator[field] = None
        
        return data
    
    def _clean_emission_zeros(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert emission limit zeros to null.
        
        OpenAI sometimes returns 0 instead of null for missing emission limits.
        A true "0 tons/yr" limit would be unusual and should be explicit in the permit.
        This prevents misleading data where 0 could be confused with "not specified".
        """
        emission_fields = [
            'noxEmissionLimitLbsHr', 'noxEmissionLimitTonsYr',
            'coEmissionLimitLbsHr', 'coEmissionLimitTonsYr',
            'vocEmissionLimitLbsHr', 'vocEmissionLimitTonsYr',
            'so2EmissionLimitLbsHr', 'so2EmissionLimitTonsYr',
            'pmEmissionLimitLbsHr', 'pmEmissionLimitTonsYr',
            'pm10EmissionLimitLbsHr', 'pm10EmissionLimitTonsYr',
            'pm25EmissionLimitLbsHr', 'pm25EmissionLimitTonsYr',
        ]
        
        for generator in data.get('generatorSets', []):
            for field in emission_fields:
                if field in generator and generator[field] == 0:
                    generator[field] = None
        
        return data
    
    def _normalize_fuel_fields(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize fuel-related fields for consistency across extractions.
        
        Ensures consistent handling of:
        - ASTM specification formats (D396-76 not 0396-76)
        - Multi-grade fuel descriptions
        """
        for generator in data.get('generatorSets', []):
            # Fix ASTM specification format: "ASTM 0396-76" → "ASTM D396-76"
            fuel_spec = generator.get('fuelSpecification')
            if fuel_spec and isinstance(fuel_spec, str):
                # Fix leading zero in ASTM spec (common OCR/extraction error)
                if 'ASTM 0' in fuel_spec or 'ASTM  0' in fuel_spec:
                    generator['fuelSpecification'] = fuel_spec.replace('ASTM 0', 'ASTM D').replace('ASTM  0', 'ASTM D')
            
            # Check for multi-grade fuel descriptions
            primary_fuel = generator.get('primaryFuelType', '')
            fuel_grade = generator.get('fuelGrade', '')
            extraction_notes = generator.get('extractionNotes') or ''
            
            if primary_fuel and isinstance(primary_fuel, str) and fuel_grade and isinstance(fuel_grade, str):
                # If fuelGrade indicates multiple grades but primaryFuelType is specific, warn
                multi_grade_indicators = ['numbers 1 or 2', 'no. 1 or 2', 'grades no. 1 and 2', 'no. 1 and no. 2']
                has_multi_grade = any(indicator in fuel_grade.lower() for indicator in multi_grade_indicators)
                
                if has_multi_grade and 'no. 2' in primary_fuel.lower() and 'distillate' not in primary_fuel.lower():
                    # Fuel grade allows multiple but primary was normalized to specific grade
                    # Add clarifying note if not already present
                    if 'fuel specification allows' not in extraction_notes.lower() and 'multiple grade' not in extraction_notes.lower():
                        note = f"Fuel specification allows {fuel_grade}; set primaryFuelType to 'no. 2 distillate' per extraction guideline."
                        generator['extractionNotes'] = note if not extraction_notes else f"{extraction_notes} {note}"
        
        return data
    
    def _extract_with_openai(self, text: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract using OpenAI - extract generators exactly as listed in permit.
        """
        # Use more text to capture all data
        text_excerpt = text[:35000]
        
        prompt = f"""Extract ALL data from this air quality permit. Return valid JSON matching the schema.

CRITICAL: Extract generators EXACTLY as structured in the permit document.

EXAMPLES OF CORRECT EXTRACTION:

Example 1 - Range notation (ONE entry):
Document shows: "EG01-EG06 (6) Cummins QSK78-G12 diesel-fueled generators, 2500 kW each"
Correct JSON:
{{
  "referenceNumber": "EG01-EG06",
  "numGenerators": 6,
  "make": "Cummins",
  "model": "QSK78-G12",
  "ratedCapacityKW": 2500
}}

Example 2 - Separate rows with numeric references (MULTIPLE entries):
Document shows:
| Equipment Description                                    | Ref No. | Capacity       |
|----------------------------------------------------------|---------|----------------|
| One Caterpillar 1500 KW diesel powered emergency gen.  | 3       | 2500 BHP      |
| One Caterpillar 1500 KW diesel powered emergency gen.  | 2       | 2500 BHP      |
| One Caterpillar 1500 KW diesel powered emergency gen.  | 1       | 2500 BHP      |

Correct JSON (3 separate entries, preserving numeric references):
[
  {{"referenceNumber": "3", "numGenerators": 1, "make": "Caterpillar", "model": null, "ratedCapacityBHP": 2500, "ratedCapacityKW": 1500}},
  {{"referenceNumber": "2", "numGenerators": 1, "make": "Caterpillar", "model": null, "ratedCapacityBHP": 2500, "ratedCapacityKW": 1500}},
  {{"referenceNumber": "1", "numGenerators": 1, "make": "Caterpillar", "model": null, "ratedCapacityBHP": 2500, "ratedCapacityKW": 1500}}
]

Example 3 - Separate rows with alphanumeric references (MULTIPLE entries):
Document shows:
| Unit | Make | Model | Capacity |
|------|------|-------|----------|
| EG01 | CAT  | 3512  | 1500 kW  |
| EG02 | CAT  | 3512  | 1500 kW  |
| EG03 | CAT  | 3512  | 1500 kW  |

Correct JSON (3 separate entries):
[
  {{"referenceNumber": "EG01", "numGenerators": 1, "make": "CAT", "model": "3512", "ratedCapacityKW": 1500}},
  {{"referenceNumber": "EG02", "numGenerators": 1, "make": "CAT", "model": "3512", "ratedCapacityKW": 1500}},
  {{"referenceNumber": "EG03", "numGenerators": 1, "make": "CAT", "model": "3512", "ratedCapacityKW": 1500}}
]

EXTRACTION RULES:
1. For referenceNumber: Copy EXACTLY as shown in "Ref No." or "Reference No." column - DO NOT add prefixes
   - Look for reference numbers in equipment tables (may appear before OR after equipment description)
   - Examples of CORRECT extraction:
     * If document shows "Ref No. 3" → use "3" (NOT "EG03")
     * If document shows "EG01" → use "EG01"
     * If document shows "EG01-EG06" → use "EG01-EG06"
   - NEVER add "EG" prefix or normalize format - use the EXACT text from the permit
   - NEVER invent reference numbers not explicitly in the document

2. For numGenerators: Parse from range notation OR count separate rows
   - Range notation (e.g., "EG01-EG06" for 6 generators) → numGenerators: 6
   - Separate rows for each unit → numGenerators: 1 for each entry

3. Extract ALL fields including:
   - Make, model, capacity (kW/BHP)
   - Fuel type: Use "no. 2 distillate" for diesel/distillate oil unless "no. 1 distillate" is specified
   - Fuel sulfur content: Extract as decimal (e.g., 0.005 for 0.5%, 0.0015 for 0.0015%)
   - Fuel throughput limit: Extract gallons/year from "Fuel Throughput" conditions, or null if not specified
   - Control technology: Extract from "Emission Controls" conditions, or null if not specified
   - Operating hours limit: Extract hours/year from permit conditions
   - Emission limits: CAREFULLY match pollutant names
     * "PM-10" or "PM10" → pm10EmissionLimitLbsHr/TonsYr
     * "PM-2.5" or "PM2.5" → pm25EmissionLimitLbsHr/TonsYr  
     * "PM" or "Particulate Matter" (without numbers) → pmEmissionLimitLbsHr/TonsYr
     * Extract both lbs/hr and tons/yr for each pollutant found

SCHEMA:
{json.dumps(schema, indent=2)}

CRITICAL EXTRACTION GUIDELINES:

1. PERMIT DETAILS:
   - Extract permit number, issue date, expiration date, facility name, address, county from header

2. GENERATORS - MAINTAIN SOURCE STRUCTURE:
   - Create ONE entry per row/line in the equipment table
   - Do not combine or split entries
   - Match generator reference numbers to their specific limits in permit conditions

3. FUEL SPECIFICATIONS:
   - primaryFuelType: Extract EXACTLY as written in permit (e.g., "diesel fuel", "distillate oil", "No. 2 fuel oil")
   - fuelGrade: Extract explicit grade mentions (e.g., "No. 2", "numbers 1 or 2") - copy verbatim
   - fuelSpecification: Extract ASTM standards exactly (e.g., "ASTM D396-76" NOT "ASTM 0396-76")
   - When permit says "numbers 1 or 2 fuel oil" or "Grades No. 1 and 2":
     * primaryFuelType: "distillate oil" (generic term when multiple grades allowed)
     * fuelGrade: record the exact phrase (e.g., "numbers 1 or 2 fuel oil")
     * Add to extractionNotes: "Fuel specification allows [grades]; recorded as distillate oil per multiple grade allowance"
   - fuelSulfurContent: Extract as decimal (0.5% = 0.005, 15 ppm = 0.000015)

4. FUEL THROUGHPUT:
   - Look for conditions like "Fuel Throughput - The engine-generator sets (Ref. Nos. EG##-EG##) combined shall consume no more than #### gallons"
   - This is the TOTAL for the group - record it for each generator in that group
   - Convert to numeric value (remove commas)
   - If NO fuel throughput limit is specified in the permit, set to null (NOT zero)

5. CONTROL TECHNOLOGY:
   - Extract from "Emission Controls" section
   - Look for phrases like "controlled by", "turbocharged", "aftercooler", "SCR", etc.
   - If NOT specified, set to null

5. CONTROL TECHNOLOGY:
   - Extract from "Emission Controls" section
   - Look for phrases like "controlled by", "turbocharged", "aftercooler", "SCR", etc.
   - If NOT specified, set to null

6. EMISSION LIMITS AND AGGREGATION:
   - Match generator reference numbers to emission limit conditions
   - CRITICAL: Pollutant name mapping:
     * "VOC", "TVOC", or "VOM" (Volatile Organic Material - Illinois term) → vocEmissionLimitLbsHr/TonsYr
     * "NOx", "NO_x", "Nitrogen Oxides" → noxEmissionLimitLbsHr/TonsYr
     * "CO", "Carbon Monoxide" → coEmissionLimitLbsHr/TonsYr
     * "PM-10" or "PM10" → pm10EmissionLimitLbsHr/TonsYr
     * "PM-2.5" or "PM2.5" → pm25EmissionLimitLbsHr/TonsYr
     * "PM" or "Particulate Matter" (without suffix) → pmEmissionLimitLbsHr/TonsYr
     * "SO2", "Sulfur Dioxide" → so2EmissionLimitLbsHr/TonsYr
   - Extract "Each" limits (per generator) for lbs/hr
   - Extract "Combined" limits (for group) for tons/yr if "Each" not available
   - Set to null if not specified
   
   - AGGREGATION TYPES (CRITICAL - DO NOT SKIP):
     * instantEmissionsAggregationType: Look for phrases with lbs/hr limits like:
       - "for each generator" / "per generator" / "each unit" → record verbatim
       - "combined" / "total" / "facility-wide" → record verbatim
       - If permit just lists value without scope, set to null
     * cumulativeEmissionsAggregationType: Look for phrases with tons/yr limits like:
       - "for each generator" / "per generator" / "each unit" → record verbatim
       - "combined" / "total" / "facility-wide" → record verbatim
       - If permit just lists value without scope, set to null

DOCUMENT TEXT:
{text_excerpt}

Return valid JSON following the schema exactly. Match the permit's structure - do not impose grouping.
"""
        
        # Prepare API call parameters
        api_params = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are an expert at extracting data from air quality permits. Extract exactly as shown in source document."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"}
        }
        
        # Only add temperature for models that support it (not gpt-5, o1, o3, etc.)
        if not any(x in self.model.lower() for x in ['gpt-5', 'o1', 'o3', 'o4']):
            api_params["temperature"] = 0
        
        try:
            response = self.client.chat.completions.create(**api_params)
            
            data = json.loads(response.choices[0].message.content)
            
            # Ensure all schema fields are present (fill missing with null)
            data = self._normalize_with_schema(data, schema)
            
            # Post-process: Convert emission limit zeros to null
            # (OpenAI sometimes returns 0 for missing values instead of null)
            data = self._clean_emission_zeros(data)
            
            # Calculate cost
            usage = response.usage
            cost = self._calculate_openai_cost(usage.prompt_tokens, usage.completion_tokens)
            
            logger.info(f"  ✓ Extracted {len(data.get('generatorSets', []))} generator entries")
            
            return {
                'data': data,
                'cost': cost
            }
            
        except Exception as e:
            logger.error(f"  ✗ OpenAI extraction failed: {e}")
            return {
                'data': {'permitDetails': {}, 'generatorSets': []},
                'cost': 0.0
            }
    
    def _validate_with_langextract(
        self,
        text: str,
        openai_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Use LangExtract to validate critical fields for QA/QC.
        
        Returns the raw extraction result for visualization, not inline citations.
        """
        validation_notes = []
        cost = 0.0
        extraction_result = None
        
        if not self.langextract_available:
            return {'extraction_result': None, 'validation_notes': validation_notes, 'cost': cost}
        
        try:
            # Use comprehensive examples covering all critical fields
            examples = self._create_langextract_examples()
            
            # Extract with LangExtract for QA/QC - COMPREHENSIVE PROMPT
            extraction_result = self.lx.extract(
                text_or_documents=text,
                prompt_description="""Extract ALL generator/equipment data for comprehensive QA/QC validation.

CRITICAL: Extract EVERY instance of the following information types:

1. GENERATOR IDENTIFICATION:
   - Equipment reference numbers (EG01, EG01-EG06, Gen-1, etc.)
   - Number of generators in each set
   - Make and model of equipment

2. CAPACITY SPECIFICATIONS:
   - Rated capacity in kilowatts (kW)
   - Rated capacity in brake horsepower (bhp or hp)
   - Maximum capacity if different from rated

3. OPERATING LIMITS:
   - Operating hours per year (e.g., "500 hours per year", "≤218 hrs/yr")
   - Fuel throughput limits (gallons per year)

4. FUEL SPECIFICATIONS:
   - Fuel type (diesel, distillate, natural gas, etc.)
   - Sulfur content (%, ppm, or decimal)
   - Control technology descriptions

5. EMISSION LIMITS (MOST CRITICAL):
   Extract ALL pollutant limits in BOTH lbs/hr AND tons/yr:
   - Nitrogen Oxides (NOx, NO_x)
   - Carbon Monoxide (CO)
   - Volatile Organic Compounds (VOC, TVOC, VOM - use "voc" for VOM)
   - Particulate Matter (PM, PM-10, PM10, PM-2.5, PM2.5)
   - Sulfur Dioxide (SO2, SO_2)
   
   NOTE: Illinois permits use "VOM" (Volatile Organic Material) - treat as VOC
   
   Look for patterns like:
   "NOx: 53.7 lbs/hr, 83.75 tons/yr"
   "CO emissions shall not exceed 3.85 lbs/hr"
   "PM-10: 0.36 lbs/hr and 0.58 tons per year"
   "VOM: 0.075 g/bhp-hr (27.58 lbs/hr, 120.9 tons/yr)"

IMPORTANT: For each extraction, capture the EXACT source text from the permit for traceability.""",
                examples=examples,
                api_key=self.api_key,
                model_id=self.model
            )
            
            # Verify extraction result has valid structure
            if not extraction_result or not hasattr(extraction_result, 'extractions'):
                logger.warning("  ⚠️  LangExtract returned invalid result, skipping QA/QC")
                validation_notes.append("QA/QC: Skipped due to invalid LangExtract response")
                return {'extraction_result': None, 'validation_notes': validation_notes, 'cost': 0.0}
            
            # Cross-validate with OpenAI results
            openai_generators = openai_data.get('generatorSets', [])
            langextract_gen_ids = set()
            
            for extraction in extraction_result.extractions:
                if extraction.extraction_class == "GENERATOR":
                    gen_id = extraction.attributes.get("generator_id") if extraction.attributes else None
                    if gen_id:
                        langextract_gen_ids.add(gen_id)
            
            # Validation summary (for JSON output)
            validation_notes.append(f"OpenAI: {len(openai_generators)} generator sets")
            validation_notes.append(f"LangExtract: {len(langextract_gen_ids)} references validated")
            validation_notes.append(f"Extractions: {len(extraction_result.extractions)} source citations")
            
            # Calculate total generator units
            total_units = sum(
                g.get('numGenerators', 1) if isinstance(g.get('numGenerators'), int)
                else int(str(g.get('numGenerators', '1')).split()[0]) if g.get('numGenerators')
                else 1
                for g in openai_generators
            )
            validation_notes.append(f"Total units: {total_units}")
            
            cost = 0.002  # Approximate LangExtract cost
            
            logger.info(f"  ✓ QA/QC: {len(extraction_result.extractions)} extractions, "
                       f"{len(langextract_gen_ids)} generators validated")
            
        except Exception as e:
            logger.warning(f"  ⚠️  LangExtract QA/QC failed: {e}")
            validation_notes.append(f"QA/QC error: {str(e)}")
        
        return {
            'extraction_result': extraction_result,  # Raw result for visualization
            'validation_notes': validation_notes,
            'cost': cost
        }
    
    def _create_langextract_examples(self) -> List:
        """
        Create UNIVERSAL LangExtract examples that work across state formats.
        
        Design principles:
        - Minimal format assumptions (handles lists, tables, paragraphs)
        - Semantic content focus (extract meaning, not format)
        - Multiple pollutant variations (VOC, VOM, full names)
        - Flexible reference patterns (EG##, G-#, numeric)
        - Simplified text to avoid format-specific brittleness
        """
        return [
            # Example 1: Compact emission list (works for VA bullet lists)
            self.lx.data.ExampleData(
                text="""EG01-EG06: 6 generators, 2500 kW
NOx 53.7 lbs/hr 83.75 tons/yr
CO 3.85 lbs/hr 6.05 tons/yr
VOC 1.29 lbs/hr 2.01 tons/yr
Hours: 500/year""",
                extractions=[
                    self.lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="NOx 53.7 lbs/hr 83.75 tons/yr",
                        attributes={"generator_id": "EG01-EG06", "pollutant": "nox", "lbs_hr": "53.7", "tons_yr": "83.75"}
                    ),
                    self.lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="CO 3.85 lbs/hr 6.05 tons/yr",
                        attributes={"generator_id": "EG01-EG06", "pollutant": "co", "lbs_hr": "3.85", "tons_yr": "6.05"}
                    ),
                    self.lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="VOC 1.29 lbs/hr 2.01 tons/yr",
                        attributes={"generator_id": "EG01-EG06", "pollutant": "voc", "lbs_hr": "1.29", "tons_yr": "2.01"}
                    ),
                    self.lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="500/year",
                        attributes={"generator_id": "EG01-EG06", "type": "hours", "value": "500"}
                    ),
                ]
            ),
            # Example 2: Table format with full names (works for IL tables)
            self.lx.data.ExampleData(
                text="""G-1 thru G-6: six 2000 kW engines
Nitrogen Oxides (NOx) 55.16 lbs/hr 66.19 tons/yr
Carbon Monoxide (CO) 11.63 lbs/hr 13.95 tons/yr
Volatile Organic Material (VOM) 1.60 lbs/hr 1.92 tons/yr  
Particulate Matter (PM) 0.73 lbs/hr 0.88 tons/yr""",
                extractions=[
                    self.lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Nitrogen Oxides (NOx) 55.16 lbs/hr 66.19 tons/yr",
                        attributes={"generator_id": "G-1 thru G-6", "pollutant": "nox", "lbs_hr": "55.16", "tons_yr": "66.19"}
                    ),
                    self.lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Carbon Monoxide (CO) 11.63 lbs/hr 13.95 tons/yr",
                        attributes={"generator_id": "G-1 thru G-6", "pollutant": "co", "lbs_hr": "11.63", "tons_yr": "13.95"}
                    ),
                    self.lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Volatile Organic Material (VOM) 1.60 lbs/hr 1.92 tons/yr",
                        attributes={"generator_id": "G-1 thru G-6", "pollutant": "voc", "lbs_hr": "1.60", "tons_yr": "1.92"}
                    ),
                    self.lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="Particulate Matter (PM) 0.73 lbs/hr 0.88 tons/yr",
                        attributes={"generator_id": "G-1 thru G-6", "pollutant": "pm", "lbs_hr": "0.73", "tons_yr": "0.88"}
                    ),
                ]
            ),
            # Example 3: Annual only (simplified)
            self.lx.data.ExampleData(
                text="""Ref. 3: Caterpillar 1500 kW
Annual emissions:
NOx 15.98 tons/yr
CO 3.44 tons/yr
PM-10 1.12 tons/yr
Operating: 500 hours/yr""",
                extractions=[
                    self.lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="NOx 15.98 tons/yr",
                        attributes={"generator_id": "3", "pollutant": "nox", "tons_yr": "15.98"}
                    ),
                    self.lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="CO 3.44 tons/yr",
                        attributes={"generator_id": "3", "pollutant": "co", "tons_yr": "3.44"}
                    ),
                    self.lx.data.Extraction(
                        extraction_class="EMISSION",
                        extraction_text="PM-10 1.12 tons/yr",
                        attributes={"generator_id": "3", "pollutant": "pm10", "tons_yr": "1.12"}
                    ),
                    self.lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="500 hours/yr",
                        attributes={"generator_id": "3", "type": "hours", "value": "500"}
                    ),
                ]
            ),
        ]
    
    def _run_sanity_checks(self, data: Dict[str, Any]) -> List[str]:
        """
        Run post-extraction sanity checks to catch obvious errors.
        
        Returns list of warning messages.
        """
        warnings = []
        generators = data.get('generatorSets', [])
        
        if not generators:
            warnings.append("⚠️ WARNING: No generators extracted")
            return warnings
        
        # Check 1: Duplicate reference numbers
        ref_numbers = [g.get('referenceNumber') for g in generators if g.get('referenceNumber')]
        duplicates = [ref for ref in set(ref_numbers) if ref_numbers.count(ref) > 1]
        if duplicates:
            warnings.append(f"⚠️ WARNING: Duplicate reference numbers: {', '.join(duplicates)}")
        
        # Check 2: Missing critical fields and alternative options
        for i, gen in enumerate(generators):
            ref = gen.get('referenceNumber', f'Entry {i+1}')
            make = gen.get('make', '')
            model = gen.get('model', '')
            
            if not make:
                warnings.append(f"⚠️ WARNING: {ref} missing make")
            elif ' or ' in make.lower() or ' / ' in make:
                warnings.append(f"⚠️ WARNING: {ref} has multiple make options: '{make}' - schema requires single deterministic choice")
            
            if not model:
                warnings.append(f"⚠️ WARNING: {ref} missing model")
            elif ' or ' in model.lower() or ' / ' in model:
                warnings.append(f"⚠️ WARNING: {ref} has multiple model options: '{model}' - schema requires single deterministic choice")
            
            if not gen.get('ratedCapacityKW') and not gen.get('ratedCapacityBHP'):
                warnings.append(f"⚠️ WARNING: {ref} missing capacity")
            
            # Check for multiple capacity values in same field
            rated_kw = gen.get('ratedCapacityKW')
            rated_bhp = gen.get('ratedCapacityBHP')
            if rated_kw and isinstance(rated_kw, str) and ' or ' in str(rated_kw).lower():
                warnings.append(f"⚠️ WARNING: {ref} has multiple kW options: '{rated_kw}' - schema requires smallest value")
            if rated_bhp and isinstance(rated_bhp, str) and ' or ' in str(rated_bhp).lower():
                warnings.append(f"⚠️ WARNING: {ref} has multiple BHP options: '{rated_bhp}' - schema requires smallest value")
        
        # Check 3: Unrealistic values
        for gen in generators:
            ref = gen.get('referenceNumber', 'Unknown')
            num_gens = gen.get('numGenerators', 1)
            if num_gens and (num_gens < 1 or num_gens > 100):
                warnings.append(f"⚠️ WARNING: {ref} has unrealistic numGenerators: {num_gens}")
            
            capacity = gen.get('ratedCapacityKW')
            if capacity and (capacity < 10 or capacity > 50000):
                warnings.append(f"⚠️ WARNING: {ref} has unrealistic capacity: {capacity} kW")
        
        # Check 4: Total generator count
        total_units = sum(g.get('numGenerators', 1) for g in generators)
        if total_units > 50:
            warnings.append(f"⚠️ WARNING: High total generator count: {total_units} (check for extraction errors)")
        
        # Check 5: Critical extractionNotes validation
        # Check permit-level extractionNotes for multiple IDs
        permit_details = data.get('permitDetails', {})
        permit_number = permit_details.get('permitNumber', '')
        permit_notes = permit_details.get('extractionNotes')
        
        # Look for indicators of multiple IDs in permit number field itself
        if any(indicator in str(permit_number).lower() for indicator in ['application no', 'id no', 'permit no']):
            if not permit_notes:
                warnings.append("⚠️ INFO: Permit number may contain multiple identifiers but no extractionNotes provided")
        
        # Check generator-level extractionNotes for critical fields
        for gen in generators:
            ref = gen.get('referenceNumber', 'Unknown')
            gen_notes = gen.get('extractionNotes')
            
            # If emissionsScope is set but emissionsGroupRef is null and vice versa, might need notes
            scope = gen.get('emissionsScope')
            group_ref = gen.get('emissionsGroupRef')
            if scope in ['combined_group', 'facility_wide'] and not group_ref:
                if not gen_notes or 'emission' not in gen_notes.lower():
                    warnings.append(f"⚠️ INFO: {ref} has emissionsScope='{scope}' without emissionsGroupRef or clarifying notes")
            
            # If fuelNormalized is populated, ideally should have notes explaining mapping
            fuel_norm = gen.get('fuelNormalized')
            if fuel_norm and fuel_norm not in [None, 'other']:
                primary_fuel = gen.get('primaryFuelType', '')
                # Only suggest notes if the normalization isn't obvious
                if fuel_norm and not any(norm_hint in primary_fuel.lower() for norm_hint in ['no. 2', 'no. 1', 'natural gas', 'propane']):
                    if not gen_notes or 'fuel' not in gen_notes.lower():
                        warnings.append(f"ℹ️ INFO: {ref} has fuelNormalized='{fuel_norm}' - consider adding extractionNotes to document mapping")
        
        if warnings:
            logger.warning(f"Sanity checks found {len(warnings)} issues")
        else:
            logger.info("✓ All sanity checks passed")
        
        return warnings
    
    def generate_visualization(
        self,
        extraction_result: ExtractionResult,
        output_dir: Path,
        permit_number: str
    ) -> Path:
        """
        Generate HTML visualization with interactive source citations.
        
        Args:
            extraction_result: Result from extract()
            output_dir: Directory to save visualization
            permit_number: Permit number for filename
            
        Returns:
            Path to generated HTML file
        """
        if not extraction_result.langextract_result:
            logger.warning("No LangExtract result available for visualization")
            return None
        
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            # Save annotated JSONL for LangExtract viewer
            jsonl_filename = f"{permit_number}_annotated"
            
            self.lx.io.save_annotated_documents(
                [extraction_result.langextract_result],
                output_name=jsonl_filename,
                output_dir=str(output_dir)
            )
            
            # LangExtract saves without extension, find the actual file
            jsonl_path = output_dir / jsonl_filename
            if not jsonl_path.exists():
                # Try with common extensions
                for ext in ['', '.jsonl', '.json']:
                    test_path = output_dir / f"{jsonl_filename}{ext}"
                    if test_path.exists():
                        jsonl_path = test_path
                        break
            
            logger.info(f"  ✓ Saved annotated data: {jsonl_path}")
            
            # Generate interactive HTML visualization
            html_path = output_dir / f"{permit_number}_visualization.html"
            html_content = self.lx.visualize(str(jsonl_path))
            
            with open(html_path, 'w') as f:
                if hasattr(html_content, 'data'):
                    f.write(html_content.data)  # For Jupyter/Colab
                else:
                    f.write(html_content)
            
            logger.info(f"  ✓ Saved HTML visualization: {html_path}")
            logger.info(f"  📊 Open {html_path.name} in browser for interactive source highlighting")
            
            return jsonl_path
            
        except Exception as e:
            logger.error(f"  ✗ Failed to generate visualization: {e}")
            return None
    
    def _calculate_completeness(
        self,
        data: Dict[str, Any],
        validation_notes: List[str]
    ) -> float:
        """
        Calculate completeness score based on fields populated.
        
        Score breakdown:
        - 0.5: Base score
        - 0.1: Has permit number
        - 0.1: Has facility name
        - 0.1: Has generator sets
        - 0.1: Complete generator specs (make/model/capacity)
        - 0.2: Has emissions data
        
        Returns:
            float: Score from 0-1 indicating data completeness
        """
        score = 0.5
        
        # Permit details
        permit_details = data.get('permitDetails', {})
        if permit_details.get('permitNumber'):
            score += 0.1
        if permit_details.get('facilityName'):
            score += 0.1
        
        # Generators
        generators = data.get('generatorSets', [])
        if generators:
            score += 0.1
            
            # Complete specs
            complete = sum(
                1 for g in generators
                if g.get('make') and g.get('model') and g.get('ratedCapacityKW')
            )
            score += 0.1 * (complete / len(generators))
            
            # Emissions
            with_emissions = sum(
                1 for g in generators
                if g.get('noxEmissionLimitLbsHr')
            )
            score += 0.2 * (with_emissions / len(generators))
        
        return min(score, 1.0)
    
    def _calculate_openai_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Calculate OpenAI API cost."""
        # gpt-4o-mini pricing: $0.150/1M input, $0.600/1M output
        input_cost = (prompt_tokens / 1_000_000) * 0.150
        output_cost = (completion_tokens / 1_000_000) * 0.600
        return input_cost + output_cost
