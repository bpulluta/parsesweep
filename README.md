# ParseSweep

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

**AI-powered document extraction pipeline.**

ParseSweep discovers, extracts, and compiles structured data from documents into Excel/CSV. Define what you need with a JSON schema, configure how to run it with a YAML config, and let the AI handle the rest.

## What It Does

```
Documents (PDF, DOCX, TXT, XLSX)
        │
        ▼
┌─── discover ───┐    ┌─── extract ───┐    ┌─── compile ───┐
│ Find docs on   │ →  │ AI extracts   │ →  │ Merge + dedup │ → Excel/CSV
│ the web        │    │ structured    │    │ into clean    │
│ (optional)     │    │ JSON per doc  │    │ spreadsheet   │
└────────────────┘    └───────────────┘    └───────────────┘
```

Each step runs independently via the same config file.

---

## Quick Start

```bash
# Install and setup
curl -fsSL https://pixi.sh/install.sh | bash
git clone https://github.com/bpulluta/parsesweep.git && cd parsesweep
pixi install
pixi run psweep init   # configure API credentials

# Run extraction
pixi run psweep extract --config config/utility_rate_tariffs/run.yaml

# Compile results into Excel
pixi run psweep compile --config config/utility_rate_tariffs/run.yaml
```

---

## Architecture

### Two Files Per Domain

| File | Role |
|------|------|
| **Schema** (JSON) | Defines WHAT to extract — field names, types, descriptions |
| **Config** (YAML) | Defines HOW to run — paths, page targeting, dedup, output format |

### Config Is the Single Entry Point

```bash
# Every command takes --config
psweep discover --config config/my_domain/run.yaml
psweep extract  --config config/my_domain/run.yaml
psweep compile  --config config/my_domain/run.yaml
```

Each command reads its own section from the config:

```yaml
# config/my_domain/run.yaml
domain: my_domain

extraction:
  schema: schemas/personal/my_schema.json
  input_dir: documents/my_domain          # where documents live
  output_dir: extracted/my_domain
  pages:
    auto_locate:
      section_description: "the section with rate tables"
      trigger_chars: 200000

compilation:
  schema: schemas/personal/my_schema.json
  input_dir: extracted/my_domain          # default: matches extraction output_dir
  output_dir: compiled/my_domain
  deduplication:
    key_fields: [name, type, value]
  output:
    default_format: excel

discovery:   # optional — only for web document acquisition
  targets_csv: targets.csv
  queries:
    - "{entity} {state} filetype:pdf"
```

### Modular by Design

Run any step independently or chain them:

```bash
# Full pipeline
psweep discover --config config/tariffs/run.yaml
psweep extract  --config config/tariffs/run.yaml
psweep compile  --config config/tariffs/run.yaml

# Or just extract from local documents
psweep extract --config config/tariffs/run.yaml

# Or just compile existing extractions
psweep compile --config config/tariffs/run.yaml
```

---

## Commands

| Command | Purpose |
|---------|---------|
| `extract` | Extract structured data from documents to JSON |
| `compile` | Merge extracted JSONs into Excel/CSV with deduplication |
| `discover` | Find and download documents from the web |
| `curate` | Filter discovered documents via review |
| `compare` | Generate QA/QC comparison reports (multi-model) |
| `benchmark` | Performance profiling across runs |
| `check` | Validate an extraction result |
| `check-schema` | Validate a schema file |
| `init` | Interactive project setup |
| `init-domain-schema` | Create a starter schema for a new domain |
| `preview` | Preview document content |
| `estimate` | Estimate extraction cost/time |
| `config` | Show current configuration |

### Extract

```bash
pixi run psweep extract --config config/my_domain/run.yaml

# Options
  --config PATH       Config YAML (required for full features)
  --schema PATH       Schema-only mode (quick testing without a config file)
  -n N                Limit to N files
  --reprocess         Re-extract existing files
  --enable-qa-qc      Run multi-model QA/QC
  --live-dashboard    Real-time progress display
  --validate-config   Dry run — validate inputs only
```

### Compile

```bash
pixi run psweep compile --config config/my_domain/run.yaml

# Options
  --config PATH       Config YAML
  --schema PATH       Schema-only mode
  --dry-run           Preview deduplication without writing
  --validate-config   Dry run — validate inputs only
```

