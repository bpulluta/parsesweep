# Configuration Files

This directory contains runtime configuration files for discovery, extraction,
and compilation, organized by domain/category.

## Start with discovery

Copy the short discovery-only template and edit its domain and query:

```bash
mkdir -p config/my_domain
cp config/TEMPLATE.yaml config/my_domain/run.yaml
```

```yaml
domain: my_domain
discovery:
  queries:
    - "industrial pump installation manual filetype:pdf"
```

Set `SERPAPI_API_KEY` in your environment or `.env` — never in YAML. No schema
or LLM credential is needed unless you add extraction or paid LLM review.
Queries auto-enable SerpApi when search is omitted. Explicit
`search.enabled: false` wins. Replace `queries` with
`seeds` for known document URLs; seed-only input does not auto-enable search.

```bash
pixi run psweep discover --config config/my_domain/run.yaml --validate-config
pixi run psweep discover --config config/my_domain/run.yaml
```

The default is 10 unique candidates per query and 1 acquisition per target,
with no crawl, concurrency 2, retry attempts 3, and robots/ToS policy modes
`warn`. Search cache, browser mode, browser escalation, code-host adapters, and
LLM document review are off. Inspect the documents before adding stages:
`discovered/my_domain/runs/<run_id>/curated` holds one run's selected files;
`discovered/my_domain/curated` is the consolidated downstream folder.

[TEMPLATE.yaml](TEMPLATE.yaml) keeps discovery active and later extraction and
compilation commented out. [ADVANCED.yaml](ADVANCED.yaml) preserves the expanded
stage examples and annotations. Copy only settings you need, replacing model
and schema placeholders before running those stages. The existing
`init-domain-schema` command scaffolds an extraction schema, not a discovery
wizard; no new setup command or config knob is needed. Keep project-specific
schemas under the local `schemas/personal/` directory.

## Structure

```text
config/
├── TEMPLATE.yaml                    # Minimal discovery-only starting point
├── ADVANCED.yaml                    # Expanded stage examples and annotations
└── my_domain/                       # Create locally for your domain
    ├── run.yaml
    ├── targets.csv                  # optional authored target rows
    └── page_ranges.csv              # optional manual extraction page ranges
```

---

## Runtime Config Overview

All three pipeline commands (`discover`, `extract`, `compile`) accept a `--config`
flag pointing at a YAML file. All supplied sections are validated when loaded;
a command executes only its own section. Only that explicit file is loaded.
Adjacent section overrides beside `run.yaml`, `run.yml`, or `run.json` are
retired. Move their `discovery`/`extraction`/`compilation`/`validation` sections
into the selected config and remove the adjacent files; the loader reports
which files need migration. Explicit section-wrapped configs remain supported.

Discovery rejects unknown nested options, quoted booleans, invalid regexes,
and invalid limits before execution, even without `--config-strict`.
`pipeline`, `scoring`, `dedupe`, `file_filters`, and `keyword_validation` were
unused discovery blocks and are no longer accepted. Use supported selection,
ranking, discovery rules, and document-classifier settings instead.
Canonical discovery YAML uses `search`, `search.max_results`, and selection
`max_per_target`/`require_any`/`exclude`. Retired `seeker`,
`max_results_per_query`, `primary_per_target`, `relevance_require_any_terms`, and
`relevance_exclude_any_terms` fail with the replacement key in the error,
even when canonical keys coexist.
`discover --validate-config` checks the same normalized input plan as execution,
without network calls or output writes; it does not check live credentials,
provider availability, or network reliability.

Supported validation flags (all commands):
- `--validate-config`: validate resolved inputs and exit — no stage execution.
- `--show-effective-config`: print resolved values with source attribution and continue.
- `--config-strict`: fail on unknown keys in runtime config sections.

```bash
# Validate and preview effective config for any command
pixi run psweep discover --config config/my_domain/run.yaml --validate-config
pixi run psweep extract --config config/my_domain/run.yaml --show-effective-config
pixi run psweep compile --config config/my_domain/run.yaml --validate-config
```

