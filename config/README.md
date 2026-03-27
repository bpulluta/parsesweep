# Configuration Files

This directory contains runtime configuration files for acquisition, processing,
and consolidation, organized by domain/category.

## Structure

```
config/
├── generator_manuals/
│   └── run.yaml             # Acquisition + processing + consolidation config
├── tariffs/
│   ├── run.yaml             # Optional unified runtime config
│   └── page_ranges.csv      # Page range specifications for tariff documents
└── solar/
    └── page_ranges.csv
```

---

## Runtime Config Overview

All three pipeline commands (`acquire`, `process`, `consolidate`) accept a `--config` flag pointing at a YAML file. Sections not relevant to a command are ignored.

Supported validation flags (all commands):
- `--validate-config`: validate resolved inputs and exit — no processing runs.
- `--show-effective-config`: print resolved values with source attribution and continue.
- `--config-strict`: fail on unknown keys in runtime config sections.

```bash
# Validate and preview effective config for any command
pixi run streamline-extract acquire --config config/generator_manuals/run.yaml --validate-config
pixi run streamline-extract process --config config/tariffs/run.yaml --show-effective-config
pixi run streamline-extract consolidate --config config/geothermal_ordinances/run.yaml --validate-config
```

---

## Acquisition Config Reference

The `acquisition` section drives the `acquire` command.

### Top-Level Keys

| Key | Type | Description |
|-----|------|-------------|
| `topology.mode` | string | `distributed`, `centralized`, or `hybrid` |
| `targets` | list | Per-target discovery inputs (see below) |
| `query_families` | dict | Named lists of query templates |
| `seeker` | dict | Search/prioritization settings |
| `runtime` | dict | Rate limiting, concurrency, and policy settings |

### `targets[]` or `targets_csv`

Each target defines one document-discovery run. Free-form fields are available as template variables for query rendering.

**Option 1: Inline targets (small number of targets)**
```yaml
acquisition:
  targets:
    - manufacturer: Generac           # used in query template as {manufacturer}
      power_class_kw: "200-300"       # used as {power_class_kw}
      query: "Generac 250kW generator manual pdf"   # explicit query (optional)
    - manufacturer: John Deere
      power_class_kw: "200-300"

    # Jurisdiction-style target (geothermal, ordinances, etc.)
    - id: chaffee_county_co
      jurisdiction: Chaffee County
      state: CO
      topic: geothermal ordinance
      seeds:
        - https://www.chaffeecounty.org/departments/land_use_code.php
```

**Option 2: CSV file (hundreds or thousands of targets)**
```yaml
acquisition:
  targets_csv: targets.csv   # Load targets from CSV in same directory
  query_families:
    generator_similar_power:
      - "{manufacturer} {power_class_kw} kW generator spec pdf"
```

**targets.csv format** (one header row, each row = one target):
```csv
manufacturer,power_class_kw
Generac,200-300
John Deere,50-100
Caterpillar,100-200
Cummins,150-250
```

**CSV features:**
- Column names become template variables for `query_families`
- Empty cells → `None` values (works with optional template placeholders `{var?}`)
- Relative paths resolve relative to config directory
- Can mix CSV targets with inline targets (CSV loaded first, inline takes precedence if duplicated)

**Why CSV targets?**
- Inline targets: 10-20 targets → readable and maintainable in YAML
- CSV targets: 100+ targets → scales without manual YAML repetition
- Single query_family template → automatically applied to all CSV rows
- All CSV columns available as template variables → no code changes needed for new domains

### `query_families`

Named lists of query templates. Use `{field}` placeholders that are filled from `targets[].` fields at runtime.

```yaml
acquisition:
  query_families:
    generator_similar_power:
      - "{manufacturer} {power_class_kw} kW generator spec pdf"
      - "{manufacturer} generator {power_class_kw} kW manual pdf"
    geothermal_generic:
      - "{jurisdiction} {state} geothermal ordinance pdf"
      - "{jurisdiction} {state} geothermal land use code"
```

### `seeker` Settings

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `provider` | string | `serpapi` | Search provider. Only `serpapi` is currently supported. |
| `max_results` | int | `10` | Max search results per query. |
| `use_query_family` | string | — | Name of query family to render against each target. |
| `fallback_to_seed_only` | bool | `true` | Fall back to seed-only if SerpApi is unavailable. |
| `link_prioritization_mode` | string | `heuristic` | Candidate ranking mode: `heuristic` or `off`. |
| `link_top_k` | int | `5` | Number of top-ranked candidates to surface for download. |
| `link_prioritization_keywords` | list | — | Domain-specific keywords that boost matching URLs. |
| `link_prioritization_domain_scores` | dict | — | Extra `{domain_substring: score}` authority overrides. |
| `power_range_kw` | list | — | `[min_kw, max_kw]` — bonus for URLs with in-range kW values. |

