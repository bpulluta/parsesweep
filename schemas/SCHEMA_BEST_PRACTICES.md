# Schema Design Best Practices

## Overview

StreamlineExtract v2.0+ uses a metadata-driven architecture where:
- **`$metadata` section** (required) defines extraction and consolidation behavior
- **Schema properties** define the data structure to extract
- Your schema design directly determines your spreadsheet output

Design schemas to match how you want to analyze the data.

---

## 🔴 Required: The `$metadata` Section

Every schema MUST include a `$metadata` section. Without it, extraction will fail:
```
ERROR: Schema missing required $metadata section
```

### Minimal Valid Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$metadata": {
    "domain": "Your Domain",
    "version": "1.0.0",
    "extraction": {
      "main_data_array": "items",
      "identifier_fields": ["metadata.document_id"],
      "context_objects": ["metadata"]
    },
    "consolidation": {
      "deduplication": {
        "key_fields": ["item_name"],
        "ignore_fields": ["notes"]
      }
    }
  },
  "type": "object",
  "properties": {
    "metadata": {
      "type": "object",
      "properties": {
        "document_id": {"type": "string"}
      }
    },
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "item_name": {"type": "string"}
        }
      }
    }
  }
}
```

### Complete `$metadata` Structure

```json
{
  "$metadata": {
    "domain": "Energy - Utility Tariffs",
    "version": "1.0.0",
    "description": "Extract rate schedules and charges from utility tariff documents",
    
    "extraction": {
      "main_data_array": "rate_schedules",
      "identifier_fields": [
        "utility_info.utility_name",
        "utility_info.state"
      ],
      "context_objects": [
        "utility_info",
        "document_applicability"
      ],
      "display_name_template": "{utility_name} ({state})",
      "document_type": "Utility Tariff"
    },
    
    "consolidation": {
      "deduplication": {
        "key_fields": ["rate_name", "charge_type", "season"],
        "ignore_fields": ["notes", "extracted_text"],
        "strategy": "latest",
        "comparison_mode": "exact"
      },
      "output": {
        "default_format": "excel",
        "column_order": ["utility_name", "rate_name", "charge_type"],
        "freeze_columns": 2,
        "auto_width": true
      }
    },
    
    "validation": {
      "required_fields": ["utility_info", "rate_schedules"],
      "quality_checks": [
        {
          "field": "rate",
          "type": "numeric",
          "message": "Rate should be numeric when applicable"
        }
      ],
      "completeness_threshold": 0.8
    },
    
    "qa_qc": {
      "comparison": {
        "primary_fields": ["rate", "amount", "unit"],
        "secondary_fields": ["rate_name", "charge_type", "season"],
        "field_types": {
          "rate": "numeric",
          "amount": "numeric",
          "unit": "text_normalized",
          "rate_name": "text_exact",
          "charge_type": "text_exact",
          "season": "text_exact"
        },
        "numeric_tolerance": 0.0,
        "fuzzy_threshold": 0.85
      },
      "record_matching": {
        "key_fields": ["rate_name", "charge_type"],
        "fuzzy_match": true,
        "match_threshold": 0.8
      },
      "ignore_fields": ["notes", "details"]
    }
  }
}
```

### Required Fields

| Field | Purpose | Example |
|-------|---------|---------|
| `extraction.main_data_array` | Array that becomes spreadsheet rows | `"rate_schedules"` |
| `extraction.identifier_fields` | Fields that identify the source document | `["metadata.permit_id"]` |
| `consolidation.deduplication.key_fields` | Fields that determine record uniqueness | `["name", "type", "date"]` |

### Recommended Fields

| Field | Purpose | Example |
|-------|---------|---------|
| `extraction.context_objects` | Top-level objects with metadata | `["metadata", "location"]` |
| `consolidation.deduplication.ignore_fields` | Fields to ignore when deduplicating | `["notes", "timestamp"]` |
| `domain` | Category for organization | `"Environmental - Air Quality"` |
| `version` | Schema version (semver) | `"2.1.0"` |

### Optional: QA/QC Configuration

The `qa_qc` section configures multi-model comparison behavior (used with `--enable-qa-qc`):

| Field | Purpose | Example |
|-------|---------|---------|
| `comparison.primary_fields` | Critical fields for accuracy | `["value", "unit"]` |
| `comparison.secondary_fields` | Important classification fields | `["category", "type"]` |
| `comparison.field_types` | How to compare each field | `{"value": "numeric"}` |
| `record_matching.key_fields` | Fields to match records across models | `["category", "name"]` |
| `ignore_fields` | Fields to skip during comparison | `["notes", "details"]` |

**Field Type Options:**
| Type | Description |
|------|-------------|
| `numeric` | Compare only numeric content (extract numbers from text) |
| `text_exact` | Exact string match |
| `text_normalized` | Normalize before comparing (e.g., "feet" = "ft") |
| `text_fuzzy` | Similarity matching with threshold |

**JSONPath Notation**: Use dot notation for nested fields: `"parent.child.field"`

---

## The Golden Rule: Main Array = Rows

The array specified in `$metadata.extraction.main_data_array` becomes rows in your spreadsheet.

**Example:**
```json
{
  "$metadata": {
    "extraction": {
      "main_data_array": "rate_schedules",
      "context_objects": ["utility_info"]
    }
  },
  "utility_info": {
    "utility_name": "Metro Electric",
    "state": "CA"
  },
  "rate_schedules": [
    {"rate_name": "Schedule R", "type": "Residential"},
    {"rate_name": "Schedule C", "type": "Commercial"}
  ]
}
```

**Result**: 2 rows in spreadsheet
- Row 1: Metro Electric (CA) - Schedule R - Residential
- Row 2: Metro Electric (CA) - Schedule C - Commercial

Context objects (`utility_info`) are included in every row.

---

## Data Structure Design

### Nested Arrays: Automatic Expansion vs Summarization

The consolidator automatically decides based on array characteristics:

#### ✅ Expands to Columns (Best for Analysis)

**Criteria:**
- ≤15 items
- ≤20 fields per item
- Consistent structure (same fields across items)

**Example:**
```json
"charges": [
  {"type": "Customer", "rate": 7.10, "unit": "$"},
  {"type": "Energy", "rate": 0.1038, "unit": "$/kWh"},
  {"type": "Demand", "rate": 5.00, "unit": "$/kW"}
]
```

**Result:** Separate columns for each charge type
- `Charges Customer Rate` = 7.10
- `Charges Customer Unit` = $
- `Charges Energy Rate` = 0.1038
- `Charges Energy Unit` = $/kWh
- `Charges Demand Rate` = 5.00
- `Charges Demand Unit` = $/kW

#### ✅ Summarizes to Text (Best for Context)

**Criteria:**
- >15 items, OR
- >20 fields per item, OR
- Inconsistent structure

**Example:**
```json
"permit_history": [
  {"date": "2023-01-15", "action": "Applied", "officer": "J. Smith"},
  {"date": "2023-02-20", "action": "Reviewed", "officer": "M. Jones"},
  ... 28 more items
]
```

**Result:** Single column with summary text
- `Permit History` = "Applied: 2023-01-15 (J. Smith); Reviewed: 2023-02-20 (M. Jones); ..."

---

## Design Patterns

### Pattern 1: Entity-Based (Recommended)

**Best for:** Most use cases where you have a clear primary entity

✅ **Good:**
```json
{
  "$metadata": {
    "extraction": {
      "main_data_array": "rate_schedules",
      "context_objects": ["utility_info"]
    }
  },
  "utility_info": {
    "utility_name": "Metro Electric",
    "state": "CA"
  },
  "rate_schedules": [
    {
      "rate_name": "Residential",
      "charges": [
        {"type": "Customer", "rate": 7.10},
        {"type": "Energy", "rate": 0.1038}
      ]
    }
  ]
}
```

**Result:** One row per rate schedule, with charges expanded as columns

❌ **Bad:**
```json
{
  "$metadata": {
    "extraction": {
      "main_data_array": "charges"
    }
  },
  "charges": [
    {"rate_name": "Residential", "type": "Customer", "rate": 7.10},
    {"rate_name": "Residential", "type": "Energy", "rate": 0.1038},
    {"rate_name": "Commercial", "type": "Customer", "rate": 9.50},
    ... 50 more charges
  ]
}
```

**Problem:** Creates 50+ rows instead of a few organized rate schedules

### Pattern 2: Include Distinguishing Context

Always include fields that differentiate similar items.

✅ **Good:**
```json
{
  "type": "Energy",
  "rate": 0.1038,
  "season": "Summer",
  "time_period": "On-peak",
  "tier": "1"
}
```

**Result:** Clear column names
- `Charges Energy Summer On-Peak Tier 1 Rate`

❌ **Bad:**
```json
{
  "type": "Energy",
  "rate": 0.1038
}
```

**Problem:** Lost context - which season? which time period?

### Pattern 3: Consistent Field Structure

Use the same fields across all items in an array.

✅ **Good:**
```json
"requirements": [
  {"category": "Setback", "value": 1320, "unit": "feet"},
  {"category": "Height", "value": 35, "unit": "feet"},
  {"category": "Noise", "value": 50, "unit": "dBA"}
]
```

**Result:** Clean expansion with predictable columns

❌ **Bad:**
```json
"requirements": [
  {"category": "Setback", "distance": 1320, "from": "residence"},
  {"category": "Height", "max_height": 35},
  {"category": "Noise", "limit_day": 50, "limit_night": 45}
]
```

**Problem:** Inconsistent fields → forced to summarize instead of expand

---

## Optimization Guidelines

### For Quantitative Analysis

**Goal:** Structured, analyzable columns

1. **Keep nested arrays small** (≤15 items)
2. **Use consistent field names** across all array items
3. **Include distinguishing context** (season, tier, period, type)
4. **Separate values from units**

**Example:**
```json
"charges": [
  {
    "charge_type": "Energy",
    "rate": 0.1038,
    "unit": "$/kWh",
    "season": "Summer",
    "time_period": "On-peak",
    "tier": "1"
  }
]
```

---

## Temporal Metadata Best Practices

**Why it matters:** Most documents have a critical time dimension - when they were enacted, when they're effective, when they expire. Without temporal metadata, you can't determine if data is current, track changes over time, or perform temporal analysis.

### Common Temporal Fields by Document Type

| Document Type | Critical Temporal Fields |
|---------------|--------------------------|
| **Ordinances/Regulations** | `date_adopted`, `date_effective`, `date_last_amended`, `supersedes` |
| **Permits** | `permit_issuance_date`, `permit_expiration_date`, `renewal_date` |
| **Tariffs/Rate Schedules** | `effective_date`, `revision_date`, `filing_date`, `supersedes` |
| **Journal Articles** | `publication_date`, `received_date`, `revised_date`, `accepted_date` |
| **Financial Filings** | `filing_date`, `fiscal_year_end`, `period_start`, `period_end` |
| **Contracts** | `execution_date`, `effective_date`, `expiration_date`, `renewal_date` |

### Design Recommendations

**1. Place in context objects** - Temporal fields typically describe the document, not individual data items, so include them in context objects (metadata, jurisdiction, utility_info, etc.)

**2. Allow as-written formats** - Documents use varied date formats. Capture exactly as written for accuracy:
```json
"date_adopted": {
  "type": ["string", "null"],
  "description": "Date adopted, exactly as written (e.g., 'May 17, 2013', '10/6/2015')"
}
```

**3. Make fields nullable** - Not all documents state all dates explicitly:
```json
"type": ["string", "null"],
"description": "... Null if not stated."
```

**4. Include in column_order** - Place temporal fields early in Excel output for visibility:
```json
"column_order": [
  "utility_name",
  "state",
  "effective_date",
  "revision_date",
  "rate_name",
  ...
]
```

**5. Provide extraction guidance** - Help the LLM find temporal information:
```json
"description": "Date the ordinance became effective, exactly as written. Look for phrases like 'effective date', 'shall become effective on', 'in effect as of'. Null if not stated."
```

### Example: Ordinance Schema

```json
{
  "jurisdiction": {
    "type": "object",
    "properties": {
      "state": {"type": "string"},
      "county": {"type": "string"},
      "ordinance_code": {"type": ["string", "null"]},
      "date_adopted": {
        "type": ["string", "null"],
        "description": "Date adopted by legislative body, exactly as written (e.g., 'May 17, 2013'). Look for 'adopted on', 'enacted on', 'passed on'. Null if not stated."
      },
      "date_effective": {
        "type": ["string", "null"],
        "description": "Date became law, exactly as written. Look for 'effective date', 'shall become effective on'. Null if not stated."
      },
      "date_last_amended": {
        "type": ["string", "null"],
        "description": "Most recent amendment date. Look for 'amended on', 'as amended', 'last revised'. Null if not stated."
      },
      "supersedes": {
        "type": ["string", "null"],
        "description": "Previous ordinance this replaces. Look for 'replaces', 'supersedes', 'repeals and replaces'. Null if not stated."
      }
    }
  }
}
```

### When to Use Structured vs As-Written Dates

**Structured (YYYY-MM-DD):**
- ✅ When dates are consistently formatted in source documents
- ✅ When downstream analysis requires date arithmetic
- ✅ For permits, filings with standardized formats
- Example: `"permit_issuance_date": "2024-01-15"`

**As-Written:**
- ✅ When date formats vary widely across documents
- ✅ When preserving exact source text is important
- ✅ For ordinances, legal documents with varied formats
- Example: `"date_adopted": "May 17, 2013"`

**Hybrid Approach:**
```json
"effective_date_raw": "January 1st, 2024",
"effective_date_normalized": "2024-01-01"
```

---

### For Qualitative/Contextual Data

**Goal:** Readable summaries

1. **Use descriptive name/type fields**
2. **Let large arrays summarize automatically**
3. **Include important details in text**

**Example:**
```json
"inspection_notes": [
  {
    "date": "2024-01-15",
    "inspector": "J. Smith",
    "findings": "Equipment in good condition",
    "action_required": "None"
  },
  ... many more notes
]
```

---

## Common Mistakes

### ❌ Missing `$metadata` Section

**Problem:** Extraction fails immediately

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "properties": {
    "items": {"type": "array"}
  }
}
```