### Discover

```bash
pixi run psweep discover --config config/my_domain/run.yaml

# Options
  --config PATH       Config YAML (required)
  --dry-run           Show plan without downloading
  --max-downloads N   Limit downloads
```

---

## Setting Up a New Domain

### 1. Create Schema

Define what to extract:

```bash
pixi run psweep init-domain-schema --name my_domain \
  --reference-schema schemas/example_utility_rate_schema.json
```

Or create manually — a minimal schema:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$metadata": {
    "extraction": {
      "main_data_array": "items",
      "identifier_fields": ["metadata.name"],
      "context_objects": ["metadata"]
    }
  },
  "type": "object",
  "properties": {
    "metadata": {
      "type": "object",
      "properties": {
        "name": { "type": "string", "description": "Document name" }
      }
    },
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "field1": { "type": "string", "description": "First field" },
          "field2": { "type": ["number", "null"], "description": "Second field" }
        }
      }
    }
  }
}
```

Save it to `schemas/personal/my_schema.json`. The `schemas/personal/` directory is the recommended location for your project-specific schemas.

### 2. Create Config

Copy the template and customize:

```bash
cp config/TEMPLATE.yaml config/my_domain/run.yaml
```

Minimal config for testing:

```yaml
domain: my_domain

extraction:
  schema: schemas/personal/my_schema.json
  input_dir: documents/my_domain
  output_dir: extracted/my_domain

compilation:
  schema: schemas/personal/my_schema.json
  input_dir: extracted/my_domain
  output_dir: compiled/my_domain
  deduplication:
    key_fields: [field1, field2]
```

### 3. Extract and Compile

```bash
# Put test documents in documents/my_domain/

# Schema-only quick test (no config needed, good for iteration)
pixi run psweep extract documents/my_domain/ \
  --schema schemas/personal/my_schema.json -n 2

# Full run via config (recommended — picks up all settings)
pixi run psweep extract --config config/my_domain/run.yaml
pixi run psweep compile --config config/my_domain/run.yaml
```

---

## Large Documents

For documents over 200 pages, configure **page targeting** to automatically find relevant sections:

```yaml
extraction:
  schema: schemas/personal/my_schema.json
  pages:
    csv: config/domain/page_ranges.csv   # manual ranges (win over auto)
    auto_locate:                          # LLM fallback for uncovered docs
      section_description: "the section containing rate schedules"
      trigger_chars: 200000              # only for docs exceeding this size
      max_selected_pages: 30
```

- Use `pages.csv` alone for fully manual ranges
- Use `pages.auto_locate` alone for fully automatic targeting
- Use both: CSV entries win, auto-locate fills in uncovered large docs
- `--pages 615-759` — CLI override for a single file
- `max_context: 1400000` — brute force (expensive)

---

## QA/QC Multi-Model Validation

Run 2+ models on the same documents to compare outputs:

```bash
pixi run psweep extract --config config/my_domain/run.yaml --enable-qa-qc
pixi run psweep compare --config config/my_domain/run.yaml
```

Configure in the `qaqc:` section of your config:

```yaml
qaqc:
  default_lane: quantitative
  lanes:
    quantitative:
      enabled: true
      record_matching:
        key_fields: [field1, field2]
      comparison:
        primary_fields: [value, unit]
```

---

## Multi-Source Synthesis

When extracting from many documents about the same entities (news articles, filings, reports for the same set of companies or sites), the **synthesis** stage reconciles multiple per-document records into one authoritative row per entity — resolving conflicts, tracking confidence, and collecting citation URLs.

Configure in the `compilation.synthesis:` block:

```yaml
compilation:
  schema: schemas/personal/my_schema.json
  input_dir: extracted/my_domain
  output_dir: compiled/my_domain
  synthesis:
    enabled: true
    min_sources_for_llm: 2          # skip LLM call when only 1 source
    group_by: ["entity.name"]       # one output row per distinct entity
    identity_fields:
      - entity.category
      - entity.location
    citation_field: source_provenance.citation_url
    reconcile_fields:
      - {field: status}
      - {field: date_announced, evidence: evidence_announced}
      - {field: value, evidence: value_evidence}
