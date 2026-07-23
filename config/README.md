# Configuration Files

This directory contains runtime configuration files for discovery, extraction,
and compilation, organized by domain/category.

## Structure

```
config/
├── TEMPLATE.yaml                    # Annotated template — copy this to start
├── utility_rate_tariffs/
│   ├── run.yaml
│   └── page_ranges.csv
├── datacenter_timelines/
│   └── run.yaml
├── geothermal_ordinances/
│   └── run.yaml
├── industrial_pump_datasheets/
│   └── run.yaml
├── generator_manuals/
│   └── run.yaml
├── aq_permits_va/
│   └── run.yaml
└── solar/
    └── page_ranges.csv
```

---

## Runtime Config Overview

All three pipeline commands (`discover`, `extract`, `compile`) accept a `--config` flag pointing at a YAML file. Sections not relevant to a command are ignored.

Supported validation flags (all commands):
- `--validate-config`: validate resolved inputs and exit — no extraction runs.
- `--show-effective-config`: print resolved values with source attribution and continue.
- `--config-strict`: fail on unknown keys in runtime config sections.

```bash
# Validate and preview effective config for any command
pixi run psweep discover --config config/generator_manuals/run.yaml --validate-config
pixi run psweep extract --config config/tariffs/run.yaml --show-effective-config
pixi run psweep compile --config config/geothermal_ordinances/run.yaml --validate-config
```

---

## Discovery Config Reference

The `discovery` section drives the `discover` command.

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
discovery:
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
discovery:
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
discovery:
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
| `link_top_k` | int | `0` | Global cap on ranked candidates. `0` = no cap (recommended); per-target `max_per_target` controls recall. Set > 0 only to hard-cap total downloads. |
| `link_prioritization_keywords` | list | — | Domain-specific keywords that boost matching URLs. |
| `link_prioritization_domain_scores` | dict | — | `{domain_substring: score}` soft authority boosts. |

Configure these via the `link_prioritization:` block (`mode`, `top_k`,
`keywords`, `domain_scores`) — see the example below.

#### Link Prioritization explained

When `link_prioritization.mode: heuristic` (the default), every candidate is
scored before download using these signals:

| Signal | Weight | What it measures |
|--------|--------|-----------------|
| `file_type` | 35% | URL extension: PDF/DOCX/XLSX = positive; shopping/image = negative |
| `domain_authority` | 25% | Built-in defaults: `.gov`/`.edu` = positive, forums/marketplaces = negative. Domain-specific boosts come from config (`link_prioritization.domain_scores`, or `selection.relevance_allowed_domain_patterns` as a soft boost). |
| `keyword` | 25% | Presence of `link_prioritization_keywords` + built-in doc-path keywords in URL and anchor |

Ranking sets download *order*; it no longer caps by default. `link_top_k: 0`
(the default) means per-target `selection.max_per_target` is the sole recall
control — the prior default of `5` silently dropped valid documents across
targets. Set `link_top_k` > 0 only for a deliberate global cap. All candidates
are preserved in `manifest.lineage.link_prioritization` for audit. Set
`link_prioritization_mode: off` to disable ranking entirely.

```yaml
# Example: generator domain with power-range bonus
discovery:
  seeker:
    provider: serpapi
    max_results: 6
    use_query_family: generator_similar_power
  link_prioritization:                 # own block, not under seeker
    mode: heuristic
    keywords: [manual, operator, installation, spec]
    domain_scores: {"generac.com": 0.2, "cummins.com": 0.2}
    # top_k: 5                         # optional global cap; omit for no cap
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
| `relevance_exclude_any_terms` | list | Terms that disqualify candidates (and the download-time URL guard). |
| `relevance_allowed_domain_patterns` | list | Preferred host patterns. **Soft trust boost only** (ranking) — no longer a hard filter, so off-list sources are still kept. |
| `require_supported_document` | bool | Require a currently supported document extension/type. |
| `target_identity_require_any_templates` | list | Template-driven identity hints. Candidate must match at least one rendered token. |
| `target_identity_require_all_templates` | list | Template-driven identity hints. Candidate must match all rendered tokens. |
| `target_identity_exclude_any_templates` | list | Template-driven negative identity hints. |

Example: the same generic mechanism can support very different domains without code branching.

```yaml
discovery:
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

