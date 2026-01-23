"""OpenAI API client for structured extraction."""

import json
import logging
from typing import Dict, Any

import openai

logger = logging.getLogger(__name__)


class OpenAIClient:
    """
    Simple wrapper for OpenAI API calls.
    
    Handles both regular OpenAI and Azure OpenAI endpoints.
    """
    
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        use_azure: bool = False,
        azure_endpoint: str = None,
        azure_api_version: str = None,
    ):
        """
        Initialize OpenAI client.
        
        Args:
            api_key: OpenAI or Azure API key
            model: Model to use (default: gpt-4o-mini)
            use_azure: Use Azure OpenAI instead of OpenAI
            azure_endpoint: Azure OpenAI endpoint URL (required if use_azure=True)
            azure_api_version: Azure API version (required if use_azure=True)
        """
        self.api_key = api_key
        self.model = model
        self.use_azure = use_azure
        
        # Initialize OpenAI client (Azure or regular)
        if use_azure:
            from openai import AzureOpenAI
            self._client = AzureOpenAI(
                api_key=api_key,
                api_version=azure_api_version,
                azure_endpoint=azure_endpoint
            )
        else:
            self._client = openai.OpenAI(api_key=api_key)
    
    def extract(
        self,
        text: str,
        schema: Dict[str, Any],
        system_prompt: str = None,
        user_prompt: str = None,
    ) -> Dict[str, Any]:
        """
        Extract structured data using OpenAI.
        
        Args:
            text: Document text to extract from
            schema: JSON schema for extraction
            system_prompt: Optional custom system prompt
            user_prompt: Optional custom user prompt (overrides default)
            
        Returns:
            dict with 'data' (extracted data) and 'cost' (API cost)
        """
        # Use default prompts if not provided
        if system_prompt is None:
            system_prompt = (
                "You are an expert at extracting structured data from documents. "
                "Extract exactly as shown in source document."
            )
        
        if user_prompt is None:
            user_prompt = self._build_extraction_prompt(text, schema)
        
        # Prepare API call parameters
        api_params = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
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
            response = self._client.chat.completions.create(**api_params)
            
            # Check for empty response
            if not response.choices or not response.choices[0].message.content:
                logger.error(
                    f"  ✗ Empty response from API "
                    f"(model={self.model}, reasoning={is_reasoning_model})"
                )
                logger.error(f"     Response: {response}")
                return {"data": {}, "cost": 0.0}
            
            # Parse JSON response
            data = json.loads(response.choices[0].message.content)
            
            # Calculate cost
            usage = response.usage
            cost = self._calculate_cost(usage.prompt_tokens, usage.completion_tokens)
            
            # Count total items extracted
            total_items = sum(
                len(value) for value in data.values() 
                if isinstance(value, list)
            )
            logger.info(f"  ✓ Extracted {total_items} item(s) from document")
            
            return {"data": data, "cost": cost}
        
        except Exception as e:
            logger.exception("  ✗ OpenAI extraction failed")
            return {"data": {}, "cost": 0.0}
    
    def _build_extraction_prompt(
        self, text: str, schema: Dict[str, Any]
    ) -> str:
        """Build the extraction prompt."""
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

1. **Read schema carefully** - The schema defines what to extract and what to exclude
2. **Scan document structure** - Identify relevant sections based on schema scope
3. **Extract verbatim** - Copy exact language from document, preserve legal/technical precision
4. **Verify against schema** - Ensure each field follows schema requirements and examples
5. **Document source** - Include section references for traceability

**SCHEMA-DRIVEN FILTERING:**

The schema is your single source of truth for what to extract. Follow its guidance exactly:

- **Read field descriptions** - Each field description defines what content belongs in that field
- **Respect scope definitions** - If schema specifies to include/exclude certain topics, follow exactly
- **Check examples** - Schema examples show the expected extraction patterns
- **Verify applicability** - Before extracting, confirm the content matches the schema's defined scope
- **When uncertain** - Re-read the schema's description for that field to determine if content should be included

The schema defines the boundaries - extract everything within those boundaries, nothing outside them.

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

{text}

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
        return prompt
    
    def _calculate_cost(
        self, prompt_tokens: int, completion_tokens: int
    ) -> float:
        """
        Calculate API cost based on token usage.
        
        Args:
            prompt_tokens: Number of prompt tokens
            completion_tokens: Number of completion tokens
            
        Returns:
            Cost in USD
        """
        # Cost per 1M tokens (as of Jan 2024)
        # gpt-4o-mini: $0.150/1M input, $0.600/1M output
        # gpt-4o: $2.50/1M input, $10.00/1M output
        # o1: $15/1M input, $60/1M output
        
        if "gpt-4o-mini" in self.model.lower():
            input_cost_per_1m = 0.150
            output_cost_per_1m = 0.600
        elif "gpt-4o" in self.model.lower():
            input_cost_per_1m = 2.50
            output_cost_per_1m = 10.00
        elif "o1" in self.model.lower() or "o3" in self.model.lower():
            input_cost_per_1m = 15.0
            output_cost_per_1m = 60.0
        else:
            # Default to gpt-4o-mini pricing
            input_cost_per_1m = 0.150
            output_cost_per_1m = 0.600
        
        input_cost = (prompt_tokens / 1_000_000) * input_cost_per_1m
        output_cost = (completion_tokens / 1_000_000) * output_cost_per_1m
        
        return input_cost + output_cost