---

## Discovery Config Reference

The `discovery` section drives the `discover` command.

### Search budget and source scope

`search.max_results` defaults to `10` and caps unique candidates **per query**
locally, across live/cached results and enabled news results. Provider `num`
does not override the cap. There is no hidden pagination or refill.
`selection.max_per_target` defaults to `1` and controls acquisition per target,
not semantic curation. These limits do not guarantee successful downloads or
recall. Search caching is opt-in with `search.cache: true`; a cache hit makes
zero live search calls.

Use a host allowlist **and** URL-path regex for strict sources:

```yaml
discovery:
  queries:
    - "site:example.org installation manual filetype:pdf"
  allowed_domains: [example.org]
  discovery_rules:
    include_url_patterns: ['^https?://(?:www\.)?example\.org/documents/']
```

Scope applies before top-one selection and to final redirects. Search `site:`
and `filetype:` hints are not enforcement. Concrete provider/vendor source
filters belong in each domain config; parsers do not add them automatically.
Soft domain ranking boosts do not replace an allowlist.

PDF candidates are preferred to web pages when present. HTML/HTM and
extensionless HTTP(S) URLs remain provisionally eligible with the default
`require_supported_document`; known non-document binaries are excluded and
GET/MIME checks verify ambiguous candidates. No `require_supported_document:
false` workaround is needed for web documents. Eligibility does not certify
semantic relevance.

### Top-Level Keys

| Key | Type | Description |
|-----|------|-------------|
| `queries` | list | Simple queries or templates; enables SerpApi when search is omitted. |
| `seeds` | list | Known URLs; seed-only input does not auto-enable search. |
| `search` | dict | Provider, per-query candidate budget, opt-in cache, and provider parameters. |
| `allowed_domains` | list | Hard host scope; combine with URL-path rules when needed. |
| `discovery_rules` | dict | `include_url_patterns` and `include_link_text_patterns` regexes. |
| `follow_links` | bool | Opt into crawling; omitted means direct acquisition. |
| `topology.mode` | string | Advanced topology: `distributed`, `centralized`, or `hybrid`. |
| `targets` | list/object | Per-target discovery inputs or a generated target source. |
| `targets_csv` | string | Authored target rows; path relative to config directory. |
| `query_families` | dict | Named lists of query templates. |
| `runtime` | dict | Crawl budgets, download concurrency, and request pacing. |
| `policy` | dict | Robots/ToS modes and acknowledged domains. |
| `retry_policy` | dict | Attempts and backoff limits. |
| `request_headers` | dict | Public, non-empty HTTP header names and values; no credentials. |

### `targets[]` or `targets_csv`

Each target supplies document-discovery inputs. Free-form fields are available
as template variables for query rendering. Supply `state` and `jurisdiction`
explicitly for geographic partitioning, or use `partition_by` for your own
metadata layout. Discovery never infers geography from query text and has no
built-in US state aliases; `jurisdiction_aliases` contains only your configured
aliases.

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
  search:
    provider: serpapi
    use_query_family: generator_similar_power
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
- Can mix CSV targets with inline targets (CSV loaded first)

**Why CSV targets?**
- Inline targets keep small target sets readable in YAML
- CSV avoids repeating target metadata in YAML for larger sets
- A query-family template can be applied to all CSV rows
- All CSV columns are available as template variables — no code changes needed

### `query_families`

Named lists of query templates. Use `{field}` placeholders filled from target fields at runtime; select a family with `search.use_query_family`.

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

### `search` Settings

`search:` is the canonical block for provider, query-family selection, result
limits, cache, and API parameters. `seeker:` is retired and fails validation
with migration guidance; it is not merged with `search`. Explicit
`search.enabled: false` overrides inferred search and top-level
`enable_serpapi`; explicit CLI overrides win last.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `provider` | string | `serpapi` | Only `serpapi` is currently supported. |
| `max_results` | int | `10` | Local unique-candidate cap per query, including cached/news results. |
| `use_query_family` | string | — | Name of query family to render against each target. |
| `enabled` | bool | Inferred from query/provider configuration | Explicitly disable/enable search; not a string. |

