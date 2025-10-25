# QA/QC Strategy for Permit Extraction

## Executive Summary

This document outlines a comprehensive Quality Assurance and Quality Control (QA/QC) strategy for air quality permit extraction using LangExtract as a validation layer on top of OpenAI's structured extraction.

**Purpose**: Ensure policy-grade accuracy through traceability, cross-validation, and confidence-based error detection.

## Current Architecture Analysis

### Stage 1: OpenAI Structured Extraction (Primary)
- **Strengths**: Fast, cost-effective ($0.002-0.004/permit), good at understanding schema structure
- **Weaknesses**: No source traceability, can hallucinate, may miss nuanced permit language
- **Current Role**: Primary extraction engine

### Stage 2: LangExtract QA/QC (Validation)
- **Strengths**: Provides source citations, high precision on targeted extraction, traceability for audits
- **Weaknesses**: More expensive, slower, requires careful prompt engineering
- **Current Role**: Limited validation (only generator counts and basic checks)

## Critical Issues with Current Implementation

1. **Insufficient Validation**: Only validates generator counts, not actual field values
2. **No Cross-Validation**: LangExtract results are stored but not used to correct OpenAI errors
3. **Missing Confidence Scoring**: No mechanism to flag low-confidence extractions
4. **Limited Traceability**: Source citations exist but aren't linked to extracted fields
5. **Schema Mismatch**: LangExtract examples don't align with updated schema fields

## Proposed QA/QC Strategy

### Level 1: Critical Fields (High-Confidence Override)
**Fields that MUST be correct for policy decisions:**
- Emission limits (NOx, CO, VOC, PM, PM10, PM2.5, SO2)
- Operating hours limits
- Fuel throughput limits
- Fuel sulfur content
- Number of generators

**Strategy**: 
- Extract with both OpenAI and LangExtract
- Compare values with fuzzy matching (±5% tolerance for numeric values)
- If LangExtract has source citation and values differ significantly, **flag for review** or **override with LangExtract value**
- Require confidence threshold (e.g., 0.85) for automatic override

### Level 2: Important Fields (Discrepancy Flagging)
**Fields important but with acceptable variation:**
- Generator make/model
- Capacity ratings (kW/BHP)
- Control technology descriptions
- Fuel type specifications

**Strategy**:
- Cross-validate but don't override
- Flag discrepancies for manual review
- Include both values in output with confidence scores

### Level 3: Contextual Fields (Sanity Check Only)
**Fields less critical or more interpretive:**
- Facility address formatting
- County names (standardization)
- Date formatting

**Strategy**:
- Basic sanity checks (not null, reasonable format)
- No cross-validation needed

## Implementation Design

### Enhanced QA/QC Flow

```
Input: Permit Text + Schema
    ↓
[Stage 1] OpenAI Extraction
    ↓
    Result: primary_data
    ↓
[Stage 2] LangExtract Validation
    ↓
    Extract: Critical fields only (targeted)
    Result: langextract_data + source_citations
    ↓
[Stage 3] Cross-Validation Engine
    ↓
    ├─→ Compare Critical Fields
    │   ├─→ Match within tolerance → Accept OpenAI
    │   └─→ Significant difference → Flag or Override
    │
    ├─→ Compare Important Fields
    │   └─→ Discrepancy → Add to validation_notes
    │
    └─→ Sanity Checks
        └─→ Unrealistic values → Add warnings
    ↓
[Stage 4] Confidence Scoring
    ↓
    Calculate per-field and overall confidence
    ↓
Output: ExtractionResult + ValidationReport
```

### Confidence Scoring System

**Per-Field Confidence**:
```python
confidence = {
    'source_available': 0.3,      # LangExtract found source text
    'values_match': 0.3,          # OpenAI and LangExtract agree
    'reasonable_value': 0.2,      # Passes sanity checks
    'complete_extraction': 0.2    # All required fields present
}
```

**Override Threshold**: 0.80 (80% confidence)
**Flag Threshold**: 0.60 (60% confidence - below this, flag for review)

### LangExtract Schema Alignment

Update LangExtract extraction classes to match new schema:

```python
extraction_classes = [
    "PERMIT_INFO",      # Permit number, dates, facility info
    "GENERATOR",        # Generator ID, make, model
    "CAPACITY",         # kW, BHP ratings
    "FUEL_SPEC",        # Fuel type, sulfur content, throughput
    "OPERATING_LIMIT",  # Hours per year
    "EMISSION_LIMIT",   # NOx, CO, VOC, PM, PM10, PM2.5, SO2 (lbs/hr, tons/yr)
    "CONTROL_TECH"      # Control technology description
]
```