#### Link Prioritization explained

When `link_prioritization_mode: heuristic` (the default), every candidate returned by SerpApi is scored before download using four signals:

| Signal | Weight | What it measures |
|--------|--------|-----------------|
| `file_type` | 35% | URL extension: PDF/DOCX/XLSX = positive; shopping/image = negative |
| `domain_authority` | 25% | Manufacturer / government domains = positive; forums / shopping = negative |
| `keyword` | 25% | Presence of `link_prioritization_keywords` + built-in doc-path keywords in URL and anchor |
| `power_class` | 15% | Numeric kW/kVA value in URL: in-range = +0.15; out-of-range = -0.10 |

`link_top_k` controls how many ranked candidates proceed to download. All candidates are preserved in `manifest.lineage.link_prioritization` for audit. Set `link_prioritization_mode: off` to disable ranking and pass all raw candidates through.

```yaml
# Example: generator domain with power-range filtering
acquisition:
  seeker:
    provider: serpapi
    max_results: 6
    use_query_family: generator_similar_power
    link_prioritization_mode: heuristic
    link_top_k: 5
    link_prioritization_keywords:
      - manual
      - operator
      - installation
      - spec
    power_range_kw: [200, 300]
```

### `selection` Settings

The `selection` section is the main domain-agnostic policy surface for deciding which discovered candidates survive before download. This is the preferred scaling mechanism instead of adding built-in bundle names tied to specific domains.

Design rule:
- Keep policy generic and config-driven.
- Do not depend on hardcoded preset families like `legal_by_jurisdiction`, `tariff_by_utility`, or `manual_by_manufacturer` inside the product runtime.
- Express domain differences through per-domain config values, target fields, query families, and selection templates.

| Key | Type | Description |
|-----|------|-------------|
| `primary_per_target` | int | Number of candidates to keep per target before global ranking. |
| `exclude_draft` | bool | Drop draft-like candidates before prioritization. |
| `draft_patterns` | list | Terms that indicate draft or non-final documents. |
| `relevance_require_any_terms` | list | Candidate must match at least one of these terms. |
| `relevance_require_legal_marker_terms` | list | Extra marker terms for domains that need legal/code signals. |
| `relevance_exclude_any_terms` | list | Terms that disqualify candidates. |
| `relevance_allowed_domain_patterns` | list | Generic host-pattern filter for trustworthy sources. |
| `require_supported_document` | bool | Require a currently supported document extension/type. |
| `target_identity_require_any_templates` | list | Template-driven identity hints. Candidate must match at least one rendered token. |
| `target_identity_require_all_templates` | list | Template-driven identity hints. Candidate must match all rendered tokens. |
| `target_identity_exclude_any_templates` | list | Template-driven negative identity hints. |

Example: the same generic mechanism can support very different domains without code branching.

```yaml
acquisition:
  selection:
    primary_per_target: 2
    exclude_draft: true
    draft_patterns: ["draft", "proposed"]
    relevance_require_any_terms: ["ordinance", "manual", "tariff", "specification"]
    relevance_exclude_any_terms: ["summary", "presentation", "press release"]
    relevance_allowed_domain_patterns: [".gov", ".us", "manufacturer.com", "codepublishing.com"]
    require_supported_document: true
    target_identity_require_any_templates:
      - "{jurisdiction}"
      - "{manufacturer}"
    target_identity_require_all_templates:
      - "{state}"
```

This keeps the runtime extensible across any domain while leaving the actual policy choices in the domain config, where they belong.

### `runtime` Settings

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `min_request_interval_ms` | int | `0` | Minimum delay between outbound requests (ms). |
| `max_concurrent_downloads` | int | `2` | Max parallel file downloads. |
| `retry_max_attempts` | int | `3` | Max retry attempts for transient failures. |
| `retry_initial_backoff_seconds` | float | `1.0` | Initial retry backoff. |
| `retry_max_backoff_seconds` | float | `8.0` | Max retry backoff cap. |
| `robots_policy_mode` | string | `ignore` | `ignore`, `warn`, or `enforce` robots.txt rules. |
| `tos_policy_mode` | string | `ignore` | `ignore`, `warn`, or `enforce` ToS acknowledgement. |

### SerpApi Environment Variables

| Variable | Description |
|----------|-------------|
| `SERPAPI_API_KEY` | SerpApi key (primary). |
| `SERPAPI_KEY` | SerpApi key (fallback). |
| `SERPAPI_SSL_VERIFY` | Set to `false` to disable SerpApi SSL verification (TLS interception environments). |
| `ACQUISITION_SSL_VERIFY` | Set to `false` to disable download SSL verification. |

### Acquisition Outputs

Each non-dry run writes to deterministic, run-scoped paths:

