# Schema Design Best Practices

## Overview

ParseSweep v2.0+ uses a metadata-driven architecture where:
- **`$metadata` section** (required) defines extraction and record-identity behavior
- **Schema properties** define the data structure to extract
- Your schema design directly determines your spreadsheet output

Design schemas to match how you want to analyze the data.

For authoring, use this mental model:

- `Schema` (`schemas/personal/<domain>_schema.json`): The **extraction contract**. Fields, descriptions, examples, row shape, identifiers, and deduplication keys. Field-level enum values belong here (in `properties`), not in `$metadata`.
- `Runtime config` (`config/<domain>/run.yaml`): **Runtime behavior and presentation**. Models, discovery settings, QA/QC lanes, and all compilation output settings (column order, renames, exclude_fields, default_format, freeze_columns). This file drives *how* a run behaves and *how* output looks.

**Separation rule:** `$metadata` in the schema owns `extraction.*` and `identity.deduplication` (data identity — what makes a record unique). Everything else — output formatting, normalization, synthesis, model tiers — lives only in `config/<domain>/run.yaml`. Output config in `$metadata` is not supported and will be rejected by `check-schema`.

---

## Authoring Workflow

### Start With the Schema

Most new extraction setups should begin with a schema only. That keeps iteration fast and local to one file.

Add or update `config/<domain>/run.yaml` later if you need any of the following:

- shared QA/QC lanes for a domain
- shared compilation output settings such as column order or renames
- model tier assignments or discovery configuration
- domain-level runtime overrides you do not want to repeat on the CLI

If none of those apply yet, keep the schema focused on extraction structure.

### Fast Iteration Loop

Use a short loop while evolving a schema:

```bash
# 1. Validate schema structure
pixi run psweep check-schema schemas/my_schema.json

# 2. Extract a tiny sample
pixi run psweep extract documents/sample/ \
  --schema schemas/my_schema.json \
  -n 2 \
  --fresh

# 3. Consolidate the sample
pixi run psweep compile extracted/sample/ \
  --schema schemas/my_schema.json \
  --verbose

# 4. Preview deduplication before writing outputs
pixi run psweep compile extracted/sample/ \
  --schema schemas/my_schema.json \
  --dry-run \
  --report-format json \
  --fail-on-suspicious high
```

Review after each pass:

- Did `main_data_array` create the right rows?
- Did `identifier_fields` identify the document correctly?
- Did your new fields extract consistently?
- Do `key_fields` keep distinct records separate?
- Does the dry-run dedup report show any merges you did not intend?
- Does the dry-run dedup report flag suspicious groups where non-key values disagree?
- Are any suspicious groups ranked `high` severity because numeric or unit fields disagree?
- Should the iteration loop fail automatically until those `high` severity groups are resolved?
- If a `run.yaml` config exists, did verbose output show the resolved config you expected?

The severity ranking is schema-aware: fields declared as numeric in your schema can be ranked `high` severity even if the flattened column name is not obviously numeric.

This loop is the safest way to add fields and tune descriptions without re-running a full batch.

### Do You Need a `run.yaml` config?

Use this decision rule:

- `No`: You are still defining fields, examples, descriptions, identifiers, or deduplication keys for one schema.
- `Yes`: You want shared runtime QA/QC behavior, shared compilation presentation (column order, renames), or domain-level model/discovery settings.

Keep authoring friction low by delaying `config/<domain>/run.yaml` until you actually need shared runtime behavior.

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
    "identity": {
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

This example shows the full metadata surface that belongs in the schema. All output presentation settings (`column_order`, `exclude_fields`, `column_renames`, `freeze_columns`, `auto_width`, `default_format`) and normalization settings belong in `config/<domain>/run.yaml` — not here.

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
    
    "identity": {
      "deduplication": {
        "key_fields": ["rate_name", "charge_type", "season"],
        "ignore_fields": ["notes", "extracted_text"],
        "strategy": "latest",
        "comparison_mode": "exact"
      }
    }
  }
}
```

### Required Fields

| Field | Purpose | Example |
|-------|---------|---------|
| `extraction.main_data_array` | Array that becomes spreadsheet rows | `"rate_schedules"` |
| `extraction.identifier_fields` | Fields that identify the source document | `["metadata.permit_id"]` |
| `identity.deduplication.key_fields` | Fields that determine record uniqueness | `["name", "type", "date"]` |

### Recommended Fields

| Field | Purpose | Example |
|-------|---------|---------|
| `extraction.context_objects` | Top-level objects with metadata | `["metadata", "location"]` |
| `identity.deduplication.ignore_fields` | Fields to ignore when deduplicating | `["notes", "timestamp"]` |
| `domain` | Category for organization | `"Environmental - Air Quality"` |
| `version` | Schema version (semver) | `"2.1.0"` |

### Optional: Validation (QA/QC) Runtime Configuration (in `run.yaml`)

Validation behavior is runtime policy and belongs in the `validation` section of
`config/<domain>/run.yaml` (the `validate` command reads it, mirroring how
`compile` reads `compilation`):

```yaml
validation:
  models: [primary, secondary]      # two model tiers to contrast
  output_dir: validated/<domain>
  comparison_approach: mixed        # mixed | numeric_only | text_review
  record_matching:
    key_fields: [feature, specific_subject, applies_to]
    semantic_match_threshold: 0.38
  comparison:
    primary_fields: [value, units]
  judge:
    enabled: true
    model: judge
    apply_on: all                   # none | non_numeric | all
