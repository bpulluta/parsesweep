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
Search runs only when `queries` (or a target `query`) is set. For known
document URLs, replace `queries` with `sources`; configured URLs are always
downloaded and kept.

```bash
pixi run psweep discover --config config/my_domain/run.yaml --validate-config
pixi run psweep discover --config config/my_domain/run.yaml
```

The default is 10 unique candidates per query and 1 acquisition per target,
with no link following (`crawl.max_depth: 0`), concurrency 2, retry attempts 3, and robots/ToS policy modes
`warn`. Search cache, browser mode, browser escalation, code-host adapters, and
LLM document review are off. Inspect the documents before adding stages:
`outputs/my_domain/discovered/runs/<run_id>/curated` holds one run's selected files;
`outputs/my_domain/discovered/curated` is the consolidated downstream folder.

[TEMPLATE.yaml](TEMPLATE.yaml) keeps discovery active and later extraction and
compilation commented out. [ADVANCED.yaml](ADVANCED.yaml) preserves the expanded
stage examples and annotations. Copy only settings you need, replacing model
and schema placeholders before running those stages. The existing
`init-domain-schema` command scaffolds an extraction schema, not a discovery
wizard; no new setup command or config knob is needed. Keep each domain's
schema beside its config as `config/<domain>/schema.json`.

## Structure