```text
documents/<domain>/acquired/runs/<run_id>/
    by_jurisdiction/<state>/<jurisdiction>/   # partition_mode=jurisdiction
    by_host/<source-host>/                    # partition_mode=host (default fallback)

output/acquisition/<domain>/runs/<run_id>/
    manifest.json       # full run record
    download_index.csv  # machine-readable file list
```

**`manifest.json` observability sections:**
- `timing`: `started_at`, `completed_at`, `elapsed_seconds`
- `stage_summaries.seeker`: candidates discovered, after prioritization
- `stage_summaries.routing`: mode, applied, candidates-in/out
- `stage_summaries.downloads`: downloaded, skipped, failed, total_bytes
- `candidate_summary`: total, by acceptance class (accepted/needs_review/rejected), by source
- `lineage.link_prioritization.candidates`: full ranked list with per-candidate heuristic scores

**`download_index.csv` columns:** `run_id`, `domain`, `partition_mode`, `source_state`, `source_jurisdiction`, `source_host`, `status`, `url`, `final_url`, `mime_type`, `bytes`, `relative_path`, `path`, `error`

### Policy Design Decision

Acquisition should remain domain-agnostic at the product level.

That means:
- prefer domain config values over built-in preset bundle names
- prefer reusable template variables and selection filters over domain-coded branches
- add new runtime knobs only when they are generic enough to help multiple domains

If a domain needs different discovery behavior, encode it in that domain's `run.yaml` rather than asking the runtime to grow a new hardcoded bundle family.

---

## Processing Config Reference

### Variable Categories

| Category | Keys |
|----------|------|
| Required | `input_dir`, `schema` |
| Optional | `output_dir`, `pages_csv`, `pages`, `profile`, `provider`, `model`, `limit` |
| Advanced | `max_context`, `skip_existing`, `enable_qaqc`, `qaqc_lane`, `live_dashboard` |

---

## Consolidation Config Reference

| Category | Keys |
|----------|------|
| Required | `input_dir`, `schema` |
| Optional | `output_dir`, `dry_run`, `report_format` |
| Advanced | `fail_on_suspicious` |

---

## Full `run.yaml` Template

```yaml
domain: generator_manuals

acquisition:
  topology:
    mode: distributed           # distributed | centralized | hybrid
  targets:
    - manufacturer: Generac
      power_class_kw: "200-300"
      query: "Generac industrial generator manual pdf"
  query_families:
    generator_similar_power:
      - "{manufacturer} {power_class_kw} kW generator spec pdf"
      - "{manufacturer} generator {power_class_kw} kW manual pdf"
  seeker:
    provider: serpapi
    use_query_family: generator_similar_power
    max_results: 6
    link_prioritization_mode: heuristic
    link_top_k: 5
    link_prioritization_keywords:
      - manual
      - operator
      - installation
    power_range_kw: [200, 300]
  runtime:
    min_request_interval_ms: 200
    max_concurrent_downloads: 2
    robots_policy_mode: ignore
    tos_policy_mode: ignore

processing:
  input_dir: documents/generator_manuals
  schema: schemas/personal/generator_manuals_schema.json
  output_dir: processed/generator_manuals

consolidation:
  input_dir: processed/generator_manuals
  schema: schemas/personal/generator_manuals_schema.json
  output_dir: consolidated/generator_manuals
```

---

## Page Ranges CSV Format

Page range CSV files specify which pages to extract from specific documents:

```csv
file_path,start_page,end_page
document1.pdf,615,650
document2.pdf,100,200
full_document.pdf,,
```

- **file_path**: Filename or full path.
- **start_page**: First page to extract (1-indexed, inclusive).
- **end_page**: Last page to extract (1-indexed, inclusive).
- Leave both empty to process the full document.

```bash
pixi run streamline-extract process documents/tariffs/ \
  --pages-csv config/tariffs/page_ranges.csv
```

---

## Why config/ Instead of documents/?

- **Separation of concerns**: config and input data are distinct.
- **Scalability**: each domain can have multiple config files without cluttering documents/.
- **Version control**: config changes are tracked separately from large document files.


## Runtime Config (Gate 0)

You can now configure `process` and `consolidate` with a runtime config file.

Supported commands:
- `pixi run streamline-extract process --config config/<domain>/run.yaml --validate-config`
- `pixi run streamline-extract process --config config/<domain>/run.yaml --show-effective-config`
- `pixi run streamline-extract consolidate --config config/<domain>/run.yaml --validate-config`

Validation controls:
- `--validate-config`: validate resolved inputs and exit.
- `--show-effective-config`: show resolved values with source attribution.
- `--config-strict`: fail on unknown keys in runtime config sections.

Current supported runtime sections:
- `processing`
- `consolidation`
- `acquisition` (active for `acquire` command)

