# GitHub Copilot Instructions for ParseSweep

## Project Overview
ParseSweep is a universal document extraction system that uses LLMs to extract structured data from documents (PDFs, DOCX, TXT, XLSX, CSV) into JSON, then compiles into Excel/CSV.

**Version 2.0+**: All schemas MUST include `$metadata` section. No heuristic fallbacks.

## Architecture Baseline

- The contract-first runtime is the active system for this repository.
- Do not add or revive legacy execution paths, schema adapters, or backward-compatibility scaffolding unless explicitly requested.
- Prefer runtime quality, repo hygiene, and repeatable onboarding over expanding optional surface area.
- Keep ownership boundaries explicit:
  - schema: extraction contract, field definitions, identifiers, minimal dedup semantics
  - config (`config/<domain>/run.yaml`): runtime behavior, QA/QC lanes, model tiers, compilation presentation
- Start new domains with the leanest schema that can support a smoke extraction, then expand through iteration.

## Documentation Policy

**Repository Documentation:**
- Main README.md in root for the active product overview, architecture baseline, and core workflows
- schemas/SCHEMA_BEST_PRACTICES.md for schema authoring and runtime-config guidance
- .github/copilot-instructions.md for repository-specific Copilot operating guidance

**DO NOT commit:**
- New migration plans, temporary progress reports, or bootstrap-era implementation notes into the active documentation path
- Keep active docs focused on current operation, onboarding, and authoring guidance
- Put temporary planning material in a local ignored archive or the issue tracker

## Package Manager
**ALWAYS use `pixi` for running commands, NOT pip or python directly.**

## Standard Commands

### Document Processing

**IMPORTANT: Always specify --schema for production use. Without it, the system uses a generic example schema.**

**Geothermal ordinances extraction:**
```bash
pixi run psweep extract documents/geothermal_ordinances/ \
  --schema schemas/personal/geothermal_ordinance_schema.json
```

**Tariff extraction:**
```bash
pixi run psweep extract documents/tariffs/ \
  --schema schemas/personal/electricity_tariff_schema.json \
  --max-context 1400000
```

**Air quality permits extraction:**
```bash
pixi run psweep extract documents/aq_permits/ \
  --schema schemas/personal/air_quality_permits_schema.json
```

**Solar ordinances extraction:**
```bash
pixi run psweep extract documents/solar/ \
  --schema schemas/personal/solar_ordinance_schema.json
```

**Key options:**
- `--schema`: **REQUIRED** for production - Path to schema JSON file (see Schema Requirements below)
- `--output`: Custom output directory (defaults to extracted/category/)
- `--max-context`: Maximum characters to extract (default: 400000, increase for large docs)
- `--pages-csv`: Path to CSV file specifying page ranges (e.g., `config/tariffs/page_ranges.csv`)
- `--enable-qa-qc`: Enable multi-model QA/QC extraction (runs 2+ models, generates comparison reports)
- `--reprocess`: Reprocess already extracted files
- `-n N`: Limit to N files

**Using page ranges:**
```bash
# Extract specific pages from documents
pixi run psweep extract documents/tariffs/ \
  --schema schemas/personal/electricity_tariff_schema.json \
  --pages-csv config/tariffs/page_ranges.csv
```

**Page ranges CSV format (config/category/page_ranges.csv):**
```csv
file_path,start_page,end_page
document1.pdf,615,650
document2.pdf,100,200
full_doc.pdf,,
```

### Consolidation

**IMPORTANT: Specify the same schema used during extraction for best results.**

**Consolidate extracted JSONs to Excel/CSV:**
```bash
pixi run psweep compile extracted/tariffs \\\n  --schema schemas/personal/electricity_tariff_schema.json

pixi run psweep compile extracted/geothermal_ordinances \\\n  --schema schemas/personal/geothermal_ordinance_schema.json

pixi run psweep compile extracted/aq_permits \\\n  --schema schemas/personal/air_quality_permits_schema.json
```

