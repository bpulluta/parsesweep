# Configuration Reference

A ParseSweep run is driven by a YAML config containing any subset of
`extraction`, `compilation`, `discovery`, and `validation`. Loading validates
all supplied sections; each command executes only its own section. Only the
explicitly selected config file is loaded. Adjacent section override files
beside `run.yaml`, `run.yml`, or `run.json` are retired: move their settings into
the corresponding sections of the run config and remove those adjacent files,
or load a section-wrapped config explicitly with `--config`. The loader names
the files to migrate rather than silently ignoring them.

## Start small, add as you need

Discovery alone needs no schema or LLM configuration:

```yaml
domain: my_domain
discovery:
  queries:
    - "industrial pump installation manual filetype:pdf"
```

Set `SERPAPI_API_KEY` in the environment or `.env`, then run
`pixi run psweep discover --config config/my_domain/run.yaml`.
`discovery.queries` auto-enables SerpApi when search is omitted; explicit
`search.enabled: false` wins over inference.
Seed-only input does not auto-enable search. Replace `queries` with
`seeds: [https://example.org/documents/manual.pdf]` for known document URLs.

Start from `config/TEMPLATE.yaml`; use `config/ADVANCED.yaml` for expanded
stage settings. The existing `init-domain-schema` scaffold creates a schema,
not a discovery wizard or a new config mode.

Once you have suitable documents, only two keys are required to extract —
`schema` and `input_dir`:

```yaml
extraction:
  schema: schemas/personal/my_schema.json
  input_dir: discovered/my_domain/curated
```

Extraction requires LLM credentials; discovery does not unless you explicitly
configure paid `document_review`. Add a setting only when you have a reason
to change its default. The [Getting Started guide](getting-started.md) walks
through discovery first, and [Tuning & Iteration](tuning.md) covers later tuning.

## Defaults at a glance

These are the values that apply when you omit a key. Defaults come from
shared runtime definitions; templates show explicit choices separately.

| Setting | Section | Default | Change it when… |
|---------|---------|---------|-----------------|
| `search.enabled` | discovery | Inferred for `queries`; off for seed-only input | Explicitly enable/disable search |
| `search.max_results` | discovery | `10` unique candidates per query | Change the candidate budget, not the download count |
| `search.cache` | discovery | `false` | Opt into search-result caching |
| `selection.max_per_target` | discovery | `1` | Acquire more candidates per target |
| `follow_links` | discovery | `false` (direct acquisition) | Explicitly enable crawling |
| `runtime.max_concurrent_downloads` | discovery | `2` | Change download concurrency |
| `retry_policy.max_attempts` | discovery | `3` total attempts | Change the retry budget |
| `browser_mode` | discovery | `false` | Explicitly use headless-browser downloads |
| `browser_escalation.enabled` | discovery | `false` | Opt into thin/JS-shell browser recovery |
| `code_host_adapters.enabled` | discovery | `false` | Opt into supported code-host API recovery |
| `document_review` | discovery | Not configured (no LLM review) | Add explicit paid model-based curation |
| `partition_mode` | discovery | `auto` | You need a specific output layout |
| `policy.robots_mode` | discovery | `warn` | Enforce (`enforce`) or silence (`ignore`) |
| `policy.tos_mode` | discovery | `warn` | Enforce (`enforce`) or silence (`ignore`) |
| `model` | extraction | `.env` model → `gpt-4o-mini` | You want a stronger/cheaper tier |
| `max_context` | extraction | `600000` | Output is truncated on large docs |
| `output_dir` | extraction | `extracted/<domain>` | You want a custom location |
| `skip_existing` | extraction | `true` | Validated record reuse; `--fresh` resets whole output after preflight |
| `timeout_seconds` | extraction | `120` (`LLM_TIMEOUT` env) | Very large docs time out |
| `provider` | extraction | `auto` | Auto-detection picks the wrong one |
| `pages.auto_locate.trigger_chars` | extraction | `200000` | Target only docs above a size |
| `pages.auto_locate.max_selected_pages` | extraction | `30` | Sections span more/fewer pages |
| `output.default_format` | compilation | `both` when unset | You want only `csv` or `excel` |
| `synthesis.min_sources_for_llm` | compilation | `2` | Tune when the LLM reconciler runs |
| dedup `key_fields` | schema `$metadata.identity.deduplication` | none | Compiled sheet has duplicate rows |