**Error:** `Schema missing required $metadata section`

✅ **Solution:** Always include complete `$metadata`

### ❌ Flat Structure Instead of Nested

**Problem:** Hard to extend, loses organization

```json
{
  "rate_name": "Schedule R",
  "customer_charge": 7.1,
  "energy_charge_summer": 0.1038,
  "energy_charge_winter": 0.0857,
  "demand_charge_summer": 5.0
}
```

✅ **Better:**
```json
{
  "rate_name": "Schedule R",
  "charges": [
    {"type": "Customer", "rate": 7.1},
    {"type": "Energy", "season": "Summer", "rate": 0.1038},
    {"type": "Energy", "season": "Winter", "rate": 0.0857},
    {"type": "Demand", "season": "Summer", "rate": 5.0}
  ]
}
```

### ❌ Excessive Nesting

**Problem:** Creates unreadable column names

```json
{
  "rate_schedules": [
    {
      "details": {
        "info": {
          "basics": {
            "rate_name": "Schedule R"
          }
        }
      }
    }
  ]
}
```

**Result:** `Rate Schedules Details Info Basics Rate Name` (too long!)

✅ **Better:** Keep to 2-3 levels maximum
```json
{
  "rate_schedules": [
    {
      "rate_name": "Schedule R",
      "effective_date": "2024-01-01"
    }
  ]
}
```