Configure candidate ranking via `link_prioritization:` (`mode`, `top_k`,
`keywords`, `domain_scores`), not unsupported keys inside `search`.

#### Link Prioritization explained

When `link_prioritization.mode: heuristic` (the default), candidates are
scored before download using these signals:

| Signal | Weight | What it measures |
|--------|--------|-----------------|
| `file_type` | 35% | Document extensions contribute positive scores; known image/binary extensions contribute negative scores. Shopping-path penalties apply only when configured. |
| `domain_authority` | 25% | No built-in vendor, government, education, forum, or marketplace authority table. Configure `link_prioritization.domain_scores`; `selection.relevance_allowed_domain_patterns` adds a separate soft selection boost. |
| `keyword` | 25% | Configured keywords match URL/title/snippet/anchor evidence and replace the generic fallback (`document`, `download`, `guide`, `report`). |

PDF candidates are preferred to web candidates when present. Ranking is
heuristic, not semantic review. `link_prioritization.top_k: 0` (the default)
adds no global ranking cap; `selection.max_per_target` controls per-target
acquisition. Set `top_k` > 0 only for a deliberate global cap. Ranked
candidate lineage is recorded in the manifest. Set `mode: off` to disable
ranking.

```yaml
# Example: generator domain with configured relevance boosts
discovery:
  search:
    provider: serpapi
    max_results: 6
    use_query_family: generator_similar_power
  link_prioritization:                 # own block, not under search
    mode: heuristic
    keywords: [manual, operator, installation, spec]
    domain_scores: {"generac.com": 0.2, "cummins.com": 0.2}
    # top_k: 5                         # optional global cap; omit for no cap
```

### `selection` Settings

The `selection` section is the domain-agnostic policy surface for deciding which discovered candidates survive before download.

Design rule:
- Keep policy generic and config-driven.
- Do not depend on hardcoded preset families like `legal_by_jurisdiction`, `tariff_by_utility`, or `manual_by_manufacturer` inside the product runtime.
- Express domain differences through per-domain config values, target fields, query families, and selection templates.

| Key | Type | Description |
|-----|------|-------------|
| `max_per_target` | int | Acquisition count per target; default `1`, not the LLM review retention count. |
| `max_per_host_per_target` | int | Optional host-diversity cap per target; `0` disables it. |
| `exclude_draft` | bool | Apply configured draft-pattern exclusions before prioritization. |
| `draft_patterns` | list | Domain-authored draft/non-final regexes; omitted means no built-in legal draft list. |
| `require_any` | list | Candidate must match at least one of these terms. |
| `relevance_require_legal_marker_terms` | list | Extra marker terms for domains that need legal/code signals. |
| `exclude` | list | Terms that disqualify candidates (and the download-time URL guard). |
| `exclude_text_patterns` | list | Case-insensitive regex exclusions on candidate text evidence. |
| `exclude_url_patterns` | list | Case-insensitive regex guards on the original URL. |
| `relevance_allowed_domain_patterns` | list | Preferred host patterns; **soft ranking boost only**, not a host allowlist. |
| `require_supported_document` | bool | Default supported-document eligibility includes provisional HTML/HTM and extensionless HTTP(S) URLs; GET/MIME verifies ambiguity. |
| `target_identity_require_any_templates` | list | Candidate must match at least one rendered identity hint. |
| `target_identity_require_all_templates` | list | Candidate must match all rendered identity hints. |
| `target_identity_exclude_any_templates` | list | Template-driven negative identity hints. |

Example: generic policy supports different domains without code branching.

```yaml
discovery:
  selection:
    max_per_target: 2
    exclude_draft: true
    draft_patterns: ["draft", "proposed"]
    require_any: ["ordinance", "manual", "tariff", "specification"]
    exclude: ["summary", "presentation", "press release"]
    relevance_allowed_domain_patterns: [".gov", ".us", "manufacturer.com", "codepublishing.com"]
    require_supported_document: true
    target_identity_require_any_templates:
      - "{jurisdiction}"
      - "{manufacturer}"
    target_identity_require_all_templates:
      - "{state}"
```

