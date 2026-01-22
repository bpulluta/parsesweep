"""Module to extract relevant permit text"""

import json
import logging
from functools import cached_property

from elm.ords.llm.calling import LLMCaller

from streamline_extract import PACKAGE_ROOT


logger = logging.getLogger(__name__)


PERMIT_TEXT_EXTRACTION_SYSTEM_MESSAGE = """Verbatim excerpt extractor for documents

Role
- You are a verbatim text extractor. Given a JSON extraction schema and the full text of a document, return only the exact text excerpts from the document that are relevant to the schema. Do not perform any data extraction or interpretation.

Core rules
- Output only text copied verbatim from the input document. No paraphrasing, rewriting, summarizing, or explanation.
- Preserve original wording, spelling, capitalization, punctuation, spacing, line breaks, and structure (including tables as monospaced text).
- Do not add to or modify anything: no labels, keys, headings, quotes, brackets, ellipses, highlights, annotations, comments, or metadata. Do not translate.
- Do not output JSON or field/value pairs. Do not infer or normalize units, numbers, or dates. Do not fix typos.
- If multiple excerpts are returned, place them in the same order they appear in the document. Use a single blank line between excerpts. Do not add any other separators.

Relevance and completeness
- Include every passage that could supply information for any field in the provided JSON schema.
- Capture all relevant passages, tables, specifications, requirements, etc. that match the schema fields. Include specifications, limits, conditions, definitions, exemptions, requirements, cross-references, tables, attachments, and section numbers/headers needed for interpretation. Do not omit relevant content for appearing redundant; if identical text is duplicated verbatim, include one instance unless different context adds meaning.
- Capture the minimal span that preserves full meaning and usability for extraction, including necessary context such as:
  - Units, thresholds, ranges, limits, qualifiers, conditions, exceptions, and footnotes linked to the value.
  - Condition numbers, section headers, table headers/row labels that are required to interpret values.
  - Cross-references or incorporations by reference when they define or constrain a requirement.
- Avoid irrelevant text. Do not include marketing material, navigation, boilerplate not tied to schema fields, or general background not needed for extraction.
- If the same relevant wording appears multiple times, include one instance unless different contexts change its meaning.
- Ensure 100% coverage: scan the entire document so no relevant passage is missed.

Handling special content
- Tables: copy exactly as rendered in text, including spacing and headers needed to interpret entries.
- Figures/attachments: include only text present in the permit (e.g., captions or referenced text); do not invent or OCR unavailable content.
- Page markers, footers, or watermarks: include only if they are part of a passage needed to interpret relevant content; otherwise omit.

Output format
- Output consists solely of one or more verbatim excerpts from the permit, in document order, separated by a single blank line.
- Do not include any preface or postface. If no relevant content exists, return an empty output.
"""
PERMIT_TEXT_EXTRACTION_PROMPT = """You are given two inputs:

# JSON extraction schema #

{json_schema}


# Document full text #

{full_permit_text}

Task: Return only verbatim excerpts from the document that are relevant for an extraction task using the JSON schema. Do not perform extraction or interpretation. Preserve original wording and formatting. Use minimal spans that retain full meaning (including units, limits, qualifiers, section/condition numbers, table headers/labels, and any linked footnotes or references). Order excerpts as they appear in the document, separated by a single blank line. Do not add any text, labels, or JSON. If nothing is relevant, return an empty output.
"""


class PermitTextExtractor:
    """Base implementation for a text extractor"""

    def __init__(self, llm_service, usage_tracker=None, **kwargs):
        """

        Parameters
        ----------
        llm_service : elm.ords.services.base.Service
            LLM service used for queries.
        usage_tracker : elm.ords.services.usage.UsageTracker, optional
            Optional tracker instance to monitor token usage during
            LLM calls. By default, ``None``.
        **kwargs
            Keyword arguments to be passed to the underlying service
            processing function (i.e. `llm_service.call(**kwargs)`).
            Should *not* contain the following keys:

                - usage_tracker
                - usage_sub_label
                - messages

            These arguments are provided by this caller object.
        """
        self.llm_caller = LLMCaller(
            llm_service, usage_tracker=usage_tracker, **kwargs
        )

    @cached_property
    def schema(self):
        """Load the extraction schema"""
        schema_path = (
            PACKAGE_ROOT.parent.parent
            / "schemas"
            / "air_quality_permits_schema.json"
        )
        logger.debug("Loading extraction schema from %s", schema_path)

        with schema_path.open("r", encoding="utf-8") as f:
            schema = json.load(f)

        return json.dumps(schema, indent=2)

    async def extract(self, full_permit_text):
        """Perform extraction processing"""
        logger.debug("Extracting permit text...")
        return await self.llm_caller.call(
            sys_msg=PERMIT_TEXT_EXTRACTION_SYSTEM_MESSAGE,
            content=PERMIT_TEXT_EXTRACTION_PROMPT.format(
                json_schema=self.schema,
                full_permit_text=full_permit_text,
            ),
            usage_sub_label="permit_text_extraction",
        )