### ❌ Single-Item Arrays

**Problem:** Unnecessary complexity

```json
{
  "rate_name": ["Schedule R"],
  "effective_date": ["2024-01-01"]
}
```

✅ **Better:** Use simple values
```json
{
  "rate_name": "Schedule R",
  "effective_date": "2024-01-01"
}
```

### ❌ Wrong Main Array Selection

**Problem:** Creates too many or too few rows

```json
{
  "$metadata": {
    "extraction": {
      "main_data_array": "charges"  // ❌ Too granular
    }
  },
  "rate_schedules": [
    {
      "rate_name": "Schedule R",
      "charges": [
        {"type": "Customer", "rate": 7.1},
        {"type": "Energy", "rate": 0.1038},
        ... 20 more charges
      ]
    }
  ]
}
```

**Result:** 20+ rows per rate schedule instead of 1 row with expanded charge columns

✅ **Better:**
```json
{
  "$metadata": {
    "extraction": {
      "main_data_array": "rate_schedules"  // ✅ Right level
    }
  }
}
```

---

## Testing Your Schema

### 1. Validate Schema Structure

```bash
pixi run streamline-extract validate-schema schemas/your_schema.json
```

Checks for:
- Required `$metadata` section
- Valid JSONPath in identifier_fields
- Proper field references