Keep required identity hints consistent with the fields supplied by each target.
Start with targeted queries and add exclusions/required terms only for observed
mismatches. Use `allowed_domains` plus `discovery_rules` when source scope is a
requirement, rather than relying on ranking boosts. Optional keyword
classification or paid LLM review happens after acquisition; neither establishes
population coverage.

### Optional advanced discovery features

See [ADVANCED.yaml](ADVANCED.yaml), the [configuration reference](../docs/config-reference.md), and the existing domain configs for stage-specific examples.

| Key | Purpose |
|-----|---------|
| `targets` (object with `source:`) | Generate targets: `dataset` (one per row) or `cross_product`. |
| `query_context_aliases` | Coalesce alias → first non-empty target field for query templates. |
| `browser_mode` | Off by default; explicitly use headless Chrome downloads (requires Selenium + Chrome). |
| `browser_escalation` | Off by default; `{enabled: true}` opts into thin/JS-shell recovery. Threshold-only settings do not enable it. |
| `code_host_adapters` | Off by default; `{enabled: true}` opts into supported code-host API recovery. Threshold-only settings do not enable it. |
| `document_classifier` | Keyword check; `action: warn` (flag) or `filter`. |
| `document_review` | Explicit optional paid LLM curation; requires `document_description`, `model`, and LLM credentials. `keep_top` is separate from acquisition count. Writes an editable `review.csv` (see [Curate](../README.md#curate)). |
| `partition_by` | Output layout `by_<f1>/<v1>/<v2>/…` from target-metadata fields. |

### `runtime` Settings

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `min_request_interval_ms` | int | `0` | Minimum delay between outbound requests (ms). |
| `max_concurrent_downloads` | int | `2` | Max parallel file downloads. |
| `max_depth` | int | engine default | Crawl descent budget; `0` means no descent. Direct acquisition is the default topology. |
| `max_pages` / `max_files` | int | engine default | Positive crawl/download budgets. |
| `timeout_seconds` | int | engine default | Positive routing request timeout. |

Retry and policy controls belong in separate blocks:

```yaml
discovery:
  retry_policy:
    max_attempts: 3
    initial_backoff_seconds: 1.0
    max_backoff_seconds: 8.0
  policy:
    robots_mode: warn              # ignore, warn, or enforce
    tos_mode: warn                 # ignore, warn, or enforce
    acknowledged_tos_domains: []
  request_headers:
    User-Agent: ParseSweep/2.0
```

HTTP enforcement is available. Strict robots/ToS enforcement cannot certify
browser redirects or subresource requests, so it fails before unsupported
browser navigation (including browser recovery). Use HTTP acquisition when
enforcement is required; browser opt-in is not policy certification.

These retry defaults are shared by CLI and engine. Download concurrency defaults
to `2` in both paths. Count limits reject booleans and numeric strings; delay
values must be finite and non-negative.

### SerpApi Environment Variables

| Variable | Description |
|----------|-------------|
| `SERPAPI_API_KEY` | SerpApi key (primary); keep out of YAML. |
| `SERPAPI_KEY` | SerpApi key (fallback). |
| `SERPAPI_SSL_VERIFY` | SerpApi TLS verification; unchanged default `false`, set `true` to enable. |
| `DISCOVERY_SSL_VERIFY` | Download TLS verification; unchanged default `false`, set `true` to enable. |
| `REQUESTS_CA_BUNDLE` | Requests CA bundle path, including enterprise certificates if applicable. |

Choose verification explicitly via the environment. Other request paths can
differ; config validation neither contacts these endpoints nor tests their
certificate chains.

### Discovery Outputs

Each acquisition run has a run-scoped folder, with a `latest` pointer to the
most recent run that produced downloads and a consolidated curated folder
for downstream input:

```text
discovered/<domain>/
    checkpoint.json                 # v2 resume: effective config + artifact evidence
    latest -> runs/<run_id>         # symlink to the current run
    curated/                        # consolidated selected docs -> downstream input
    runs/<run_id>/
        manifest.json               # full run record
        download_index.csv          # machine-readable file list (+ review cols)
        review.csv                  # human-editable curation ledger
        documents/                  # all downloads from this run (not a recall guarantee)
            <partition>/<file>          # by_state_jurisdiction / by_host / by_<fields>
            <partition>/.text/<filename>.txt        # cached OCR text
            <partition>/.text/<filename>.meta.json  # source + text digest evidence
            <partition>/.review/<filename>.json     # LLM verdict + human override
        curated/                    # selected docs from this run
            <partition>/<file>
```

Use `discovered/<domain>/curated` for consolidated downstream extraction, or
`discovered/<domain>/runs/<run_id>/curated` to inspect/use one run. The
`latest/curated` pointer refers only to that latest run. Where a review ledger
is present, edit `review.csv` (`human_decision` = `keep`/`reject`) and run
`pixi run psweep curate` to rebuild curated selections.

Checkpoint v2 skips only work whose effective config fingerprint and artifact
SHA-256 evidence still match. Changed settings, missing/altered artifacts, and
v1 entries are not reusable. Corpus publication rejects concurrent writers and
recovers interrupted curated/provenance swaps from its journal on the next
writer; this is discovery-specific, not a general pipeline transaction.

OCR pairs use full filenames, so `report.pdf` and `report.html` cannot collide.
Source size/mtime and digest plus the cached-text digest must match; partial,
stale, or mismatched pairs are misses. Temporary-file replacement publishes
metadata last; no stem-only fallback exists. Curation carries OCR and full-name
review sidecars with selected files. Native extraction/OCR correction behavior
is unchanged.

Discovery exit codes are verbosity-independent: `success`, `dry_run`, and
validated `reused` work return **0**; failed or partially failed acquisition
(including failure with zero downloads) returns **1**; genuinely `empty` work
returns **2**. Inspect the manifest's outcome rather than counting files alone.

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
| Advanced | `max_context`, `skip_existing`, `live_dashboard` |

---

## Compilation Config Reference

| Category | Keys |
|----------|------|
| Required | `input_dir`, `schema` |
| Optional | `output_dir`, `dry_run`, `report_format` |
| Advanced | `fail_on_suspicious` |

---

## Adding Extraction and Compilation

After inspecting acquired documents, create your own extraction schema and add
only the needed sections to the same domain config:

```yaml
extraction:
  schema: schemas/personal/my_schema.json
  input_dir: discovered/my_domain/curated
  output_dir: extracted/my_domain
  model: your-model-name
compilation:
  schema: schemas/personal/my_schema.json
  input_dir: extracted/my_domain
  output_dir: compiled/my_domain
```

Configure LLM credentials in the environment before extraction. For local
inputs, use `documents/my_domain` instead. [ADVANCED.yaml](ADVANCED.yaml)
contains the expanded model, output, page-targeting, synthesis, and QA/QC
examples; placeholder schema paths refer to files you create locally, not
published private schemas.

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
- Auto-locate results are cached in `.pages/<name>.json` next to the file; unchanged cached inputs avoid another model call.
- On any failure, extraction falls back to the full document.

---

## Why config/ Instead of documents/?

- **Separation of concerns**: config and input data are distinct.
- **Scalability**: each domain can have multiple config files without cluttering documents/.
- **Version control**: config changes are tracked separately from large document files.

## Further Reference

Use [TEMPLATE.yaml](TEMPLATE.yaml) for a minimal discovery start and
[ADVANCED.yaml](ADVANCED.yaml) for expanded examples. The
[Getting Started guide](../docs/getting-started.md) shows the progression from
discovery to schema-driven extraction and compilation; the
[Configuration Reference](../docs/config-reference.md) renders both templates.