```

Synthesis runs automatically on `compile` when enabled. It uses the same LLM configured in your environment (override with `synthesis.model:` for a specific stage).

---

## Project Structure

```
parsesweep/
├── config/                    # Domain configs (one per domain)
│   ├── TEMPLATE.yaml          # Annotated template — copy this to start
│   ├── utility_rate_tariffs/
│   │   ├── run.yaml
│   │   └── page_ranges.csv    # optional manual page ranges
│   └── datacenter_timelines/
│       └── run.yaml
├── schemas/
│   ├── personal/              # Your extraction schemas (JSON) — put new schemas here
│   └── example_utility_rate_schema.json
├── documents/                 # Input documents (created by you)
├── extracted/                 # JSON extraction output (generated)
├── compiled/                  # Final Excel/CSV output (generated)
├── discovered/                # Web discovery output (generated by discover)
└── src/psweep/                # Source code
    ├── cli/                   # CLI commands
    ├── discovery/             # Web document discovery
    ├── extraction/            # Document extraction
    ├── compilation/           # Data compilation + synthesis
    ├── qa_qc/                 # QA/QC comparison
    └── utils/                 # Shared utilities
```

---

## Configuration Reference

See `config/TEMPLATE.yaml` for a fully annotated config template.

### Config Sections

| Section | Used By | Required |
|---------|---------|----------|
| `extraction:` | `extract` | Yes (needs `schema`) |
| `compilation:` | `compile` | Yes (needs `schema`) |
| `discovery:` | `discover` | Only for web discovery |
| `qaqc:` | `compare`, `extract --enable-qa-qc` | Only for QA/QC |
| `domain:` | All | Recommended |
| `models:` | All | Optional (env default) |

### Key Extraction Settings

```yaml
extraction:
  schema: path/to/schema.json      # required
  input_dir: path/to/documents     # required (or pass as CLI argument)
  output_dir: extracted/domain     # default: extracted/<domain>
  max_context: 600000              # chars per document
  pages:                           # page selection for large PDFs
    csv: path/to/ranges.csv        # manual ranges (win over auto)
    auto_locate:                   # LLM fallback for uncovered docs
      section_description: "..."
      trigger_chars: 200000
      max_selected_pages: 30
```

### Key Compilation Settings

```yaml
compilation:
  schema: path/to/schema.json      # required
  input_dir: extracted/domain      # required (or pass as CLI argument)
  output_dir: compiled/domain      # default: compiled/<domain>
  deduplication:
    key_fields: [f1, f2, f3]       # what makes a row unique
    ignore_fields: [notes]         # excluded from comparison
  normalization:
    state_column: State            # auto-abbreviate state names
  output:
    default_format: excel          # excel, csv, or both
    column_order: [f1, f2, ...]    # custom column ordering
    column_renames: {Old: new}     # rename columns
    exclude_fields: [internal_f]   # hide columns
    freeze_columns: 2              # freeze in Excel
    auto_width: true               # auto-size columns
  synthesis:                       # optional — multi-source reconciliation
    enabled: true
    group_by: ["entity.key_field"]
    reconcile_fields:
      - {field: some_field}
```

### Model Overrides (Optional)

```yaml
models:
  primary: gpt-4o                  # model for extraction
  secondary: gpt-4o-mini           # model for cheaper tasks (e.g. page targeting)
```

---

## Supported Formats

PDF, DOCX, DOC, TXT, XLSX, CSV

---

## Environment Setup

```bash
# .env file (created by `psweep init`)
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_OPENAI_MODEL=your-deployment

# Or OpenAI
OPENAI_API_KEY=sk-your-key
```

---

## Troubleshooting

**"No documents found"** — Check file extensions are supported and path exists.

**"Schema missing $metadata"** — Schema needs `$metadata.extraction.main_data_array` and `identifier_fields`.

**Large document timeout** — Add `pages.auto_locate` in your config or increase `max_context`.

**Rate limit errors** — Azure API throttling. Wait and retry, or reduce batch size with `-n`.

---

## Development

```bash
pixi run python -m pytest tests/   # run tests
pixi run psweep check-schema schemas/personal/my_schema.json  # validate schema
pixi run psweep extract --config ... --validate-config  # dry-run validation
```

---

## License

MIT License. See [LICENSE](LICENSE).