**With custom output:**
```bash
pixi run psweep compile extracted/data \\\n  --schema schemas/your_schema.json \\\n  --output my_analysis/
```

**Outputs:**
- Automatically creates `compiled/` directory
- Generates both `.xlsx` and `.csv` files
- Auto-deduplicates identical entries
- Auto-sizes Excel columns

### Development Commands

**Run tests:**
```bash
pixi run pytest
```

**Install new dependency:**
```bash
# Edit pixi.toml first, then:
pixi install
```

**Run Python script:**
```bash
pixi run python script_name.py
```

## Project Structure

```
ParseSweep/
├── config/                 # Configuration files (page ranges, etc.)
│   ├── tariffs/
│   ├── aq_permits/
│   └── geothermal_ordinances/
├── documents/              # Input documents
│   ├── geothermal_ordinances/
│   ├── aq_permits/
│   └── tariffs/
├── schemas/                # JSON schemas
│   ├── personal/       # Production schemas
│   │   ├── electricity_tariff_schema.json
│   │   ├── geothermal_ordinance_schema.json
│   │   └── air_quality_permits_schema.json
│   └── example_utility_rate_schema.json
├── extracted/              # Raw JSON extractions
│   ├── geothermal_ordinances/
│   ├── aq_permits/
│   └── tariffs/
├── compiled/           # Final Excel/CSV outputs
│   ├── geothermal_ordinances/
│   └── tariffs/
└── src/psweep/
    ├── cli/               # CLI commands
    ├── extraction/        # Document extraction logic
    ├── compilation/     # Data compilation logic
    └── utils/             # Utilities
```

## Supported Document Formats
- PDF (.pdf) - Primary format, includes OCR for image-based PDFs
- Word Documents (.docx, .doc)
- Text files (.txt)
- Excel (.xlsx)
- CSV (.csv)

## Common Workflows

### Full Pipeline (Process → Consolidate)
```bash
# 1. Clear old data (optional)
rm -rf extracted/category/* compiled/category/*

# 2. Process documents (specify correct schema!)
pixi run psweep extract documents/tariffs/ \\\n  --schema schemas/personal/electricity_tariff_schema.json

# 3. Consolidate (use same schema!)
pixi run psweep compile extracted/tariffs/ \\\n  --schema schemas/personal/electricity_tariff_schema.json
```

### Large Document Processing
For documents like complete tariff books, use **page targeting** (preferred) or increase context:

**Option 1 — LLM-assisted page targeting (recommended for config-based runs):**
Uses a cheap keyword scan + one LLM call to find the right pages automatically.
```bash
# pages.auto_locate is configured in the run config YAML
pixi run psweep extract --config config/utility_rate_tariffs/run.yaml
```

**Option 2 — Manual page ranges:**
```bash
pixi run psweep extract documents/tariffs/ \
  --schema schemas/personal/electricity_tariff_schema.json \
  --pages-csv config/tariffs/page_ranges.csv
```

**Option 3 — Increase context window (brute force):**
```bash
pixi run psweep extract documents/tariffs/ \
  --schema schemas/personal/electricity_tariff_schema.json \
  --max-context 1400000
```

### QA/QC Workflow (Multi-Model Validation)
Validate extractions by running 2+ AI models and comparing outputs:
```bash
# Step 1: Run QA/QC extraction on the production schema/runtime path
pixi run psweep extract documents/geothermal_ordinances/ \
  --schema schemas/personal/geothermal_ordinance_schema.json \
  --enable-qa-qc

# Step 2: Generate/regenerate comparison reports (no re-extraction needed)
pixi run psweep compare extracted/geothermal_ordinances/qa_qc \
  --schema schemas/personal/geothermal_ordinance_schema.json

# Optional: evaluate the qualitative review lane explicitly
pixi run psweep compare extracted/geothermal_ordinances/qa_qc \
  --schema schemas/personal/geothermal_ordinance_schema.json \
  --qaqc-lane qualitative

# Step 3: Review comparison_report.xlsx for agreement analysis
```