```

Run `pixi run psweep validate --config config/<domain>/run.yaml` so both models
and all matching/comparison settings come from the same runtime config.

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

The compiler automatically decides based on array characteristics:

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
pixi run psweep check-schema schemas/your_schema.json
```

Checks for:
- Required `$metadata` section
- Valid JSONPath in identifier_fields
- Proper field references

### 2. Test on Sample Documents

```bash
# Extract 1-2 sample documents
pixi run psweep extract documents/samples/ \
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
pixi run psweep compile extracted/samples/

# Review outputs
open compiled/samples/*.xlsx
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

## Value Typing and Obligation Classification

Regulatory extraction schemas (ordinances, permits, tariffs) share a common
problem: a single free-text `value` field ends up carrying *four different kinds
of information at once* — what is regulated, how much, how binding it is, and
under what circumstances. That overloading destroys comparability and dedup.

The fix is to decompose every requirement row on **four axes**:

| Axis | Question | Canonical fields |
|------|----------|------------------|
| **WHAT** | What is regulated? | `feature` + `specific_subject` + `requirement_description` + `applies_to` |
| **HOW MUCH** | What magnitude / bound? | `value` + `value_category` + `value_interpretation` + `units` + `range_low` + `range_high` |
| **HOW BINDING** | Is it mandatory? | `obligation` |
| **WHEN / WHERE** | Under what circumstance / scope? | `condition` + `applies_to` |
| **EVIDENCE** | How do we know? | `source_verbatim` + `summary` + `reasoning` + `section` |

### The three classification fields

**`value_category`** (required, `["quantitative", "qualitative"]`)
- `quantitative`: a measurable magnitude lives in `value` (distance, dBA, acres,
  MW, hours, %, $).
- `qualitative`: there is no magnitude; the substance lives in
  `requirement_description` (or an enumerated set in `applicable_values`), and
  `value` is null.
- Independent of obligation — a quantitative limit can be conditional, and a
  qualitative item can be required.

**`value_interpretation`** (`["exact","minimum","maximum","range","formula","tiered","enumerated", null]`)
- `exact` — single fixed value ("shall be 35 feet").
- `minimum` — floor ("at least", "no less than").
- `maximum` — ceiling ("shall not exceed").
- Coverage-radius phrasing ("show/list/include items within one mile") is usually
  `maximum` (inside a radius cap).
- Separation phrasing ("no structure within 50 feet of a fault") is usually
  `minimum` (required standoff distance).
- `range` — a span; populate `range_low` and `range_high`.
- `formula` — computed ("1.1× turbine height").
- `tiered` — value depends on a band; **emit one row per tier** with the band in
  `condition`.
- `enumerated` — a discrete set of allowed values; use with `applicable_values`.
- `null` — qualitative requirements (present / absent).

**`obligation`** (required, `["required","prohibited","conditional","allowed","recommended","informational"]`)

Classify from the governing clause, with these normalization rules:
- For **quantitative limits/setbacks/windows**, use `obligation = "required"` and encode direction in `value_interpretation` (`minimum` / `maximum` / `range`) even when wording is negative ("shall not exceed", "shall not be within").
- Reserve `obligation = "prohibited"` for **qualitative bans** without a measurable threshold.
- For permit-gated phrasing ("may be permitted only through CUP"), model the permit gate as `obligation = "required"` on the approval row; avoid flipping between `allowed` and `required` for the same gate.

| Governing language | `obligation` |
|--------------------|--------------|
| "shall", "must", "is required", "no less than" | `required` |
| "shall not", "no person shall", "may not", "is not permitted", "prohibited" (non-numeric ban) | `prohibited` |
| "may require", "at the discretion of", "as a condition of approval", "unless waived" | `conditional` |
| "is permitted", "may be located", "is allowed" | `allowed` |
| "should", "encouraged", "best available technology" | `recommended` |
| definitions, procedures, measured baselines, external standards | `informational` |

When multiple verbs appear, precedence is:
`prohibited > required > conditional > recommended > allowed > informational`.

### LLM prompt instructions to embed in field descriptions

- **obligation:** "Never place obligation language inside `value` or
  `requirement_description`. For quantitative rows, use `required` and encode
  direction in `value_interpretation` even if wording is negative. Reserve
  `prohibited` for qualitative bans. Cite trigger language in `reasoning`."
- **source_verbatim:** "Exact word-for-word operative clause; never paraphrase.
  If it exceeds 50 words, quote the operative head, insert '…', then the tail."
- **reasoning:** "Terse `trigger → label`, ≤15 words." e.g.
  `"'may require' → conditional; no magnitude → qualitative"`.
- **requirement_description:** "No obligation language — never include shall / may
  / must / required / prohibited."

### Anti-patterns

| ❌ Anti-pattern | ✅ Correct |
|----------------|-----------|
| `value = "shall not exceed 55 dBA"` | `value = 55`, `units = "dBA"`, `value_interpretation = "maximum"`, `obligation = "required"` |
| `value = "shall not be within 50 feet of an active fault"` + `obligation = "prohibited"` | `value = 50`, `units = "feet"`, `value_interpretation = "minimum"`, `obligation = "required"` |
| `value_interpretation = "exact"` for `"within one mile"` in map/reporting scope | `value_interpretation = "maximum"` (coverage radius cap) |
| `value = "Sound barriers may be required"` | `requirement_description = "Sound barriers"`, `value_category = "qualitative"`, `obligation = "conditional"` |
| `requirement_description = "Fencing is required"` | `requirement_description = "Solid perimeter fencing"`, `obligation = "required"` |
| `obligation = "allowed"` for `"...may be permitted only through CUP"` | `obligation = "required"` on the permit-gate approval row |
| One row per permitted district (`AG`, `RE`, `R1`…) | one row, `value_interpretation = "enumerated"`, `applicable_values = ["AG","RE","R1"]` |
| `applies_to = "nighttime"` | `condition = "nighttime"` (applies_to is the *facility type*, not the trigger) |
| Paraphrasing in `source_verbatim` | copy the clause verbatim; paraphrase only in `summary` |

### Before / after (natural gas noise limit)

Old overloaded single field:

```
value = "Compressor stations shall not exceed 55 dBA at night at the property line"
```

New multi-field decomposition:

```json
{
  "feature": "noise",
  "specific_subject": "nighttime limit",
  "applies_to": "Compressor station",
  "value_category": "quantitative",
  "value": 55,
  "value_interpretation": "maximum",
  "units": "dBA",
  "obligation": "required",
  "condition": "nighttime",
  "source_verbatim": "...shall not exceed 55 dBa...at night...at the property line.",
  "reasoning": "'shall not exceed' → required; number → quantitative/maximum"
}
```

---

## Cross-Domain Consistency Guidelines

Regulatory domains (natural gas, geothermal, solar, and future domains) share the
same requirement-row shape. Keeping field **names and semantics identical** across
domains lets one compilation, dedup, and QA/QC path serve every domain, and lets
analysts compare across domains.

### QA/QC hardening conventions (recommended across all regulatory domains)

These conventions came from live multi-model validation runs and improve
precision/recall **without adding domain-specific runtime code**:

1. **Add a row-type discriminator when rows mix different requirement kinds.**
   - Use a canonical field like `rule_kind` (enum) to distinguish
     `limit`, `deadline`, `duration`, `monitoring`, `equipment_standard`, etc.
   - Include it in schema dedup keys **and** QA/QC `record_matching.key_fields`
     in `config/<domain>/run.yaml` so like-for-like rows are compared.
2. **Encode quantitative vs qualitative constraints in the schema.**
   - Quantitative rows should require numeric semantics (`value`, `units`,
     `value_interpretation`).
   - Qualitative rows should force quantitative-only fields to null.
3. **Keep schema as contract; keep QA/QC policy in config.**
   - Schema: field shape, enums, conditional constraints, dedup identity.
   - Config: model pair, judge model, comparison fields, matching behavior.
4. **Treat output shape drift as a first-class validation failure.**
   - `main_data_array` is contractually an array; if provider output drifts
     (e.g., object-map keyed by `"0"`, `"1"`), normalize deterministically in
     runtime and log it, then continue comparison.
5. **Do not choose production models from agreement % alone.**
   - Use multi-doc runs and source-grounded review of `ONLY model-x` and `DIFFER`
     rows to determine whether misses are recall gaps or true false positives.

### Canonical requirement-row fields (universal — same name in every domain)

| Field | Type | Required | Axis | Notes |
|-------|------|----------|------|-------|
| `feature` | string (enum) | ✅ | WHAT | Domain-specific **enum values**, universal field name |
| `specific_subject` | string \| null | | WHAT | Sub-aspect; participates in dedup key |
| `applies_to` | string \| null | | WHAT / WHERE | Governed facility type, document's terminology |
| `requirement_description` | string \| null | | WHAT | Obligation-free description of qualitative rows |
| `value_category` | string | ✅ | HOW MUCH | `quantitative` / `qualitative` |
| `value` | number \| string \| null | | HOW MUCH | Magnitude only; no units, no obligation words |
| `value_interpretation` | string \| null | | HOW MUCH | exact / minimum / maximum / range / formula / tiered / enumerated |
| `units` | string \| null | | HOW MUCH | Null for qualitative |
| `range_low` | number \| null | | HOW MUCH | Lower bound when `value` is a range |
| `range_high` | number \| null | | HOW MUCH | Upper bound when `value` is a range |
| `obligation` | string | ✅ | HOW BINDING | Full controlled vocabulary |
| `condition` | string \| null | | WHEN / WHERE | Situational trigger, **not** the facility type |
| `applicable_values` | array \| null | | HOW MUCH | Enumerated interchangeable values |
| `source_verbatim` | string \| null | | EVIDENCE | Exact quote; audit anchor |
| `summary` | string \| null | | EVIDENCE | Paraphrase allowed (omit if domain has no summary lane) |
| `reasoning` | string \| null | | EVIDENCE | `trigger → label`, hidden from output by default |
| `section` | string \| null | | EVIDENCE | Ordinance / permit section reference |
| `notes` | string \| null | | EVIDENCE | Exceptions, cross-references |

### What varies per domain vs. what is universal

- **Varies per domain:** the `feature` **enum values**; the domain-specific
  **examples** inside each field description; `document_applicability` gate fields;
  `jurisdiction` / identifier structure; `$metadata.domain` and `version`.
- **Universal (do not rename):** every field **name** above, the
  `value_category` / `value_interpretation` / `obligation` vocabularies (defined
  in `properties[*].enum`), and the canonical `key_fields` / `ignore_fields` in
  `$metadata.identity.deduplication`.

### Canonical `$metadata` blocks

Use the same dedup keys in every regulatory domain (vocabularies belong in `properties[*].enum`, not in `$metadata`):

```json
"deduplication": {
  "key_fields": ["feature", "specific_subject", "applies_to", "value_category", "value", "value_interpretation", "units", "obligation", "condition"],
  "ignore_fields": ["source_verbatim", "summary", "reasoning", "notes", "applicable_values", "range_low", "range_high"]
}
```

> Jurisdiction context (state / county / subdivision) does **not** need to be in
> `key_fields` — compilation automatically groups by jurisdiction and never merges
> rows across jurisdictions. `ignore_fields` may omit `summary` for a domain that
> has no summary field.

### Creating a new regulatory domain schema

1. Copy an existing regulatory schema (`natural_gas_pipeline_schema.json` is the
   reference) as a template.
2. Replace the `feature` enum with your domain's standardized features.
3. Update the domain-specific **examples** in `specific_subject`, `applies_to`,
   `condition`, and `requirement_description` — keep the field names and their
   instructive descriptions intact.
4. Set `$metadata.domain` and bump `version`; keep the canonical
   `key_fields` and `ignore_fields` in `$metadata.identity.deduplication` unchanged.
5. Adjust `document_applicability` and `jurisdiction` gate fields for the domain.
6. Validate: `pixi run psweep check-schema schemas/personal/<domain>_schema.json`.

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
- **File:** `personal/geothermal_ordinance_schema.json`
- **Pattern:** Requirements-based, 4-axis value typing
- **Main array:** `requirements`
- **Good for:** Regulatory compliance

### Natural Gas Pipelines & Compressor Stations
- **File:** `personal/natural_gas_pipeline_schema.json`
- **Pattern:** Requirements-based, 4-axis value typing (reference schema for new regulatory domains)
- **Main array:** `requirements`
- **Good for:** Regulatory compliance, cross-jurisdiction comparison

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
| **Regulatory value typing** | Decompose rows on 4 axes: WHAT / HOW MUCH / HOW BINDING / WHEN·WHERE |
| **`value_category` / `obligation`** | Required classifier fields for regulatory schemas |
| **Cross-domain consistency** | Keep canonical field names identical across domains; vary only `feature` enums + examples |

---

## Tips for Success

1. **Start with the end in mind** - Design your schema based on how you want to analyze the data in Excel
2. **Use production schemas as templates** - Copy and modify rather than starting from scratch
3. **Test early and often** - Extract 1-2 documents before processing hundreds
4. **Keep it simple** - Fewer levels of nesting = clearer outputs
5. **Be consistent** - Same structure across all items in an array enables expansion
6. **Include context** - Add fields that distinguish similar items (season, type, tier)
7. **Validate first** - Use `check-schema` command before extraction

**Remember:** Good schema design = good data analysis. Take time to structure your schema correctly.