> **Recall-first guidance:** prefer targeted `queries` + light `exclude` terms
> over long `relevance_require_any_terms`/`allowed_domain_patterns` lists — the
> latter tend to reject the real document. Move precision to
> `document_classifier` (keyword) and `document_review` (LLM), which flag or
> curate after download.

### Newer discovery keys

These are documented with examples in
[TEMPLATE.yaml](TEMPLATE.yaml); in brief:

| Key | Purpose |
|-----|---------|
| `targets` (object with `source:`) | Generate targets automatically: `dataset` (one per row) or `cross_product` (e.g. counties × doc-types). |
| `query_context_aliases` | Coalesce alias → first non-empty target field for query templates. |
| `browser_mode` | Download via headless Chrome for bot-protected sites (Akamai); needs Selenium + Chrome. |
| `document_classifier` | Cheap keyword check; `action: warn` (flag) or `filter`. |
| `document_review` | LLM grades each download and selects the primary one(s) per target into `curated/` (see [Curate](../README.md#curate-command)); writes an editable `review.csv`. |
| `partition_by` | Output layout `by_<f1>/<v1>/<v2>/…` from target-metadata fields. |

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
| `DISCOVERY_SSL_VERIFY` | Set to `false` to disable download SSL verification. |

### Discovery Outputs

Each non-dry run is one self-contained, run-scoped folder, with a `latest`
pointer to the most recent run that produced downloads:

```text
discovered/<domain>/
    checkpoint.json                 # domain-level resume state
    latest -> runs/<run_id>         # symlink to the current run
    runs/<run_id>/
        manifest.json               # full run record
        download_index.csv          # machine-readable file list (+ review cols)
        review.csv                  # human-editable curation ledger
        documents/                  # ALL downloads (recall set)
            <partition>/<file>          # by_state_jurisdiction / by_host / by_<fields>
            <partition>/.text/          # cached OCR text (scanned PDFs only)
            <partition>/.review/        # per-file LLM verdict + human override
        curated/                    # final selected docs only -> downstream input
            <partition>/<file>
```

Downstream `extract` consumes `discovered/<domain>/latest/curated`.
Edit `review.csv` (`human_decision` = `keep`/`reject`) and run
`psweep curate` to rebuild `curated/`.

**`manifest.json` observability sections:**
- `timing`: `started_at`, `completed_at`, `elapsed_seconds`
- `stage_summaries.seeker`: candidates discovered, after prioritization
- `stage_summaries.routing`: mode, applied, candidates-in/out
- `stage_summaries.downloads`: downloaded, skipped, failed, total_bytes
- `candidate_summary`: total, by acceptance class (accepted/needs_review/rejected), by source
- `lineage`: `documents_dir`, `download_index_csv`, `review_index_csv`, `curated_dir`, `curated_count`, ranked candidates

**`download_index.csv` columns:** `run_id`, `domain`, `partition_mode`, `source_state`, `source_jurisdiction`, `source_host`, `status`, `url`, `final_url`, `mime_type`, `bytes`, `relative_path`, `path`, `review_selected`, `review_is_primary`, `review_relevance`, `review_doc_kind`, `error`, `target_metadata`

**`review.csv` columns (human-editable):** `target`, `partition`, `state`, `jurisdiction`, `doc_kind`, `relevance`, `llm_is_primary`, `llm_selected`, `human_decision`, `human_notes`, `llm_reason`, `relative_path`, `path`

### Policy Design Decision

Discovery should remain domain-agnostic at the product level.

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
| Optional | `output_dir`, `pages`, `profile`, `provider`, `model`, `limit` |
| Advanced | `max_context`, `skip_existing`, `enable_qaqc`, `qaqc_lane`, `live_dashboard` |

---

## Compilation Config Reference

| Category | Keys |
|----------|------|
| Required | `input_dir`, `schema` |
| Optional | `output_dir`, `dry_run`, `report_format` |
| Advanced | `fail_on_suspicious` |

---

## Starting a New Domain

Copy `TEMPLATE.yaml` and fill in your domain name, schema path, and directories:

```bash
cp config/TEMPLATE.yaml config/my_domain/run.yaml
```

See `TEMPLATE.yaml` for the fully annotated, up-to-date config template with all available knobs for each section.

---

## Page Selection for Large Documents

Configure page selection under `extraction.pages:` in your run config. Use manual
CSV ranges, LLM-assisted auto-locate, or both (CSV entries win, auto-locate fills
in uncovered large docs).

```yaml
extraction:
  pages:
    csv: config/tariffs/page_ranges.csv    # manual ranges (win over auto)
    auto_locate:                            # LLM fallback for uncovered docs
      section_description: >-
        the residential electric rate schedules showing per-kWh energy charges,
        monthly customer charge, and other rate components
      trigger_chars: 200000                 # only activate for docs exceeding this size
      max_selected_pages: 30                # max candidate pages sent to the LLM
      # keywords: [rate schedule, residential]   # optional heuristic boosts
      # model: gpt-4o-mini                       # optional cheaper locator model
```

### Page Ranges CSV Format

```csv
file_path,start_page,end_page
document1.pdf,615,650
document2.pdf,100,200
full_document.pdf,,
```

- **file_path**: Filename or full path.
- **start_page**: First page to extract (1-indexed, inclusive).
- **end_page**: Last page to extract (1-indexed, inclusive).
- Leave both empty to extract the full document (and skip auto-locate for this file).

### CLI Overrides

```bash
# Manual CSV via CLI (no config needed)
pixi run psweep extract documents/tariffs/ \
  --pages-csv config/tariffs/page_ranges.csv

# Single file page range
pixi run psweep extract doc.pdf --pages 615-759
```

### How It Works

- `pages.csv` entries are loaded first; matched files use those ranges.
- `pages.auto_locate` runs second — only on large PDFs not covered by the CSV.
- Auto-locate results are cached in `.pages/<name>.json` next to the file; the LLM call happens once.
- On any failure, extraction falls back to the full document.

---

## Why config/ Instead of documents/?

- **Separation of concerns**: config and input data are distinct.
- **Scalability**: each domain can have multiple config files without cluttering documents/.
- **Version control**: config changes are tracked separately from large document files.


## Runtime Config (Gate 0)

You can now configure `extract` and `compile` with a runtime config file.

Supported commands:
- `pixi run psweep extract --config config/<domain>/run.yaml --validate-config`
- `pixi run psweep extract --config config/<domain>/run.yaml --show-effective-config`
- `pixi run psweep compile --config config/<domain>/run.yaml --validate-config`

Validation controls:
- `--validate-config`: validate resolved inputs and exit.
- `--show-effective-config`: show resolved values with source attribution.
- `--config-strict`: fail on unknown keys in runtime config sections.

Current supported runtime sections:
- `extraction`
- `compilation`
- `discovery` (active for `discover` command)

### Discovery and SerpApi Notes

For web discovery runs that use `--enable-serpapi`, set one of:
- `SERPAPI_API_KEY=...`
- `SERPAPI_KEY=...` (fallback supported)

If your environment uses TLS interception or custom cert chains and SerpApi SSL verification fails, disable verification explicitly:
- `SERPAPI_SSL_VERIFY=false`

You can set this in your shell for a single run:

```bash
SERPAPI_SSL_VERIFY=false pixi run psweep discover ... --enable-serpapi
```

Or place it in `.env` for recurring local runs.

For non-dry discovery downloads, TLS verification is controlled separately:
- `DISCOVERY_SSL_VERIFY=false`

Example download run with SSL disabled:

```bash
DISCOVERY_SSL_VERIFY=false pixi run psweep discover --domain <domain> --seed-url <url>
```

### Discovery Output Organization (Scalable Layout)

For non-dry `discover` runs, outputs are run-scoped and partitioned by strategy
under the single run folder (see [Discovery Outputs](#discovery-outputs)):

```text
discovered/<domain>/runs/<run_id>/
    documents/
        by_state_jurisdiction/<state>/<jurisdiction>/...  # jurisdiction partitioning
        by_host/<source-host>/...                         # host fallback/default
    curated/                                              # same layout, selected docs
    manifest.json
    download_index.csv
    review.csv
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

Each non-dry `discover` run writes `download_index.csv` next to the manifest.
This file is intended as a machine-readable handoff for downstream batch extraction.

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

See `config/geothermal_ordinances/run.yaml` for a complete, real-world example covering discovery, extraction, compilation, and QA/QC. See `TEMPLATE.yaml` for the fully annotated reference with all available knobs.

