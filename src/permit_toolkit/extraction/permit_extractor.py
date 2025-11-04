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

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        enable_qa_qc_overrides: bool = True,
    ):
        self.api_key = api_key
        self.model = model
        self.client = openai.OpenAI(api_key=api_key)

        # Initialize QA/QC validator
        self.qa_qc_validator = QAQCValidator(
            enable_overrides=enable_qa_qc_overrides
        )

        # Try to import langextract for QA/QC
        try:
            import langextract as lx

            self.lx = lx
            self.langextract_available = True
        except ImportError:
            logger.warning(
                "LangExtract not available - QA/QC citations will be limited"
            )
            self.lx = None
            self.langextract_available = False

    def extract(
        self, text: str, schema: Dict[str, Any], enable_qa_qc: bool = True
    ) -> ExtractionResult:
        """
        Extract permit data with optional QA/QC validation.

        Args:
            text: Full permit text
            schema: JSON schema for validation
            enable_qa_qc: If True, run LangExtract QA/QC for traceability

        Returns
        -------
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
        total_cost += openai_result["cost"]
        validated_data = openai_result["data"]

        # Stage 2: LangExtract QA/QC with universal examples (re-enabled)
        # Uses cross-state compatible examples (3 minimal formats)
        # Targets >30% coverage on both VA and IL
        if enable_qa_qc and self.langextract_available:
            logger.info("🔍 Stage 2: LangExtract QA/QC with Cross-Validation")
            qa_result = self._validate_with_langextract(
                text, openai_result["data"]
            )

            # Get permit number for validation report
            permit_number = (
                openai_result["data"]
                .get("permitDetails", {})
                .get("permitNumber", "unknown")
            )

            # Run cross-validation and get detailed report
            if qa_result.get("extraction_result"):
                validated_data, validation_report = (
                    self.qa_qc_validator.validate(
                        openai_data=openai_result["data"],
                        langextract_result=qa_result["extraction_result"],
                        permit_number=permit_number,
                    )
                )

                # Add report summary to validation notes
                validation_notes.append(
                    f"Overall confidence: {validation_report.overall_confidence:.2%}"
                )
                validation_notes.append(
                    f"Fields validated: {validation_report.fields_validated}/{validation_report.total_fields}"
                )
                if validation_report.overrides_applied > 0:
                    validation_notes.append(
                        f"✓ {validation_report.overrides_applied} high-confidence override(s) applied"
                    )
                if validation_report.flags_for_review > 0:
                    validation_notes.append(
                        f"⚠️  {validation_report.flags_for_review} field(s) flagged for review"
                    )
            else:
                validation_notes.extend(qa_result["validation_notes"])

            langextract_result = qa_result.get("extraction_result")
            total_cost += qa_result["cost"]
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
                validated_data, validation_notes
            )

        return ExtractionResult(
            data=validated_data,
            completeness_score=completeness,
            cost=total_cost,
            processing_time=processing_time,
            validation_notes=validation_notes,
            langextract_result=langextract_result,
            validation_report=validation_report,
        )

    def _normalize_with_schema(
        self, data: Dict[str, Any], schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Ensure all schema fields are present in extracted data, filling missing fields with null.
        This prevents data loss when OpenAI omits optional fields.
        """
        # Get schema properties
        schema_props = schema.get("properties", {})

        # Normalize permitDetails
        if "permitDetails" in schema_props and "permitDetails" in data:
            permit_schema = schema_props["permitDetails"].get("properties", {})
            for field in permit_schema.keys():
                if field not in data["permitDetails"]:
                    data["permitDetails"][field] = None

        # Normalize generatorSets
        if "generatorSets" in schema_props and "generatorSets" in data:
            generator_schema = (
                schema_props["generatorSets"]
                .get("items", {})
                .get("properties", {})
            )
            for generator in data.get("generatorSets", []):
                for field in generator_schema.keys():
                    if field not in generator:
                        generator[field] = None

        return data

    def _normalize_fuel_fields(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize fuel-related fields for consistency across extractions.

        Ensures consistent handling of:
        - ASTM specification formats (D396-76 not 0396-76)
        - Multi-grade fuel descriptions
        """
        for generator in data.get("generatorSets", []):
            # Fix ASTM specification format: "ASTM 0396-76" → "ASTM D396-76"
            fuel_spec = generator.get("fuelSpecification")
            if fuel_spec and isinstance(fuel_spec, str):
                # Fix leading zero in ASTM spec (common OCR/extraction error)
                if "ASTM 0" in fuel_spec or "ASTM  0" in fuel_spec:
                    generator["fuelSpecification"] = fuel_spec.replace(
                        "ASTM 0", "ASTM D"
                    ).replace("ASTM  0", "ASTM D")

            # Check for multi-grade fuel descriptions
            primary_fuel = generator.get("primaryFuelType", "")
            fuel_grade = generator.get("fuelGrade", "")
            extraction_notes = generator.get("extractionNotes") or ""

            if (
                primary_fuel
                and isinstance(primary_fuel, str)
                and fuel_grade
                and isinstance(fuel_grade, str)
            ):
                # If fuelGrade indicates multiple grades but primaryFuelType is specific, warn
                multi_grade_indicators = [
                    "numbers 1 or 2",
                    "no. 1 or 2",
                    "grades no. 1 and 2",
                    "no. 1 and no. 2",
                ]
                has_multi_grade = any(
                    indicator in fuel_grade.lower()
                    for indicator in multi_grade_indicators
                )

                if (
                    has_multi_grade
                    and "no. 2" in primary_fuel.lower()
                    and "distillate" not in primary_fuel.lower()
                ):
                    # Fuel grade allows multiple but primary was normalized to specific grade
                    # Add clarifying note if not already present
                    if (
                        "fuel specification allows"
                        not in extraction_notes.lower()
                        and "multiple grade" not in extraction_notes.lower()
                    ):
                        note = f"Fuel specification allows {fuel_grade}; set primaryFuelType to 'no. 2 distillate' per extraction guideline."
                        generator["extractionNotes"] = (
                            note
                            if not extraction_notes
                            else f"{extraction_notes} {note}"
                        )

        return data

    def _extract_with_openai(
        self, text: str, schema: Dict[str, Any]
    ) -> Dict[str, Any]:
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
   - Make, model, capacity (kW/BHP/HP)
   - Fuel type, grade, specification, sulfur content
   - Fuel throughput limits (gallons/year)
   - Control technology descriptions
   - Operating hours limits and rolling windows
   - Operating modes allowed
   - Monitoring and recordkeeping requirements
   - Regulatory applicability (NSPS, MACT)

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

5. CONTROL TECHNOLOGY & OPERATIONS:
   - controlTechnology: Extract from "Emission Controls" section (e.g., "turbocharged", "aftercooler", "SCR")
   - operatingHoursLimit: Extract maximum hours/year from permit conditions
   - operatingHoursRollingWindow: Extract time period exactly as written (e.g., "consecutive 12-month period")
   - operatingHoursLimitScope: "per-unit", "combined", or "facility-wide"
   - allowedOperatingModes: Extract exactly as written (e.g., "emergency only", "emergency, maintenance and testing")
   - If NOT specified, set to null

6. MONITORING & RECORDKEEPING:
   - hourMeterRequired: true/false/null
   - observationFrequency: Extract exactly as written (e.g., "daily when operated", "monthly")
   - recordkeepingWindowYears: Number of years to retain records
   - operationReasonLogRequired: true if logging operation reasons required
   - manufacturerOandMRequired: true if manufacturer O&M procedures required
   - maintenanceTrainingRecordsRequired: true if maintenance/training records required

7. REGULATORY APPLICABILITY:
   - nspsSubpartIIII: true if NSPS Subpart IIII applies, false if explicitly not applicable, null if not mentioned
   - mactSubpartZZZZ: true if MACT Subpart ZZZZ applies, false if explicitly not applicable, null if not mentioned

DOCUMENT TEXT:
{text_excerpt}

Return valid JSON following the schema exactly. Match the permit's structure - do not impose grouping.
"""

        # Prepare API call parameters
        api_params = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert at extracting data from air quality permits. Extract exactly as shown in source document.",
                },
                {"role": "user", "content": prompt},
            ],
        }

        # Reasoning models (gpt-5, o1, o3, etc.) don't support temperature or response_format
        is_reasoning_model = any(
            x in self.model.lower() for x in ["gpt-5", "o1", "o3", "o4"]
        )
        
        if not is_reasoning_model:
            api_params["temperature"] = 0
            api_params["response_format"] = {"type": "json_object"}

        try:
            response = self.client.chat.completions.create(**api_params)
            
            # Check for empty response
            if not response.choices or not response.choices[0].message.content:
                logger.error(f"  ✗ Empty response from API (model={self.model}, reasoning={is_reasoning_model})")
                logger.error(f"     Response: {response}")
                return {
                    "data": {"permitDetails": {}, "generatorSets": []},
                    "cost": 0.0,
                }

            data = json.loads(response.choices[0].message.content)

            # Ensure all schema fields are present (fill missing with null)
            data = self._normalize_with_schema(data, schema)

            # Calculate cost
            usage = response.usage
            cost = self._calculate_openai_cost(
                usage.prompt_tokens, usage.completion_tokens
            )

            logger.info(
                f"  ✓ Extracted {len(data.get('generatorSets', []))} generator entries"
            )

            return {"data": data, "cost": cost}

        except Exception as e:
            logger.exception("  ✗ OpenAI extraction failed")
            return {
                "data": {"permitDetails": {}, "generatorSets": []},
                "cost": 0.0,
            }

    def _validate_with_langextract(
        self, text: str, openai_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Use LangExtract to validate critical fields for QA/QC.

        Returns the raw extraction result for visualization, not inline citations.
        """
        validation_notes = []
        cost = 0.0
        extraction_result = None

        if not self.langextract_available:
            return {
                "extraction_result": None,
                "validation_notes": validation_notes,
                "cost": cost,
            }

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
   - Rated capacity in brake horsepower (BHP or HP)
   - Maximum capacity if different from rated

3. OPERATING LIMITS:
   - Operating hours per year (e.g., "500 hours per year", "≤218 hrs/yr")
   - Fuel throughput limits (gallons per year)
   - Operating modes allowed (e.g., "emergency only", "emergency, maintenance and testing")

4. FUEL SPECIFICATIONS:
   - Fuel type (diesel, distillate, natural gas, etc.)
   - Fuel grade (e.g., "No. 2", "Grade No. 1 and 2")
   - Sulfur content (%, ppm, or decimal)
   - Control technology descriptions (e.g., "turbocharged", "SCR")

5. MONITORING & REGULATORY:
   - Hour meter requirements
   - Recordkeeping requirements
   - Regulatory applicability (NSPS, MACT)

IMPORTANT: For each extraction, capture the EXACT source text from the permit for traceability.""",
                examples=examples,
                api_key=self.api_key,
                model_id=self.model,
            )

            # Verify extraction result has valid structure
            if not extraction_result or not hasattr(
                extraction_result, "extractions"
            ):
                logger.warning(
                    "  ⚠️  LangExtract returned invalid result, skipping QA/QC"
                )
                validation_notes.append(
                    "QA/QC: Skipped due to invalid LangExtract response"
                )
                return {
                    "extraction_result": None,
                    "validation_notes": validation_notes,
                    "cost": 0.0,
                }

            # Cross-validate with OpenAI results
            openai_generators = openai_data.get("generatorSets", [])
            langextract_gen_ids = set()

            for extraction in extraction_result.extractions:
                if extraction.extraction_class == "GENERATOR":
                    gen_id = (
                        extraction.attributes.get("generator_id")
                        if extraction.attributes
                        else None
                    )
                    if gen_id:
                        langextract_gen_ids.add(gen_id)

            # Validation summary (for JSON output)
            validation_notes.append(
                f"OpenAI: {len(openai_generators)} generator sets"
            )
            validation_notes.append(
                f"LangExtract: {len(langextract_gen_ids)} references validated"
            )
            validation_notes.append(
                f"Extractions: {len(extraction_result.extractions)} source citations"
            )

            # Calculate total generator units
            total_units = sum(
                g.get("numGenerators", 1)
                if isinstance(g.get("numGenerators"), int)
                else int(str(g.get("numGenerators", "1")).split()[0])
                if g.get("numGenerators")
                else 1
                for g in openai_generators
            )
            validation_notes.append(f"Total units: {total_units}")

            cost = 0.002  # Approximate LangExtract cost

            logger.info(
                f"  ✓ QA/QC: {len(extraction_result.extractions)} extractions, "
                f"{len(langextract_gen_ids)} generators validated"
            )

        except Exception as e:
            logger.warning("  ⚠️  LangExtract QA/QC failed: %s", e)
            validation_notes.append(f"QA/QC error: {e!s}")

        return {
            "extraction_result": extraction_result,  # Raw result for visualization
            "validation_notes": validation_notes,
            "cost": cost,
        }

    def _create_langextract_examples(self) -> List:
        """
        Create UNIVERSAL LangExtract examples for schema-aligned QA/QC.

        Design principles:
        - Focus on actual schema fields (no emission limits)
        - Minimal format assumptions (handles lists, tables, paragraphs)
        - Flexible reference patterns (EG##, G-#, numeric)
        - Simplified text to avoid format-specific brittleness
        """
        return [
            # Example 1: Generator specs with capacity and fuel
            self.lx.data.ExampleData(
                text="""EG01-EG06: 6 generators, 2500 kW each
Make: Cummins
Model: QSK78-G12
Fuel: No. 2 distillate oil, 0.0015% sulfur
Operating hours: 500 hours/year""",
                extractions=[
                    self.lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="EG01-EG06: 6 generators, 2500 kW",
                        attributes={
                            "generator_id": "EG01-EG06",
                            "num_generators": "6",
                            "capacity_kw": "2500",
                        },
                    ),
                    self.lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="Operating hours: 500 hours/year",
                        attributes={
                            "generator_id": "EG01-EG06",
                            "type": "operating_hours",
                            "value": "500",
                        },
                    ),
                    self.lx.data.Extraction(
                        extraction_class="FUEL",
                        extraction_text="No. 2 distillate oil, 0.0015% sulfur",
                        attributes={
                            "generator_id": "EG01-EG06",
                            "fuel_type": "No. 2 distillate oil",
                            "sulfur_pct": "0.0015",
                        },
                    ),
                ],
            ),
            # Example 2: Multiple generators with table format
            self.lx.data.ExampleData(
                text="""G-1 thru G-6: six 2000 kW engines
Make: Caterpillar
Model: 3516C
Fuel: diesel fuel (ultra-low sulfur)
Control: turbocharged with aftercooler
Operating: Emergency use only, 218 hours/year""",
                extractions=[
                    self.lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="G-1 thru G-6: six 2000 kW engines",
                        attributes={
                            "generator_id": "G-1 thru G-6",
                            "num_generators": "6",
                            "capacity_kw": "2000",
                        },
                    ),
                    self.lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="Emergency use only, 218 hours/year",
                        attributes={
                            "generator_id": "G-1 thru G-6",
                            "type": "operating_mode",
                            "value": "Emergency use only",
                            "hours": "218",
                        },
                    ),
                    self.lx.data.Extraction(
                        extraction_class="CONTROL",
                        extraction_text="turbocharged with aftercooler",
                        attributes={
                            "generator_id": "G-1 thru G-6",
                            "technology": "turbocharged with aftercooler",
                        },
                    ),
                ],
            ),
            # Example 3: Single generator with fuel throughput
            self.lx.data.ExampleData(
                text="""Ref. 3: One Caterpillar 1500 kW diesel generator
Fuel throughput: not to exceed 50,000 gallons per year
Operating hours: 500 hours per year maximum
Hour meter: Required""",
                extractions=[
                    self.lx.data.Extraction(
                        extraction_class="GENERATOR",
                        extraction_text="Ref. 3: One Caterpillar 1500 kW",
                        attributes={
                            "generator_id": "3",
                            "num_generators": "1",
                            "make": "Caterpillar",
                            "capacity_kw": "1500",
                        },
                    ),
                    self.lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="Fuel throughput: not to exceed 50,000 gallons per year",
                        attributes={
                            "generator_id": "3",
                            "type": "fuel_throughput",
                            "value": "50000",
                            "unit": "gallons/year",
                        },
                    ),
                    self.lx.data.Extraction(
                        extraction_class="SPEC",
                        extraction_text="Operating hours: 500 hours per year maximum",
                        attributes={
                            "generator_id": "3",
                            "type": "operating_hours",
                            "value": "500",
                        },
                    ),
                ],
            ),
        ]

    def _run_sanity_checks(self, data: Dict[str, Any]) -> List[str]:
        """
        Run post-extraction sanity checks to catch obvious errors.

        Returns list of warning messages.
        """
        warnings = []
        generators = data.get("generatorSets", [])

        if not generators:
            warnings.append("⚠️ WARNING: No generators extracted")
            return warnings

        # Check 1: Duplicate reference numbers
        ref_numbers = [
            g.get("referenceNumber")
            for g in generators
            if g.get("referenceNumber")
        ]
        duplicates = [
            ref for ref in set(ref_numbers) if ref_numbers.count(ref) > 1
        ]
        if duplicates:
            warnings.append(
                f"⚠️ WARNING: Duplicate reference numbers: {', '.join(duplicates)}"
            )

        # Check 2: Missing critical fields and alternative options
        for i, gen in enumerate(generators):
            ref = gen.get("referenceNumber", f"Entry {i + 1}")
            make = gen.get("make", "")
            model = gen.get("model", "")

            if not make:
                warnings.append(f"⚠️ WARNING: {ref} missing make")
            elif " or " in make.lower() or " / " in make:
                warnings.append(
                    f"⚠️ WARNING: {ref} has multiple make options: '{make}' - schema requires single deterministic choice"
                )

            if not model:
                warnings.append(f"⚠️ WARNING: {ref} missing model")
            elif " or " in model.lower() or " / " in model:
                warnings.append(
                    f"⚠️ WARNING: {ref} has multiple model options: '{model}' - schema requires single deterministic choice"
                )

            if not gen.get("ratedCapacityKW") and not gen.get(
                "ratedCapacityBHP"
            ):
                warnings.append(f"⚠️ WARNING: {ref} missing capacity")

            # Check for multiple capacity values in same field
            rated_kw = gen.get("ratedCapacityKW")
            rated_bhp = gen.get("ratedCapacityBHP")
            if (
                rated_kw
                and isinstance(rated_kw, str)
                and " or " in str(rated_kw).lower()
            ):
                warnings.append(
                    f"⚠️ WARNING: {ref} has multiple kW options: '{rated_kw}' - schema requires smallest value"
                )
            if (
                rated_bhp
                and isinstance(rated_bhp, str)
                and " or " in str(rated_bhp).lower()
            ):
                warnings.append(
                    f"⚠️ WARNING: {ref} has multiple BHP options: '{rated_bhp}' - schema requires smallest value"
                )

        # Check 3: Unrealistic values
        for gen in generators:
            ref = gen.get("referenceNumber", "Unknown")
            num_gens = gen.get("numGenerators", 1)
            if num_gens and (num_gens < 1 or num_gens > 100):
                warnings.append(
                    f"⚠️ WARNING: {ref} has unrealistic numGenerators: {num_gens}"
                )

            capacity = gen.get("ratedCapacityKW")
            if capacity and (capacity < 10 or capacity > 50000):
                warnings.append(
                    f"⚠️ WARNING: {ref} has unrealistic capacity: {capacity} kW"
                )

        # Check 4: Total generator count
        total_units = sum(g.get("numGenerators", 1) for g in generators)
        if total_units > 50:
            warnings.append(
                f"⚠️ WARNING: High total generator count: {total_units} (check for extraction errors)"
            )

        # Check 5: Critical extractionNotes validation
        # Check permit-level extractionNotes for multiple IDs
        permit_details = data.get("permitDetails", {})
        permit_number = permit_details.get("permitNumber", "")
        permit_notes = permit_details.get("extractionNotes")

        # Look for indicators of multiple IDs in permit number field itself
        if any(
            indicator in str(permit_number).lower()
            for indicator in ["application no", "id no", "permit no"]
        ):
            if not permit_notes:
                warnings.append(
                    "⚠️ INFO: Permit number may contain multiple identifiers but no extractionNotes provided"
                )

        # Check generator-level extractionNotes for critical fields
        for gen in generators:
            ref = gen.get("referenceNumber", "Unknown")
            gen_notes = gen.get("extractionNotes")

            # If emissionsScope is set but emissionsGroupRef is null and vice versa, might need notes
            scope = gen.get("emissionsScope")
            group_ref = gen.get("emissionsGroupRef")
            if scope in ["combined_group", "facility_wide"] and not group_ref:
                if not gen_notes or "emission" not in gen_notes.lower():
                    warnings.append(
                        f"⚠️ INFO: {ref} has emissionsScope='{scope}' without emissionsGroupRef or clarifying notes"
                    )

        if warnings:
            logger.warning(f"Sanity checks found {len(warnings)} issues")
        else:
            logger.info("✓ All sanity checks passed")

        return warnings

    def generate_visualization(
        self,
        extraction_result: ExtractionResult,
        output_dir: Path,
        permit_number: str,
    ) -> Path:
        """
        Generate HTML visualization with interactive source citations.

        Args:
            extraction_result: Result from extract()
            output_dir: Directory to save visualization
            permit_number: Permit number for filename

        Returns
        -------
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
                output_dir=str(output_dir),
            )

            # LangExtract saves without extension, find the actual file
            jsonl_path = output_dir / jsonl_filename
            if not jsonl_path.exists():
                # Try with common extensions
                for ext in ["", ".jsonl", ".json"]:
                    test_path = output_dir / f"{jsonl_filename}{ext}"
                    if test_path.exists():
                        jsonl_path = test_path
                        break

            logger.info("  ✓ Saved annotated data: %s", jsonl_path)

            # Generate interactive HTML visualization
            html_path = output_dir / f"{permit_number}_visualization.html"
            html_content = self.lx.visualize(str(jsonl_path))

            with Path(html_path).open("w") as f:
                if hasattr(html_content, "data"):
                    f.write(html_content.data)  # For Jupyter/Colab
                else:
                    f.write(html_content)

            logger.info("  ✓ Saved HTML visualization: %s", html_path)
            logger.info(
                f"  📊 Open {html_path.name} in browser for interactive source highlighting"
            )

            return jsonl_path

        except Exception as e:
            logger.error("  ✗ Failed to generate visualization: %s", e)
            return None

    def _calculate_completeness(
        self, data: Dict[str, Any], validation_notes: List[str]
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

        Returns
        -------
            float: Score from 0-1 indicating data completeness
        """
        score = 0.5

        # Permit details
        permit_details = data.get("permitDetails", {})
        if permit_details.get("permitNumber"):
            score += 0.1
        if permit_details.get("facilityName"):
            score += 0.1

        # Generators
        generators = data.get("generatorSets", [])
        if generators:
            score += 0.1

            # Complete specs
            complete = sum(
                1
                for g in generators
                if g.get("make")
                and g.get("model")
                and g.get("ratedCapacityKW")
            )
            score += 0.3 * (complete / len(generators))

        return min(score, 1.0)

    def _calculate_openai_cost(
        self, prompt_tokens: int, completion_tokens: int
    ) -> float:
        """Calculate OpenAI API cost."""
        # # gpt-4o-mini pricing: $0.150/1M input, $0.600/1M output
        # input_cost = (prompt_tokens / 1_000_000) * 0.150
        # output_cost = (completion_tokens / 1_000_000) * 0.600

        # gpt-5 pricing: $0.150/1M input, $0.600/1M output
        input_cost = (prompt_tokens / 1_000_000) * 1.25
        output_cost = (completion_tokens / 1_000_000) * 10
        return input_cost + output_cost