### Acquisition and SerpApi Notes

For web acquisition runs that use `--enable-serpapi`, set one of:
- `SERPAPI_API_KEY=...`
- `SERPAPI_KEY=...` (fallback supported)

If your environment uses TLS interception or custom cert chains and SerpApi SSL verification fails, disable verification explicitly:
- `SERPAPI_SSL_VERIFY=false`

You can set this in your shell for a single run:

```bash
SERPAPI_SSL_VERIFY=false pixi run streamline-extract acquire ... --enable-serpapi
```

Or place it in `.env` for recurring local runs.

For non-dry acquisition downloads, TLS verification is controlled separately:
- `ACQUISITION_SSL_VERIFY=false`

Example download run with SSL disabled:

```bash
ACQUISITION_SSL_VERIFY=false pixi run streamline-extract acquire --domain <domain> --seed-url <url>
```

### Acquisition Output Organization (Scalable Layout)

For non-dry `acquire` runs, outputs are run-scoped and partitioned by strategy:

```text
documents/<domain>/acquired/runs/<run_id>/
    by_jurisdiction/<state>/<jurisdiction>/...   # when partition mode resolves to jurisdiction
    by_host/<source-host>/...                    # host fallback/default for generic web sources

output/acquisition/<domain>/runs/<run_id>/manifest.json
output/acquisition/<domain>/runs/<run_id>/download_index.csv
```

Manifest download records include:
- `partition_mode`: `jurisdiction` or `host`
- `source_host`: normalized host partition key
- `source_state`: normalized state key when jurisdiction mode is used
- `source_jurisdiction`: normalized jurisdiction key when jurisdiction mode is used
- `relative_path`: path relative to run documents directory
- `path`: full saved path in workspace
- `status`: `downloaded`, `skipped_unsupported_type`, `failed`, etc.

This layout keeps large cross-state, cross-jurisdiction, and cross-website collections organized without domain-specific hardcoding.

### Run-Level Download Index

Each non-dry `acquire` run writes `download_index.csv` next to the manifest.
This file is intended as a machine-readable handoff for downstream batch processing.

Key columns include:
- `run_id`, `domain`
- `partition_mode`
- `source_state`, `source_jurisdiction`, `source_host`
- `status` (`downloaded`, `failed`, `skipped_unsupported_type`, ...)
- `url`, `final_url`, `mime_type`, `bytes`
- `relative_path`, `path`
- `error`

For large multi-state pipelines, prefer ingesting `download_index.csv` rather than walking filesystem trees.

Recommended modes:
- Jurisdiction-centric workloads (for example geothermal ordinances): use `--partition-mode jurisdiction` with `--state` and `--jurisdiction` when known.
- Mixed/social/web-scrape workloads (for example Reddit/X/forums): use `--partition-mode host`.
- General default: `--partition-mode auto` (uses jurisdiction when hints are available, else host).

### Example `run.yaml`

```yaml
domain: geothermal_ordinances

processing:
    input_dir: documents/geothermal_ordinances
    schema: schemas/personal/geothermal_ordinance_schema.json
    output_dir: processed/geothermal_ordinances
    pages_csv: config/geothermal_ordinances/page_ranges.csv
    max_context: 400000

consolidation:
    input_dir: processed/geothermal_ordinances
    schema: schemas/personal/geothermal_ordinance_schema.json
    output_dir: consolidated/geothermal_ordinances
    dry_run: false
```

### Variable Categories

Processing:
- Required: `input_dir`, `schema`
- Optional: `output_dir`, `pages_csv`, `pages`, `profile`, `provider`, `model`, `limit`
- Advanced: `max_context`, `skip_existing`, `enable_qaqc`, `qaqc_lane`, `live_dashboard`

Consolidation:
- Required: `input_dir`, `schema`
- Optional: `output_dir`, `dry_run`, `report_format`
- Advanced: `fail_on_suspicious`

## Page Ranges CSV Format

Page range CSV files specify which pages to extract from specific documents:

```csv
file_path,start_page,end_page
document1.pdf,615,650
document2.pdf,100,200
full_document.pdf,,
```

- **file_path**: Name of the document file (can be filename only or full path)
- **start_page**: First page to extract (1-indexed, inclusive)
- **end_page**: Last page to extract (1-indexed, inclusive)
- Leave both start_page and end_page empty to process the full document

## Usage

Specify page ranges CSV when processing documents:

```bash
pixi run streamline-extract process documents/tariffs/ --pages-csv config/tariffs/page_ranges.csv
```

## Why config/ Instead of documents/?

Configuration files are separated from input documents because:
- **Clear separation of concerns**: Config vs. data
- **Scalability**: Each category can have multiple config files without cluttering documents/
- **Maintainability**: Easy to find and update configurations
- **Version control**: Easier to track config changes separately from large document files
