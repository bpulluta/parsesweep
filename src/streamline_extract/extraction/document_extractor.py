"""
Universal document extractor for structured data extraction.

Architecture:
1. OpenAI Structured - Fast, accurate extraction with schema-driven parsing
2. LangExtract QA/QC - Validates results and adds source citations for traceability

Supports any document type with a defined JSON schema - fully domain-agnostic.

Cost: ~$0.002-0.004 per document (varies by length and model)
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
    """Result from document extraction."""

    data: Dict[str, Any]
    completeness_score: float  # 0-1 score based on fields populated
    cost: float
    processing_time: float
    validation_notes: List[str]
    langextract_result: Any = None
    validation_report: Optional[ValidationReport] = None


class DocumentExtractor:
    """
    Universal document extractor with traceability and configurable context.

    Two-stage approach:
    1. OpenAI Structured: Fast extraction with schema-driven parsing
    2. LangExtract QA/QC: Validates critical fields and adds citations
    
    Works with any document type and JSON schema - fully domain-agnostic.
    """

    def __init__(
        self,
        api_key: str = None,
        model: str = "gpt-4o-mini",
        enable_qa_qc_overrides: bool = True,
        max_context_chars: int = 400000,
        use_azure: bool = False,
        azure_endpoint: str = None,
        azure_api_version: str = None,
    ):
        """
        Initialize document extractor.
        
        Args:
            api_key: OpenAI or Azure API key
            model: Model to use (default: gpt-4o-mini)
            enable_qa_qc_overrides: Enable QA/QC validation overrides
            max_context_chars: Maximum characters to extract from document (default: 400000).
                             Proven reliable for fast processing. Increase with --max-context
                             for very long documents if willing to wait longer.
            use_azure: Use Azure OpenAI instead of OpenAI
            azure_endpoint: Azure OpenAI endpoint URL (required if use_azure=True)
            azure_api_version: Azure API version (required if use_azure=True)
        """
        self.api_key = api_key
        self.model = model
        self.max_context_chars = max_context_chars
        self.use_azure = use_azure
        
        # Initialize OpenAI client (Azure or regular)
        if use_azure:
            from openai import AzureOpenAI
            self.client = AzureOpenAI(
                api_key=api_key,
                api_version=azure_api_version,
                azure_endpoint=azure_endpoint
            )
        else:
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
        Extract structured data from document with optional QA/QC validation.

        Args:
            text: Full document text
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

            # Get entity identifier for validation report (works with any schema)
            entity_identifier = "unknown"
            for key, value in openai_result["data"].items():
                if isinstance(value, dict):
                    # Try common identifier fields
                    for id_field in ["permitNumber", "id", "identifier", "name", "jurisdiction"]:
                        if id_field in value and value[id_field]:
                            entity_identifier = str(value[id_field])
                            break
                    if entity_identifier != "unknown":
                        break

            # Run cross-validation and get detailed report
            if qa_result.get("extraction_result"):
                validated_data, validation_report = (
                    self.qa_qc_validator.validate(
                        openai_data=openai_result["data"],
                        langextract_result=qa_result["extraction_result"],
                        entity_identifier=entity_identifier,
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
        validated_data = self._normalize_string_fields(validated_data)
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
        Works generically with any schema structure.
        """
        # Get schema properties
        schema_props = schema.get("properties", {})

        # Normalize all top-level object fields
        for prop_name, prop_schema in schema_props.items():
            if prop_schema.get("type") == "object" and prop_name in data:
                # This is an object field (e.g., metadata, context, details)
                obj_schema = prop_schema.get("properties", {})
                for field in obj_schema.keys():
                    if field not in data[prop_name]:
                        data[prop_name][field] = None
            
            elif prop_schema.get("type") == "array" and prop_name in data:
                # This is an array field (e.g., requirements, items, rates)
                item_schema = prop_schema.get("items", {}).get("properties", {})
                for item in data.get(prop_name, []):
                    if isinstance(item, dict):
                        for field in item_schema.keys():
                            if field not in item:
                                item[field] = None

        return data

    def _normalize_string_fields(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize string fields for consistency.
        
        Cleans whitespace, removes extra spaces, and standardizes formatting.
        Works generically with any schema structure.
        """
        def clean_string(value):
            """Clean individual string value."""
            if not isinstance(value, str):
                return value
            # Remove extra whitespace
            value = " ".join(value.split())
            return value.strip()
        
        def clean_dict(obj):
            """Recursively clean all strings in a dict."""
            if isinstance(obj, dict):
                return {k: clean_dict(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [clean_dict(item) for item in obj]
            elif isinstance(obj, str):
                return clean_string(obj)
            return obj
        
        return clean_dict(data)

    def _extract_with_openai(
        self, text: str, schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Extract using OpenAI - extract data exactly as shown in source document.
        """
        # Use configurable context window to capture full document content
        # Default 200k chars supports most documents (60-100 pages)
        # For longer documents (e.g., 100+ pages with critical info on page 80+),
        # increase max_context_chars during initialization
        text_excerpt = text[:self.max_context_chars]
        
        if len(text) > self.max_context_chars:
            logger.warning(
                f"Document truncated: {len(text):,} chars -> {self.max_context_chars:,} chars. "
                f"Consider increasing max_context_chars if critical info is at end of document."
            )

        prompt = f"""Extract ALL data from this document into valid JSON matching the schema below.

══════════════════════════════════════════════════════════════════════════════
CORE PRINCIPLES
══════════════════════════════════════════════════════════════════════════════

1. EXTRACT EXACTLY AS WRITTEN - Do not normalize, convert units, paraphrase, or infer missing data
2. PRESERVE DOCUMENT STRUCTURE - Extract requirements as organized in the document
3. SEARCH ENTIRE DOCUMENT - Information may appear in tables, narrative sections, and appendices
4. VERIFY COMPLETENESS - Review all sections before returning to ensure nothing was missed
5. FOLLOW SCHEMA GUIDANCE - Pay careful attention to schema descriptions and exclusion rules

**EXTRACTION STRATEGY:**

1. **Read schema carefully** - Note what should be INCLUDED vs EXCLUDED (e.g., schema may specify to extract only certain types of content and exclude others)
2. **Scan document structure** - Identify relevant sections based on schema scope
3. **Extract verbatim** - Copy exact language from document, preserve legal/technical precision
4. **Verify against schema** - Ensure each field follows schema requirements and examples
5. **Document source** - Include section references for traceability

**CRITICAL FILTERING:**

- If schema specifies to EXCLUDE certain content (e.g., "EXCLUDE: solar power, wind power"), verify section headers and content BEFORE extracting
- Do NOT extract from sections that don't match the schema scope (e.g., if schema is for geothermal, skip solar sections entirely)
- SPECIAL CASE - Zoning Codes: If document lists geothermal as permitted/conditional use in certain zones:
  * EXTRACT: The geothermal use classification AND zone requirements from zones where geothermal is allowed
  * EXCLUDE: Requirements for other land uses (guest houses, residential, accessory buildings, manufactured homes) even if on same page
  * Make zone context explicit in "applies_to" field (e.g., "Geothermal power plants in I-A zone")
- When in doubt about applicability, check if the requirement directly regulates the target use type OR applies to zones where the target use is allowed

**FOLLOW THE SCHEMA'S STRUCTURE AND GUIDANCE:**

• Extract each field exactly as described in the schema
• Pay attention to schema's field descriptions - they contain specific extraction rules
• Review schema examples to understand expected output patterns
• Respect extraction scope defined in schema (e.g., EXCLUDE directives)
• Preserve verbatim language from document - do NOT paraphrase
• Use null for missing data (do NOT infer, calculate, or guess)

**FIELD EXTRACTION:**

Each field in the schema has a description that explains:
• What data to extract into that field
• When to use the field vs. when to use null
• Examples showing the expected pattern

READ EACH FIELD'S DESCRIPTION CAREFULLY. The schema descriptions contain the specific rules for that document type (e.g., what constitutes a "value", how to structure "details", when to extract vs. skip).

══════════════════════════════════════════════════════════════════════════════
JSON SCHEMA
══════════════════════════════════════════════════════════════════════════════

{json.dumps(schema, indent=2)}

══════════════════════════════════════════════════════════════════════════════
DOCUMENT TEXT TO EXTRACT FROM
══════════════════════════════════════════════════════════════════════════════

{text_excerpt}

══════════════════════════════════════════════════════════════════════════════
FINAL INSTRUCTIONS
══════════════════════════════════════════════════════════════════════════════

BEFORE RETURNING JSON, VERIFY:

1. SCHEMA COMPLIANCE:
   □ All required fields present per schema
   □ Enum values match schema options
   □ Field types correct (string, number, boolean, null)
   □ Extraction scope rules followed (e.g., EXCLUDE directives obeyed)

2. COMPLETENESS:
   □ Scanned entire document (not just first few pages)
   □ Checked all relevant sections based on schema scope
   □ No requirements/items skipped
   □ Section references included for traceability

3. DATA QUALITY:
   □ Extracted text verbatim (not paraphrased)
   □ Values match source document exactly
   □ Units preserved as written
   □ Missing data = null (not guessed/inferred)

4. FILTERING ACCURACY (if applicable):
   □ Verified each requirement matches schema scope
   □ Excluded content from non-applicable sections
   □ Checked section headings/titles before extracting
   □ Did not mix different technologies/topics

Return ONLY valid JSON matching the schema.
"""

        # Prepare API call parameters
        api_params = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert at extracting structured data from documents. Extract exactly as shown in source document.",
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
                    "data": {},
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

            # Count total items extracted across all arrays
            total_items = sum(
                len(value) for value in data.values() 
                if isinstance(value, list)
            )
            logger.info(
                f"  ✓ Extracted {total_items} item(s) from document"
            )

            return {"data": data, "cost": cost}

        except Exception as e:
            logger.exception("  ✗ OpenAI extraction failed")
            return {
                "data": {},
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
                prompt_description="""Extract ALL key data elements for comprehensive QA/QC validation.

CRITICAL: Extract EVERY instance of important information including:

1. ITEM IDENTIFICATION:
   - Reference numbers, IDs, or identifiers
   - Quantities or counts
   - Types, categories, or classifications

2. SPECIFICATIONS:
   - Capacity, size, or magnitude values
   - Technical specifications
   - Performance characteristics

3. REQUIREMENTS & LIMITS:
   - Operational limits or thresholds
   - Time-based constraints
   - Quantitative requirements

4. DESCRIPTIVE DETAILS:
   - Type or category information
   - Model, make, or variant details
   - Qualitative specifications

5. REGULATORY & COMPLIANCE:
   - Compliance requirements
   - Monitoring requirements
   - Recordkeeping obligations

IMPORTANT: For each extraction, capture the EXACT source text from the document for traceability.""",
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
            total_openai_items = sum(
                len(value) for value in openai_data.values()
                if isinstance(value, list)
            )
            
            langextract_item_ids = set()
            for extraction in extraction_result.extractions:
                if extraction.attributes:
                    # Look for any item identifier field
                    for id_field in ["item_id", "generator_id", "id", "feature", "requirement_id"]:
                        item_id = extraction.attributes.get(id_field)
                        if item_id:
                            langextract_item_ids.add(item_id)
                            break

            # Validation summary (for JSON output)
            validation_notes.append(
                f"OpenAI: {total_openai_items} items extracted"
            )
            validation_notes.append(
                f"LangExtract: {len(langextract_item_ids)} items validated"
            )
            validation_notes.append(
                f"Source citations: {len(extraction_result.extractions)} extractions"
            )

            cost = 0.002  # Approximate LangExtract cost

            logger.info(
                f"  ✓ QA/QC: {len(extraction_result.extractions)} source citations, "
                f"{len(langextract_item_ids)} items validated"
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
        - Completely generic - works for permits, ordinances, tariffs, regulations, etc.
        - Minimal assumptions - extracts reference numbers, values, and descriptions
        - Flexible structure - handles any document format
        
        Note: These are intentionally abstract. Schema-specific examples should
        be added based on actual document type being processed.
        """
        return [
            # Example 1: Reference with numeric constraint
            self.lx.data.ExampleData(
                text="""Section 3.2(a): Maximum distance of 100 feet
Reference ID: REQ-001
Category: Spatial constraint""",
                extractions=[
                    self.lx.data.Extraction(
                        extraction_class="REQUIREMENT",
                        extraction_text="Section 3.2(a): Maximum distance of 100 feet",
                        attributes={
                            "reference": "3.2(a)",
                            "constraint_type": "maximum",
                            "value": "100",
                            "unit": "feet",
                        },
                    ),
                ],
            ),
            # Example 2: Multiple items in a list
            self.lx.data.ExampleData(
                text="""Items A through D (four total)
Classification: Type II
Status: Active""",
                extractions=[
                    self.lx.data.Extraction(
                        extraction_class="ITEM",
                        extraction_text="Items A through D (four total)",
                        attributes={
                            "identifier": "A through D",
                            "count": "4",
                        },
                    ),
                    self.lx.data.Extraction(
                        extraction_class="DETAIL",
                        extraction_text="Classification: Type II",
                        attributes={
                            "field": "classification",
                            "value": "Type II",
                        },
                    ),
                ],
            ),
        ]

    def _run_sanity_checks(self, data: Dict[str, Any]) -> List[str]:
        """
        Run post-extraction sanity checks to catch obvious errors.
        
        Generic checks that work for any schema type.
        Returns list of warning messages.
        """
        warnings = []
        
        # Find main array fields dynamically
        main_arrays = []
        for key, value in data.items():
            if isinstance(value, list) and value:
                main_arrays.append((key, value))
        
        # Check 1: At least some items extracted
        total_items = sum(len(items) for _, items in main_arrays)
        if total_items == 0:
            warnings.append(
                "⚠️ WARNING: No items extracted - check if document contains expected data"
            )
        
        # Check 2: Look for "or" in string fields (indicates multiple options not resolved)
        for array_name, items in main_arrays:
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                    
                item_id = item.get('referenceNumber', item.get('id', item.get('feature', f'item_{idx}')))
                
                for field, value in item.items():
                    if isinstance(value, str):
                        if " or " in value.lower() or " / " in value:
                            warnings.append(
                                f"⚠️ WARNING: {array_name}[{idx}].{field} contains multiple options: '{value}'"
                            )
        
        # Don't log warnings - they're stored in validation_notes for later review if needed
        return warnings

    def generate_visualization(
        self,
        extraction_result: ExtractionResult,
        output_dir: Path,
        entity_id: str,
    ) -> Path:
        """
        Generate HTML visualization with interactive source citations.

        Args:
            extraction_result: Result from extract()
            output_dir: Directory to save visualization
            entity_id: Entity identifier for filename (e.g., permit number, ordinance ID, tariff name)

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
            jsonl_filename = f"{entity_id}_annotated"

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
            html_path = output_dir / f"{entity_id}_visualization.html"
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
        
        Generic scoring that works for any schema:
        - 0.3: Base score for successful extraction
        - 0.2: Has context fields (entity identifiers, location, etc.)
        - 0.5: Has item arrays with populated fields

        Returns
        -------
            float: Score from 0-1 indicating data completeness
        """
        score = 0.3  # Base score for successful extraction
        
        # Count non-empty fields in all top-level objects
        context_fields = 0
        total_context_fields = 0
        
        for key, value in data.items():
            if isinstance(value, dict):
                # This is a context object (metadata, jurisdiction, facility, etc.)
                for field_key, field_value in value.items():
                    total_context_fields += 1
                    if field_value not in [None, "", [], {}]:
                        context_fields += 1
        
        # Score context completeness
        if total_context_fields > 0:
            score += 0.2 * (context_fields / total_context_fields)
        
        # Count items in arrays and their completeness
        total_items = 0
        complete_items = 0
        
        for key, value in data.items():
            if isinstance(value, list) and value:
                total_items += len(value)
                # Count items with at least 50% of fields populated
                for item in value:
                    if isinstance(item, dict):
                        item_fields = len(item)
                        populated_fields = sum(
                            1 for v in item.values() 
                            if v not in [None, "", [], {}]
                        )
                        if item_fields > 0 and populated_fields / item_fields >= 0.5:
                            complete_items += 1
        
        # Score item completeness
        if total_items > 0:
            score += 0.5 * (complete_items / total_items)
        
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