The [Command Reference](commands/index.md) also renders every CLI option's
default directly from the live command definitions.

## Discovery budgets, scope, and review

`search.max_results` caps **unique candidates per rendered query**, locally,
across live/cached results and enabled news results. Provider `num` is a request
hint, not an authoritative limit. There is no hidden pagination or refill.
`selection.max_per_target` is the **acquisition count per target**, after scope
and candidate selection; it is separate from `document_review.keep_top`, which
controls optional post-acquisition LLM curation. Neither cap guarantees that
many successful downloads or establishes search recall.

Search cache is off by default. With `search.cache: true`, a cache hit makes
zero live search calls. Browser recovery also remains off until explicitly
enabled, including when threshold settings are supplied:

```yaml
discovery:
  browser_escalation:
    enabled: true
    min_shell_chars: 8000
  code_host_adapters:
    enabled: true
```

HTML/HTM and extensionless HTTP(S) URLs are provisionally eligible with the
default `selection.require_supported_document`; you do not need to set it to
`false` to discover web documents. Known non-document binaries are excluded;
GET/MIME checks verify ambiguous candidates. PDF candidates are preferred to
web pages when present. Ranking and MIME validation do not certify document
relevance or semantic accuracy.

For strict sources, combine `allowed_domains` with a URL-path regex:

```yaml
discovery:
  queries:
    - "site:example.org installation manual filetype:pdf"
  allowed_domains: [example.org]
  discovery_rules:
    include_url_patterns: ['^https?://(?:www\.)?example\.org/documents/']
```

Scope applies before top-one selection and to final redirects. Search `site:`
and `filetype:` hints are not scope/type guarantees.
`selection.relevance_allowed_domain_patterns` is a ranking boost, not a host
allowlist. `selection.exclude_url_patterns` is a case-insensitive regex guard
on URLs, not a substitute for positive source scope. Concrete provider/vendor
source filters belong in domain configs; a document parser does not configure
them automatically.

Selection uses `require_any` and `exclude` for literal relevance terms, and
`exclude_text_patterns` for candidate-text regex guards. Domain-authored
`draft_patterns` with `exclude_draft: true` express draft filtering; there is
no built-in legal draft list. Ranking has no built-in vendor/government
authority table or shopping-path list: configure `link_prioritization.domain_scores`
and `shopping_path_keywords` when needed. Configured ranking `keywords`
replace the generic `document`/`download`/`guide`/`report` fallback.

Geographic partitioning uses explicit `state` and `jurisdiction` request/target
fields, not inference from query text. `jurisdiction_aliases` supplies only
configured aliases; there is no built-in US discovery alias table. Use
`partition_by` for arbitrary metadata layouts.

`document_review` is never enabled by query inference or model aliases. Add an
explicit `document_description` and `model`, and configure LLM credentials,
only when you want paid model-based curation. Without it, acquisition uses
heuristics; inspect the files before extraction. Run-scoped selected files are
under `discovered/<domain>/runs/<run_id>/curated`; the consolidated downstream
folder is `discovered/<domain>/curated`.

Credentials belong only in environment variables or `.env`, not YAML.
`SERPAPI_SSL_VERIFY` and `DISCOVERY_SSL_VERIFY` retain their default `false`;
set them explicitly to choose TLS verification. `REQUESTS_CA_BUNDLE` selects
a Requests CA bundle. These options do not change what config validation checks.

## Discovery configuration validation

Discovery validates consumed nested blocks even without `--config-strict`:
unknown options, quoted booleans such as `"false"`, invalid regular expressions,
and invalid resource limits fail before acquisition. Use unquoted YAML
`true`/`false`, actual integers for count limits, and finite non-negative numbers
for delays. `runtime.max_depth: 0` disables descent; concurrency, result counts,
page/file budgets, timeouts, and per-target selection counts must be positive.
Host-diversity caps and request intervals may be zero.

### Canonical YAML migration

Retired aliases fail with a replacement-key error, even without strict mode
and even if a canonical key is also supplied. They are not merged:

| Retired key | Use instead |
|-------------|-------------|
| `discovery.seeker` | `discovery.search` (including `use_query_family`) |
| `discovery.browser` | `discovery.browser_mode` |
| `discovery.search.max_results_per_query` | `discovery.search.max_results` |
| `discovery.selection.primary_per_target` | `discovery.selection.max_per_target` |
| `discovery.selection.relevance_require_any_terms` | `discovery.selection.require_any` |
| `discovery.selection.relevance_exclude_any_terms` | `discovery.selection.exclude` |