## Policy-Grade Requirements

### Traceability
- Every critical value must link to source text
- Enable audit trail: "Why did we extract this value?"
- Store LangExtract citations in separate traceability file

### Error Detection Patterns
1. **Unit Mismatches**: OpenAI extracts "2500" but permit says "2500 kW" vs "2500 BHP"
2. **Aggregation Errors**: Confusing per-generator vs. total facility limits
3. **Pollutant Confusion**: PM vs PM10 vs PM2.5 misclassification
4. **Range Interpretation**: "EG01-EG06" should be 6 generators, not 1
5. **Null vs Zero**: Missing data should be null, not 0

### Validation Report Structure

```json
{
    "permit_number": "11790",
    "extraction_timestamp": "2025-10-24T12:00:00Z",
    "overall_confidence": 0.87,
    "validation_summary": {
        "total_fields_extracted": 45,
        "fields_cross_validated": 32,
        "discrepancies_found": 3,
        "overrides_applied": 1,
        "flags_for_review": 2
    },
    "field_validations": [
        {
            "field": "generatorSets[0].noxEmissionLimitLbsHr",
            "openai_value": 53.7,
            "langextract_value": 53.7,
            "confidence": 0.95,
            "status": "validated",
            "source_citation": "NOx 53.7 lbs/hr, 83.75 tons/yr (Page 3, Line 45)"
        },
        {
            "field": "generatorSets[0].operatingHoursLimit",
            "openai_value": 500,
            "langextract_value": 500,
            "confidence": 0.92,
            "status": "validated",
            "source_citation": "shall not operate more than 500 hours per year (Page 4, Line 12)"
        },
        {
            "field": "generatorSets[2].pm10EmissionLimitLbsHr",
            "openai_value": 4.49,
            "langextract_value": 4.5,
            "confidence": 0.65,
            "status": "flagged_minor_discrepancy",
            "reason": "Values differ by 0.22% - within tolerance but flagged",
            "source_citation": "PM-10: 4.5 lbs/hr (Page 5, Line 23)"
        }
    ],
    "recommendations": [
        "Review flagged discrepancies for permit 11790",
        "3 fields have confidence < 0.80 - manual verification recommended"
    ]
}
```

## Cost-Performance Trade-offs

### Option A: Full LangExtract Validation (Most Accurate)
- Extract ALL fields with LangExtract
- Cost: ~$0.015-0.020 per permit
- Time: ~15-20 seconds
- **Best for**: Policy-critical permits, regulatory submissions

### Option B: Targeted Critical Fields (Balanced) ⭐ RECOMMENDED
- Extract only critical emission/operating limits with LangExtract
- Cost: ~$0.004-0.006 per permit
- Time: ~5-8 seconds
- **Best for**: Production pipeline with high accuracy needs

### Option C: Statistical Sampling (Cost-Optimized)
- Run LangExtract on 10% of permits for quality monitoring
- Cost: ~$0.002-0.003 per permit (averaged)
- Time: ~3-4 seconds average
- **Best for**: Large-scale extraction with periodic validation

## Next Steps: Implementation Plan

1. **Update LangExtract Examples** (30 min)
   - Align extraction classes with current schema
   - Add examples for all critical fields

2. **Build Cross-Validation Engine** (2 hours)
   - Field-by-field comparison logic
   - Fuzzy matching for numeric values
   - Override decision tree

3. **Implement Confidence Scoring** (1 hour)
   - Per-field confidence calculation
   - Overall extraction confidence
   - Threshold-based flagging

4. **Create Validation Report Generator** (1 hour)
   - JSON output with traceability
   - Human-readable summary
   - Integration with visualization

5. **Test on Validation Set** (1 hour)
   - Run on 11790, 11541, 73757
   - Compare with ground truth
   - Tune thresholds

6. **Documentation & Training** (30 min)
   - Update README with QA/QC process
   - Create troubleshooting guide

## Success Metrics

- **Accuracy**: >95% on critical fields vs ground truth
- **Precision**: <5% false positive override rate
- **Recall**: >90% error detection rate
- **Cost**: <$0.01 per permit in production
- **Speed**: <10 seconds per permit
- **Traceability**: 100% of critical fields have source citations