**QA/QC outputs per document:**
- `model1.json`, `model2.json` - Individual model extractions
- `comparison_report.xlsx` - Item-centric Excel (color-coded: green=agree, red=differ, gray=missing)
- `comparison_report.csv` - Item-centric CSV (same format, one row per item)

**Report format:** Both Excel/CSV show one row per item with combined value+unit (e.g., "1320 feet"). Status column: AGREE, DIFFER, ONLY gpt-5, ONLY gpt-4.1.

## Schema Information

**v2.0+ REQUIREMENT: All schemas MUST include $metadata section:**
```json
{
  "$metadata": {
    "extraction": {
      "main_data_array": "your_array_key",
      "identifier_fields": ["path.to.id"],
      "context_objects": ["metadata_object"]
    },
    "compilation": {
      "deduplication": {
        "key_fields": ["unique_fields"],
        "ignore_fields": ["notes", "timestamp"]
      }
    }
  }
}
```

**Schema Requirements by Document Type:**

| Document Type | Schema Path | Command Example |
|--------------|-------------|------------------|
| Tariffs | `schemas/personal/electricity_tariff_schema.json` | `pixi run psweep extract documents/tariffs/ --schema schemas/personal/electricity_tariff_schema.json` |
| Geothermal Ordinances | `schemas/personal/geothermal_ordinance_schema.json` | `pixi run psweep extract documents/geothermal_ordinances/ --schema schemas/personal/geothermal_ordinance_schema.json` |
| Air Quality Permits | `schemas/personal/air_quality_permits_schema.json` | `pixi run psweep extract documents/aq_permits/ --schema schemas/personal/air_quality_permits_schema.json` |
| Solar Ordinances | `schemas/personal/solar_ordinance_schema.json` | `pixi run psweep extract documents/solar/ --schema schemas/personal/solar_ordinance_schema.json` |

**Critical:**
- The `--schema` flag is **REQUIRED** for production use
- Without it, the system falls back to a generic example schema
- Schema MUST have valid `$metadata` section or extraction will fail
- See `schemas/SCHEMA_BEST_PRACTICES.md` for schema creation guide

## API Configuration

**Environment variables (.env file):**
```bash
# Azure OpenAI (preferred — only key + endpoint needed)
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
# AZURE_OPENAI_MODEL is optional — model comes from config's models: block

# Or OpenAI
OPENAI_API_KEY=sk-your-key
```

## Important Notes

1. **Always use pixi** - Do not use `pip install`, `python`, or `python3` directly
2. **Model tiers** - Define models in `config/<domain>/run.yaml` `models:` block, not CLI flags. Config sections reference tier names (`primary`/`secondary`) via `model:`
3. **Schema required** - Always specify `--schema` for production use (see Schema Requirements)
4. **Sanity checks** - Warnings like "Sanity checks found N issues" are informational, not errors
5. **Multi-format** - System automatically handles PDFs, DOCX, TXT, XLSX, CSV in the same directory
6. **Cost visibility** - Use `--validate-config` (no API calls), `estimate` (cost preview), `run_accounting.json` (post-compile summary)
7. **Cache control** - Use `--reprocess` to re-extract without deleting files; use `--skip-existing` (default) to skip

## Troubleshooting

**"No documents found":**
- Check file extensions are supported (.pdf, .docx, .txt, .xlsx, .csv)
- Verify path exists and contains files

**"Schema missing required $metadata section" (v2.0+):**
- All schemas MUST have $metadata with `main_data_array` and `identifier_fields`
- Update old schemas following examples in schemas/ directory
- See `schemas/SCHEMA_BEST_PRACTICES.md` for migration guide
- Use production schemas as templates: air_quality_permits_schema.json, electricity_tariff_schema.json, geothermal_ordinance_schema.json

**"Schema not found" or using wrong schema:**
- Always use `--schema` flag with full path to schema
- Production schemas are in `schemas/personal/`
- See Schema Requirements table above for correct paths

**Large document timeout:**
- Increase `--max-context` for very long documents
- Default 400k chars is proven reliable for most documents