### 2. Test on Sample Documents

```bash
# Extract 1-2 sample documents
pixi run streamline-extract extract documents/samples/ \
  --schema schemas/your_schema.json \
  --limit 2

# Review JSON outputs
ls -la extracted/samples/
cat extracted/samples/sample_001.json
```

Check:
- Is the `main_data_array` populated correctly?
- Are nested arrays the right size?
- Are field names consistent?

### 3. Test Consolidation

```bash
# Consolidate to Excel/CSV
pixi run streamline-extract consolidate extracted/samples/

# Review outputs
open consolidated/samples/*.xlsx
```

Verify:
- Correct number of rows
- Nested arrays expanded/summarized as expected
- Column names are clear and readable
- Data properly deduplicated

### 4. Iterate and Refine

Common adjustments:
- Change `main_data_array` to different level
- Adjust nested array structure
- Add/modify `key_fields` for better deduplication
- Reorganize data to reduce nesting depth

---

## Production Examples

Reference these working schemas in the `schemas/` directory:

### Air Quality Permits
- **File:** `air_quality_permits_schema.json`
- **Pattern:** Permit-based with equipment nested
- **Main array:** `equipment`
- **Good for:** Environmental compliance tracking

### Electricity Tariffs
- **File:** `electricity_tariff_schema.json`
- **Pattern:** Rate schedule with charges nested
- **Main array:** `rate_schedules`
- **Good for:** Utility rate analysis