```text
config/
├── TEMPLATE.yaml                    # Minimal discovery-only starting point
├── ADVANCED.yaml                    # Expanded stage examples and annotations
└── my_domain/                       # Create locally for your domain
    ├── run.yaml
    ├── schema.json                  # extraction schema (default for `schema:`)
    ├── targets.csv                  # optional authored target rows
    └── page_ranges.csv              # optional manual extraction page ranges

outputs/my_domain/                   # derived from `domain`; never set in YAML
├── discovered/
├── extracted/
├── validated/
└── compiled/
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

Config is always strict: unknown keys at any level, quoted booleans, invalid
regexes, URLs, and limits fail before execution, naming the file, the key path,
and the closest valid key.
`discover --validate-config` checks the same normalized input plan as execution,
without network calls or output writes; it does not check live credentials,
provider availability, or network reliability.

Supported validation flags (all commands):
- `--validate-config`: validate resolved inputs and exit — no stage execution.
- `--show-effective-config`: print resolved values with source attribution and continue.

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
  crawl:
    allowed_domains: [example.org]
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
| `targets` | path/list/object | `.csv`/`.json` path, inline rows, or `{cross_product: {dimensions: [...]}}`. |
| `sources` | list | Shared document/page URLs; always downloaded and kept, saved under `_shared/`. |
| `queries` | list | Search templates (`{field}` required, `{field?}` optional); enable search. |
| `crawl` | dict | Link following, scope, browser options, and request headers. |
| `search` | dict | Per-query candidate budget, opt-in cache, and SerpApi params. |
| `runtime` | dict | `max_files`, `timeout_seconds`, concurrency, and request pacing. |
| `selection` / `link_prioritization` | dict | Filters and ranking for search results and crawled links. |
| `document_classifier` / `document_review` | dict | Post-download keyword check and optional LLM curation. |
| `policy` / `retry_policy` | dict | Robots/ToS modes and retry limits. |
| `partition_by` / `retention` | list/dict | Output layout and kept-document policy. |

### `targets`

Each row is one target. Every column except `urls` and `query` is target
metadata: it fills query templates, partitions output (`partition_by`), and is
recorded as provenance. `urls` lists the target's own documents or pages (a
list, or `a|b` in a CSV cell); they are always downloaded and kept. `query`
replaces `queries` for that row. Discovery never infers geography from query
text. Relative paths resolve against the config directory; CSV headers may
carry a UTF-8 BOM. Use `--target-limit N` to run the first N rows; edit the
file to run a different subset.

A target's identity is its `partition_by` values, or every metadata column when
`partition_by` is unset. Identity names the target in checkpoints, labels,
review prompts, and curated ownership, so it must be unique: duplicate rows fail
with both row locations. Other columns are passthrough provenance; editing them
re-runs nothing.

```yaml
discovery:
  targets:
    - {jurisdiction: Chaffee County, state: CO,
       urls: [https://www.chaffeecounty.org/land_use_code.pdf]}
    - {manufacturer: Generac, query: "Generac 250kW generator manual pdf"}
  # or: targets: targets.csv
  # or: targets: {cross_product: {dimensions: [
  #       {name: state, values: [CO, NM]},
  #       {name: county, dataset: counties.csv, column: county}]}}
  queries:
    - "{jurisdiction} {state} geothermal ordinance pdf"
```

### `search` Settings

Search uses SerpApi and runs only when `queries` or a target `query` is set.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_results` | int | `10` | Local unique-candidate cap per query, including cached/news results. |
| `cache` | bool | `false` | Cache results on disk; a cache hit makes no live call. |
| `cache_ttl_minutes` | number | `0` | Cache expiry; `0` never expires. |
| `params` | dict | — | Extra SerpApi params (e.g. `{tbm: nws}`); `q`, `num`, `api_key` are reserved. |

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
    max_results: 6
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
mismatches. Use `crawl.allowed_domains` plus `crawl.include_url_patterns` when
source scope is a requirement, rather than relying on ranking boosts. Optional keyword
classification or paid LLM review happens after acquisition; neither establishes
population coverage.

Selection filters, `max_per_target`, the classifier's `action: filter`, and
`document_review.keep_top` apply to search results and crawled links only.
Configured URLs (`sources`, target `urls`) are always kept, and shared sources
do not count against any target's caps.

### `crawl` Settings

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `provider` | string | `http` | `http` or `selenium` (requires Selenium + Chrome). |
| `max_depth` | int | `0` | `0` = never follow links; `N` fetches HTML pages up to `N` links deep. |
| `max_pages` | int | `50` | HTML pages fetched per crawl. |
| `allowed_domains` | list | source hosts | Host scope for crawled links and downloads. |
| `include_url_patterns` | list | — | Regexes; matching links are treated as documents. |
| `include_link_text_patterns` | list | — | Regexes on link text. |
| `browser_downloads` | bool | `false` | Download through headless Chrome (bot-protected sites). |
| `browser_escalation` | dict | off | `{enabled: true}` opts into JS-shell browser recovery. |
| `code_host_adapters` | dict | off | `{enabled: true}` opts into code-host API recovery. |
| `request_headers` | dict | — | Public, non-empty HTTP headers; no credentials. |

Every candidate — configured URL or search result — that is an HTML page is
crawled when `max_depth > 0`. Crawled links are normal candidates subject to
selection.

### `runtime` Settings

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_files` | int | unbounded | Download cap; configured URLs never count. |
| `timeout_seconds` | int | engine default | Request timeout. |
| `max_concurrent_downloads` | int | `2` | Max parallel file downloads. |
| `min_request_interval_ms` | int | `0` | Minimum delay between outbound requests (ms). |

Other blocks: `document_classifier` (`action: warn` or `filter`),
`document_review` (paid LLM curation; writes an editable `review.csv`, see
[Curate](../README.md#curate)), and `partition_by` (`by_<f1>/<v1>/…` from
target metadata; untargeted documents go to `_shared/`). See
[ADVANCED.yaml](ADVANCED.yaml) for every key.

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
| `SERPAPI_API_KEY` | SerpApi key; keep out of YAML. |
| `SERPAPI_SSL_VERIFY` | Search TLS verification; overrides `PSWEEP_SSL_VERIFY`. Defaults to true. |
| `DISCOVERY_SSL_VERIFY` | Download TLS verification; overrides `PSWEEP_SSL_VERIFY`. Defaults to true. |
| `PSWEEP_SSL_VERIFY` | Model TLS verification and fallback for search/downloads. Defaults to true. |
| `REQUESTS_CA_BUNDLE` | Requests CA bundle path, including enterprise certificates if applicable. |

An explicit false value is permitted only in public mode and emits a warning:
connections and credentials can be intercepted. Prefer approved CA roots,
including on a VPN. Protected mode rejects any false setting. Invalid boolean
values always fail. Config validation neither contacts endpoints nor tests
their certificate chains.

### Discovery Outputs

Each acquisition run has a run-scoped folder, with a `latest` pointer to the
most recent run that produced downloads and a consolidated curated folder
for downstream input:

```text
outputs/<domain>/discovered/
    checkpoint.json                 # v2 resume: effective config + artifact evidence
    .review_cache/<key>.json        # content-addressed LLM review grades (all runs)
    latest -> runs/<run_id>         # symlink to the current run
    curated/                        # consolidated selected docs -> downstream input
    runs/<run_id>/
        manifest.json               # full run record
        download_index.csv          # machine-readable file list (+ review cols)
        review.csv                  # human-editable curation ledger
        documents/                  # all downloads from this run (not a recall guarantee)
            <partition>/<file>          # _shared / _targets/<target_id> / by_<fields>/...
            <partition>/.text/<filename>.txt        # cached OCR text
            <partition>/.text/<filename>.meta.json  # source + text digest evidence
        curated/                    # selected docs from this run
            <partition>/<file>
```

Extraction reads the consolidated `outputs/<domain>/discovered/curated` by
default; inspect `outputs/<domain>/discovered/runs/<run_id>/curated` for one run.
The internal `latest` pointer refers only to that latest run. Where a review ledger
is present, edit `review.csv` (`human_decision` = `keep`/`reject`) and run
`pixi run psweep curate` to rebuild curated selections.

Checkpoint v2 skips only work whose effective config fingerprint and artifact
SHA-256 evidence still match. Changed settings, missing/altered artifacts, and
v1 entries are not reusable. Targets and shared `sources` are checkpointed one by one and are not part
of the fingerprint, so adding one fetches only the new item. A target is also
re-run when what it executes changes (its `urls`, rendered queries, or rendered
identity selection terms). An item is
complete once every outcome is final (downloaded, skipped, rejected, or a
terminal failure such as HTTP 404 or a TLS error). Retryable failures (timeouts,
429/5xx, network errors, a robots.txt that could not be fetched for such a
reason) and a crawl cut short by such a failure or by
`max_pages`/`max_files` keep it open for the next run. Resumed runs
publish file by file; only `--fresh` replaces whole curated partitions. Each
curated path belongs to one target or source and stays stable across re-runs:
when another published owner already holds a filename, the newcomer gets a short
deterministic hash suffix (`report-1a2b3c4d.pdf`). Failed or unselected
downloads reserve no names.
Corpus publication rejects concurrent writers and
recovers interrupted curated/provenance swaps from its journal on the next
writer; this is discovery-specific, not a general pipeline transaction.

OCR pairs use full filenames, so `report.pdf` and `report.html` cannot collide.
Source size/mtime and digest plus the cached-text digest must match; partial,
stale, or mismatched pairs are misses. Temporary-file replacement publishes
metadata last; no stem-only fallback exists. Curation carries OCR caches with
selected files. Native extraction/OCR correction behavior is unchanged.

Discovery exit codes are verbosity-independent: `success`, `dry_run`, and
validated `reused` work return **0**; failed or partially failed acquisition
(including failure with zero downloads) returns **1**; genuinely `empty` work
returns **2**. Inspect the manifest's outcome rather than counting files alone.

**`manifest.json` observability sections:**
- `timing`: `started_at`, `completed_at`, `elapsed_seconds`
- `stage_summaries.seeker`: candidates discovered, after prioritization
- `stage_summaries.crawl`: provider, max_depth, applied, candidates-in/out
- `stage_summaries.downloads`: downloaded, skipped, failed, total_bytes
- `candidate_summary`: total, by acceptance class (accepted/needs_review/rejected), by source
- `lineage`: `documents_dir`, `download_index_csv`, `review_index_csv`, `curated_dir`, `curated_count`, ranked candidates

**`download_index.csv` columns:** `run_id`, `domain`, `target_label`, `target_id`, `source_scope`, `source_url`, `status`, `classification_passed`, `url`, `final_url`, `mime_type`, `bytes`, `relative_path`, `path`, `review_selected`, `review_is_primary`, `review_relevance`, `review_doc_kind`, `error`, `target_metadata`

**`review.csv` columns (human-editable):** `target`, `partition`, `source_scope`, `doc_kind`, `relevance`, `llm_is_primary`, `llm_selected`, `human_decision`, `human_notes`, `llm_reason`, `relative_path`, `path`

### Policy Design Decision

Discovery should remain domain-agnostic at the product level.

That means:
- prefer domain config values over built-in preset bundle names
- prefer reusable template variables and selection filters over domain-coded branches
- add new runtime knobs only when they are generic enough to help multiple domains

If a domain needs different discovery behavior, encode it in that domain's `run.yaml` rather than asking the runtime to grow a new hardcoded bundle family.

---

## Extraction Config Reference

### Variable Categories

| Category | Keys |
|----------|------|
| Required | `schema` (top-level; defaults to `schema.json` beside the config) |
| Optional | `input_dir`, `pages`, `provider`, `model`, `limit` |
| Advanced | `max_context`, `skip_existing`, `live_dashboard` |

`skip_existing` means verified reuse: matching source/schema/effective settings
and valid record-byte evidence are required. Stale, malformed, and old unverifiable
outputs re-extract. Limits apply after reuse. `--fresh` resets the entire resolved
output after preflight, then rebuilds only selected documents. Failed preflight
preserves old outputs. Extract exits 0 for success/reuse, 1 for any operational
failure (including partial), and 2 for empty selection; zero-item payloads are valid.

The inert `extraction.profile`, `--profile`, and `--category` surfaces are retired.
Use a JSON schema directly, not YAML schema packs. Model aliases remain string
maps; unreachable long-form definitions and `model_defaults` are rejected.

---

## Compilation Config Reference

| Category | Keys |
|----------|------|
| Required | `schema` (top-level; shared with extraction) |
| Optional | `dry_run`, `report_format` |
| Advanced | `fail_on_suspicious` |

---

## Adding Extraction and Compilation

After inspecting acquired documents, create `config/my_domain/schema.json` and
add only the needed sections to the same domain config:

```yaml
extraction:
  model: your-model-name
```

Configure LLM credentials in the environment before extraction. For local
inputs, set `extraction.input_dir: documents/my_domain`. [ADVANCED.yaml](ADVANCED.yaml)
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