The unused `pipeline`, `scoring`, `dedupe`, `file_filters`, and
`keyword_validation` discovery blocks are rejected. Remove these no-op blocks;
use `selection`, `link_prioritization`, `discovery_rules`, and
`document_classifier` for supported behavior. Provider-specific
`search.serpapi_params` and target metadata remain open maps, except reserved
`q`, `num`, and `api_key` request controls cannot be overridden.

`discover --validate-config` uses the same normalized input-plan check as
execution, without invoking the engine, contacting providers, or writing run
artifacts. It requires seeds, a query, targets, hub pages, or enabled search
with usable templates. Quiet output includes normalized defaults and the
applied target limit. The `run` CLI and Python `run_pipeline` perform this check
before starting discovery; invalid run configs never fall back to raw YAML.
Validation does not establish provider availability, credentials, or live
network reliability.

`discovery.request_headers` reaches the engine's HTTP requests. Set public
headers such as `User-Agent` here; do not store secrets in configs or print
secret-bearing headers with effective-config/validation output. Browser requests
have their own header behavior. Explicit CLI options override config values,
including the domain. Within a config, explicit `search.enabled: false`
disables search even if `enable_serpapi` was true.

Strict robots/ToS enforcement is available for HTTP acquisition. It cannot
certify browser redirects or subresource requests and therefore fails before
unsupported browser navigation, including escalation. Keep HTTP acquisition
when enforcement is required; browser opt-in is not enforcement support.

### Outcomes, resume, and discovery publication

Outcome and exit code do not depend on quiet/default/verbose rendering:

| Manifest status | Exit code | Meaning |
|-----------------|-----------|---------|
| `success`, `dry_run`, `reused` | `0` | Successful acquisition, valid plan, or validated reuse with no errors. |
| `failed`, `partial_failure` | `1` | Acquisition failed wholly or partly; zero downloads caused by failure are not an empty success. |
| `empty` | `2` | No usable results and no acquisition failure. |

Checkpoint **v2** requires the effective discovery config fingerprint and
SHA-256 artifact evidence, including curated files, to match before reuse.
Config changes, missing/changed artifacts, and old v1 entries cause
reacquisition. Search-result caching remains a separate opt-in mechanism.

Corpus publication rejects concurrent writers with a lock and journals the
known curated/provenance swaps. The next writer recovers an interrupted
publication; corrupt recovery evidence fails closed rather than overwriting
prior files. This is a small discovery-specific protocol, not a general
pipeline or output transaction.

OCR pairs use `.text/<filename>.txt` and `.text/<filename>.meta.json`, so
`report.pdf` and `report.html` do not share a cache. Readers verify source
size/mtime and source SHA-256 plus cached-text SHA-256. Partial/stale/mismatched
pairs are misses; temporary-file replacement publishes metadata last. Old
stem-only pairs are not reused. LLM reviewer sidecars use
`.review/<filename>.json`; curation carries these full-name sidecars with
selected documents. OCR caches now hold raw text, applying current corrections
on consumption; older corrected entries invalidate rather than silently reuse.
Page/section products fingerprint source content and resolved model/settings;
section cache pairs use full filenames, text digests, and metadata-last staging.
Native, pypdf, and OCR paths honor selected page bounds and retain actual scope.

Extraction reuses records only when source/schema/settings and record-byte
evidence match. Missing proof or invalid JSON triggers re-extraction, without a
new resume setting. Valid reuse needs no provider credentials or calls. `--fresh`
resets the whole resolved output after successful preflight; filtered/limited
fresh runs rebuild only that subset. Overlapping output/input paths are rejected.
Exit 0 means success/reuse (including valid zero-item payloads); exit 1 includes
partial failure; empty selection exits 2. Pipeline drivers halt downstream work.
Atomic per-file record/manifest replacement is not a whole-run transaction or
concurrent-writer certification. Inert category/profile settings and YAML schema
pack unwrapping are retired; use direct JSON schemas and supported string aliases.

### Advanced: column-flattening overrides