### Geothermal Ordinances
- **File:** `geothermal_ordinance_schema.json`
- **Pattern:** Requirements-based
- **Main array:** `requirements`
- **Good for:** Regulatory compliance

---

## Quick Reference

| Aspect | Guideline |
|--------|-----------|
| **`$metadata` section** | Required - extraction fails without it |
| **`main_data_array`** | Becomes spreadsheet rows - choose entity level carefully |
| **Nested arrays ≤15 items** | Expands to columns (good for analysis) |
| **Nested arrays >15 items** | Summarizes to text (good for context) |
| **Field consistency** | Same fields across items → clean expansion |
| **Distinguishing fields** | Include season/period/tier for clarity |
| **Nesting depth** | 2-3 levels maximum for readable columns |
| **Context objects** | Include in every row automatically |
| **Deduplication** | Use `key_fields` to define uniqueness |

---

## Tips for Success

1. **Start with the end in mind** - Design your schema based on how you want to analyze the data in Excel
2. **Use production schemas as templates** - Copy and modify rather than starting from scratch
3. **Test early and often** - Extract 1-2 documents before processing hundreds
4. **Keep it simple** - Fewer levels of nesting = clearer outputs
5. **Be consistent** - Same structure across all items in an array enables expansion
6. **Include context** - Add fields that distinguish similar items (season, type, tier)
7. **Validate first** - Use `validate-schema` command before extraction

**Remember:** Good schema design = good data analysis. Take time to structure your schema correctly.