## Creating New Schemas (v2.0+)

## Greenfield Domain Onboarding

When the user only has raw documents for a new domain, guide them through the current runtime in this order:
1. Inspect representative documents under `documents/<domain>/` and identify the 4-8 highest-value fields the user actually needs first.
2. Start with a lean schema under `schemas/personal/` containing valid `$metadata.extraction`, top-level context objects, and a compact main data array shape. Prefer `pixi run psweep init-domain-schema --name <domain> --reference-schema <closest_schema>` over manually copying a full production schema.
3. If the user already knows the first 4-8 fields they need, prefer `--include-field ...` on `init-domain-schema` so the starter is trimmed immediately instead of expecting manual JSON edits.
4. Use the closest existing schema only as a reference for field patterns and domain phrasing.
5. Keep the separation explicit: schema owns extraction contract and minimal dedup semantics; `config/<domain>/run.yaml` owns runtime modules, QA/QC behavior, model tiers, and compilation presentation.
6. Run `pixi run psweep check-schema ...` and fix schema issues.
7. Add or update `config/<domain>/run.yaml` for runtime settings (models, discovery, QA/QC, compilation output).
8. Validate the resolved command inputs with `pixi run psweep discover --config config/<domain>/run.yaml --validate-config`, then smoke-test `extract` and `compile`.
9. Run `extract` on 1-2 documents first, then `compile`, then optional `compare` QA/QC runs.
10. Iterate on schema fields, page ranges, and qualitative review until extraction quality is acceptable.

Prefer reusing the product commands and tracked runtime files over ad hoc scripts.

**REQUIRED $metadata structure:**
1. `extraction.main_data_array` - Key for the array of items to extract
2. `extraction.identifier_fields` - Fields used to identify each document
3. `extraction.context_objects` - Metadata objects (optional but recommended)
4. `compilation.deduplication.key_fields` - Fields for deduplication

**CRITICAL: Defining key_fields for deduplication**

The `key_fields` must include ALL fields that make an item truly unique. If two items differ in ANY meaningful field value, they should NOT be treated as duplicates.

**Common mistake**: Using too few key_fields, causing distinct items to merge incorrectly.

Example - Tariff charges:
```
❌ WRONG: ["rate_name", "charge_type"]
   Problem: Merges "Service and Facility Charge" + "Production Meter Charge" 
            (both are "Customer charge" type)

✅ CORRECT: ["rate_name", "charge_type", "charge_description", "season", "time_period"]
   Each charge is uniquely identified by its description, season, and time period
```

**Guidelines for choosing key_fields:**
1. Include ALL descriptive fields that distinguish items
2. Exclude only: timestamps, notes, source metadata, extracted text
3. Include: identifiers, names, descriptions, classifications, conditions
4. Test with real data: check if items with different values are being merged
5. When in doubt, include more fields (safer than losing data)

**Verification command:**
```bash
# After compilation, check for unexpected "Merged N duplicate(s)" notes
pixi run python -c "import pandas as pd; df = pd.read_csv('output.csv'); print(df[df['Notes'].str.contains('Merged', na=False)])"
```

**Steps:**
1. Copy an existing schema from schemas/ as a template
2. Modify the properties to match your document structure
3. Update the $metadata section with correct field paths
4. Validate: `pixi run psweep check-schema schemas/your_schema.json`
5. Test on 1-2 documents before full batch

**Example minimal valid schema:**
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$metadata": {
    "domain": "Your Domain",
    "version": "1.0.0",
    "extraction": {
      "main_data_array": "items",
      "identifier_fields": ["metadata.id"],
      "context_objects": ["metadata"]
    },
    "compilation": {
      "deduplication": {
        "key_fields": ["name", "type"],
        "ignore_fields": ["notes"]
      }
    }
  },
  "type": "object",
  "properties": {
    "metadata": {
      "type": "object",
      "properties": {
        "id": {"type": "string"}
      }
    },
    "items": {
      "type": "array",
      "items": {"type": "object"}
    }
  }
}
```