When compiling nested records into flat spreadsheet columns, ParseSweep uses a
set of heuristic field-name defaults (which fields name a *type*, hold a
*value*, carry a *unit*, and so on). These ship tuned for tabular/rate-style
data; if your domain names fields differently, override them per-schema in the
schema's `$metadata.compilation.flattening` block (e.g. `type_fields`,
`value_fields`, `unit_fields`, `skip_fields`) — no code changes needed. Every
knob falls back to the built-in default when omitted.

## Schema-driven compilation

Output columns come from the domain schema, not a fixed opinion, project, or
geographic record layout. A manual can declare numeric measurements and units;
a regulation can declare requirements; a public-comment schema can declare
sentiment and evidence. Fields absent from that domain are not invented as
empty consumer columns.

Use the existing `compilation.output` settings to rename, prioritize, or exclude
columns. For example, a measurement schema declaring `manufacturer`,
`rated_power_kw`, and `measurement_quote` can use:

```yaml
compilation:
  output:
    default_format: csv
    column_renames:
      Manufacturer: manufacturer
      Rated Power Kw: rated_power_kw
      Measurement Quote: evidence
    column_order: [manufacturer, rated_power_kw, evidence]
```

`column_order` is a priority prefix, not an exact column selection; remaining
schema fields are retained. `exclude_fields` removes unwanted fields. Nested
flattening uses the schema's existing `compilation.flattening` settings.
Canonical extraction JSON remains the source for lossless arrays/objects,
quality warnings, coverage, document identity, and lineage. Spreadsheet output
is a presentation, not a replacement for that record.

Runtime compilation settings override schema `$metadata.compilation` settings.
With no format setting, CLI and Python API emit CSV and Excel. Compilation and
`run --validate-config` validate the merged contract before scanning records,
calling synthesis models, or clearing outputs with `--fresh`. Excel batches
above 1,048,575 data rows or with cells over 32,767 characters fail before
either export; select CSV to preserve full evidence. Requested files are written
to staging before publication; caught writer/publication failures restore prior
output where possible. If restoration fails, recoverable backups are retained
and their paths appear in the error. This is not a crash-safe multi-file
transaction: do not run concurrent writers into the same output directory.

Semantic summaries and classifications belong in the extraction schema's
instructions and constraints. Existing `compilation.synthesis` handles
cross-document reconciliation. Deterministic code validates structure,
provenance, identity, and export contracts; JSON Schema validation, including
declared calendar-date formats, does **not** certify quotation authenticity,
sentiment, or geographical support. US county-FIPS derivation remains an
explicit, optional wide-output normalization, not a requirement of any domain.

Page/section selection and context truncation are preserved as limitations in
canonical records and extraction manifests. Incomplete searches do not replace
previous good curated partitions. A seeded or capped sample is not a recall or
population estimate.

### Scale boundary

The compiler prepares one export frame for CSV/Excel, but global identity and
deduplication still require an in-memory frame. Complete exact/fuzzy keys use
indexed grouping rather than pairwise scans. Null-as-wildcard fuzzy matching
also uses indexed candidates while preserving ordered matching semantics;
highly ambiguous multidimensional identity buckets can still be expensive.
Prefer CSV for large batches; benchmark representative record sizes and
duplicate distributions before scaling. Schemas without
identity keys do not deduplicate; choose domain-appropriate keys explicitly
rather than inferring them in core code. Configured keys missing from both the
schema and compiled columns fail instead of silently deduplicating on fewer
fields. Atomic checkpoint snapshots avoid repeated whole-file writes, and
review caches avoid repeated parsing/model calls when grading inputs are
unchanged. Neither feature bounds acquisition latency or whole-pipeline
memory. Synthetic record, checkpoint, and mocked timeout tests do not establish
100,000-target, large-document, fuzzy-dedup, or live-API readiness.

## Minimal discovery template

Copy this short template to `config/<your_domain>/run.yaml`. Only discovery is
active; common choices and minimal later extraction/compilation blocks are
commented out. The active starter remains just `domain` and `discovery.queries`.

```{literalinclude} ../config/TEMPLATE.yaml
:language: yaml
:caption: config/TEMPLATE.yaml
```

## Advanced annotated reference

The expanded reference retains the original extraction, compilation, discovery,
model, page-targeting, and QA/QC examples. Copy only the sections you need;
placeholder models and schema paths must be replaced before those stages run.

```{literalinclude} ../config/ADVANCED.yaml
:language: yaml
:caption: config/ADVANCED.yaml
```
