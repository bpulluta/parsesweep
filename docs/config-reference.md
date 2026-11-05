# Configuration Reference

A ParseSweep run is driven by a YAML config containing any subset of
`extraction`, `compilation`, and `discovery`. Loading validates
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
Search runs only when `discovery.queries` (or a target `query`) is set.
Replace `queries` with `sources: [https://example.org/documents/manual.pdf]`
for known document URLs; configured URLs are always downloaded and kept.

Start from `config/TEMPLATE.yaml`; use `config/ADVANCED.yaml` for expanded
stage settings. The existing `init-domain-schema` scaffold creates a schema,
not a discovery wizard or a new config mode.

Once you have suitable documents, extraction needs only a schema: put it at
`config/my_domain/schema.json` (or set top-level `schema:`). Inputs default to
`outputs/my_domain/discovered/curated`; all outputs are derived under
`outputs/my_domain/`. For local documents, pass the path positionally:
`pixi run psweep extract documents/my_domain/ --config config/my_domain/run.yaml`.

Extraction requires LLM credentials; discovery does not unless you explicitly
configure paid `document_review`. Add a setting only when you have a reason
to change its default. The [Getting Started guide](getting-started.md) walks
through discovery first, and [Tuning & Iteration](tuning.md) covers later tuning.

## Defaults at a glance

These are the values that apply when you omit a key. Defaults come from
shared runtime definitions; templates show explicit choices separately.

| Setting | Section | Default | Change it when… |
|---------|---------|---------|-----------------|
| `search.max_results` | discovery | `10` unique candidates per query | Change the candidate budget, not the download count |
| `search.cache` | discovery | `false` | Opt into search-result caching |
| `selection.max_per_target` | discovery | `1` | Acquire more candidates per target |
| `crawl.max_depth` | discovery | `0` (no link following) | Crawl index pages |
| `crawl.provider` | discovery | `http` | Use `selenium` for JS/bot-protected pages |
| `runtime.max_files` | discovery | unbounded (configured URLs never count) | Cap downloads |
| `runtime.max_concurrent_downloads` | discovery | `2` | Change download concurrency |
| `retry_policy.max_attempts` | discovery | `3` total attempts | Change the retry budget |
| `crawl.browser_downloads` | discovery | `false` | Download through a headless browser |
| `crawl.browser_escalation.enabled` | discovery | `false` | Opt into browser recovery of JS shells and refused (401/403/429/451) pages and downloads |
| `crawl.code_host_adapters.enabled` | discovery | `false` | Opt into supported code-host API recovery |
| `document_review` | discovery | Not configured (no LLM review) | Add explicit paid model-based curation |
| `partition_by` | discovery | `_targets/<id>/` per target, `_shared/` for sources | You need a metadata-based output layout |
| `policy.robots_mode` | discovery | `warn` | Enforce (`enforce`) or silence (`ignore`) |
| `policy.tos_mode` | discovery | `warn` | Enforce (`enforce`) or silence (`ignore`) |
| `model` | extraction | `.env` model → `gpt-4o-mini` | You want a stronger/cheaper tier |
| `max_context` | extraction | `600000` | Output is truncated on large docs |
| `schema` | top level | `schema.json` beside the config | The schema lives elsewhere |
| `max_concurrent` | extraction | `4` | Provider rate limits allow more (or fewer) parallel documents |
| `skip_existing` | extraction | `true` | Validated record reuse; `--fresh` resets whole output after preflight |
| `timeout_seconds` | extraction | `120` (`LLM_TIMEOUT` env) | Very large docs time out |
| `provider` | extraction | `auto` | Auto-detection picks the wrong one |
| `pages.auto_locate.trigger_chars` | extraction | `200000` | Target only docs above a size |
| `pages.auto_locate.max_selected_pages` | extraction | `30` | Sections span more/fewer pages |
| `output.default_format` | compilation | `both` when unset | You want only `csv` or `excel` |
| dedup `key_fields` | schema `$metadata.identity.deduplication` | none | Compiled sheet has duplicate rows |
| `judge` | validation | none (no judging) | Disputed cells need a verdict |
| `fields` | validation | every scorable field | Focus scoring on a few columns |
| `min_accuracy` | validation | none (no gate) | `validate` must fail below a reference accuracy |

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
  crawl:
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

For strict sources, combine `crawl.allowed_domains` with a URL-path regex:

```yaml
discovery:
  queries:
    - "site:example.org installation manual filetype:pdf"
  crawl:
    allowed_domains: [example.org]
    include_url_patterns: ['^https?://(?:www\.)?example\.org/documents/']
```

Configured URLs (`sources` and target `urls`) bypass selection, the
classifier's `action: filter`, and `document_review.keep_top`; shared sources
do not count against any target's caps. Links crawled from them (with
`crawl.max_depth > 0`) are ordinary candidates, except query variants of a
configured page (its pagination), which are that configured page.

Without `crawl.include_link_text_patterns`, a crawl follows non-navigation
links. With it, only links whose text matches are followed, and each matching
page is also kept, so a paginated listing is collected page by page:

```yaml
discovery:
  crawl:
    max_depth: 10                       # longest page chain to follow
    include_link_text_patterns: ['^next$']
```

Every non-document candidate (search results included) is fetched once at
depth 0 to look for matching links, and `crawl.max_pages` is one budget for the
whole run: size it above the number of HTML candidates plus listing pages.

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

Output layout comes from explicit target fields via `partition_by`, never from
query text. Each document records `source_scope` (`shared` or `target`), its
`target_id`, and the page it was crawled from (`source_url`).

`document_review` is never enabled by query inference or model aliases. Add an
explicit `document_description` and `model`, and configure LLM credentials,
only when you want paid model-based curation. Without it, acquisition uses
heuristics; inspect the files before extraction. Run-scoped selected files are
under `outputs/<domain>/discovered/runs/<run_id>/curated`; the consolidated downstream
folder is `outputs/<domain>/discovered/curated`.

Credentials belong only in environment variables or `.env`, not YAML.
TLS verification defaults to true. `SERPAPI_SSL_VERIFY` and
`DISCOVERY_SSL_VERIFY` override the `PSWEEP_SSL_VERIFY` fallback for search and
downloads; models use `PSWEEP_SSL_VERIFY`. Explicit request arguments take
precedence. False values are allowed with a warning only in public mode;
protected mode rejects any configured false value, even if overridden.
Invalid boolean values fail. Prefer `REQUESTS_CA_BUNDLE` (or `SSL_CERT_FILE`)
for approved VPN/private roots instead of disabling verification. Discovery
ignores environment proxies and `.netrc` credentials.
Files streamed to disk stop at 1 GiB decoded (positive integer
`PSWEEP_MAX_RESPONSE_BYTES` overrides it) and abort only after
`runtime.timeout_seconds` without data; pages, search and API responses read
into memory stop at a fixed 50 MiB and must finish within the timeout. An
oversized document is a failed download (`partial_failure`), never a skip;
after raising the limit, rediscover with `--fresh`.
See [Security and protected operation](security.md) for network/resource limits.

## Discovery configuration validation

Config is always strict: unknown keys at any level fail with a did-you-mean
suggestion naming the file and key path. Quoted booleans such as `"false"`,
invalid regular expressions, non-http(s) URLs, and invalid limits also fail
before acquisition. `crawl.max_depth: 0` disables link following; result
counts, page/file budgets, timeouts, and per-target selection counts must be
positive. `search.params` is an open map except the reserved `q`, `num`, and
`api_key`.

`discover --validate-config` uses the same input-plan check as execution,
without contacting providers or writing run artifacts. It requires `sources`,
`queries`, or target rows with `query`/`urls`. The `run` CLI and Python
`run_pipeline` perform this check before starting discovery. Validation does
not establish provider availability, credentials, or live network reliability.

`discovery.crawl.request_headers` reaches every HTTP request (pages, crawl,
robots.txt, code-host APIs). The default `User-Agent` is
`Mozilla/5.0 (compatible; ParseSweep/<version>)`; robots.txt rules are matched
on the `ParseSweep` token whatever `User-Agent` is sent. Browser sessions send
the same `User-Agent` (the only header Chrome lets them set). Do not store
secrets in configs or print secret-bearing headers with
effective-config/validation output. Explicit CLI options (`--domain`,
`--source`, `--query`) override config values.

Strict robots/ToS enforcement is available for HTTP acquisition. It cannot
certify browser redirects or subresource requests and therefore fails before
unsupported browser navigation, including escalation. Keep HTTP acquisition
when enforcement is required; browser opt-in is not enforcement support.

### Outcomes, resume, and discovery publication

Outcome and exit code do not depend on quiet/default/verbose rendering:

| Manifest status | Exit code | Meaning |
|-----------------|-----------|---------|
| `success`, `dry_run`, `reused` | `0` | Successful acquisition, valid plan, or validated reuse with no errors. |
| `failed` | `1` | Acquisition failed; zero downloads caused by failure are not an empty success. |
| `empty` | `2` | No usable results and no acquisition failure. |
| `partial_failure` | `3` | Usable results plus recorded errors; `run` continues downstream and a re-run retries the failures. |

Checkpoint **v2** requires the effective discovery config fingerprint and
SHA-256 artifact evidence, including curated files, to match before reuse.
Config changes, missing/changed artifacts, and old v1 entries cause
reacquisition. Search-result caching remains a separate opt-in mechanism. Targets and shared `sources` are checkpointed one by one and are not part
of the fingerprint, so adding one fetches only the new item; a target also
re-runs when its `urls` or rendered queries change. Resumed runs
publish file by file; only `--fresh` replaces whole curated partitions.

An item left incomplete (retryable failure, unfinished search or crawl, or the
`runtime.max_files` budget) publishes its downloads so far additively — same
names are replaced, nothing is withdrawn — and its checkpoint entry keeps the
final outcome of each child URL. The next run re-fetches the listing or search,
reuses every earlier download whose file still matches its SHA-256 without a
request and without using a `max_files` slot, does not re-request terminal
outcomes (e.g. HTTP 404), and fetches only the remaining children. A capped
listing therefore converges batch by batch, and a run with one transient child
failure re-fetches only that child. Run notes report reused and fetched counts
and why partitions are still incomplete. A completed item makes no requests;
`psweep discover --fresh` re-checks completed listings and ignores all reuse.

Corpus publication rejects concurrent writers with a lock and journals the
known curated/provenance swaps. The next writer recovers an interrupted
publication; corrupt recovery evidence fails closed rather than overwriting
prior files. This is a small discovery-specific protocol, not a general
pipeline or output transaction.

OCR pairs use `.text/<filename>.txt` and `.text/<filename>.meta.json`, so
`report.pdf` and `report.html` do not share a cache. Readers verify source
size/mtime and source SHA-256 plus cached-text SHA-256. Partial/stale/mismatched
pairs are misses; temporary-file replacement publishes metadata last. Old
stem-only pairs are not reused. Republished discovery files (including
`--fresh`) inherit the OCR/transcript text of a replaced file with the same
SHA-256, so unchanged scans and recordings are not processed again. LLM review grades are cached by document content
and grading inputs in `discovered/.review_cache/<key>.json` and reused across
runs. OCR caches now hold raw text, applying current corrections
on consumption; older corrected entries invalidate rather than silently reuse.
Page/section products fingerprint source content and resolved model/settings;
section cache pairs use full filenames, text digests, and metadata-last staging.
Native, pypdf, and OCR paths honor selected page bounds and retain actual scope.

Extraction reuses records only when source/schema/settings and record-byte
evidence match. Missing proof or invalid JSON triggers re-extraction, without a
new resume setting. Valid reuse needs no provider credentials or calls. `--fresh`
resets the whole resolved output after successful preflight; filtered/limited
fresh runs rebuild only that subset. Overlapping output/input paths are rejected.
Exit 0 means success/reuse (including valid zero-item payloads); exit 1 is
failure; empty selection exits 2; partial failure exits 3. `run` (and
`run_pipeline`) halt on 1 or 2, continue past 3, and exit 3 at the end.
Atomic per-file record/manifest replacement is not a whole-run transaction or
concurrent-writer certification. Inert category/profile settings and YAML schema
pack unwrapping are retired; use direct JSON schemas and supported string aliases.

### Advanced: column-flattening overrides

Nested objects flatten to `parent_child` columns and scalar lists join with
commas. The schema's `$metadata.compilation.flattening` block accepts only
`row_array` (which nested object array expands to one row per child),
`unit_fields`, and `unit_normalizations` (an opt-in value map applied to the
named unit fields, e.g. `{"ft": "feet"}`).

## Schema-driven compilation

Output columns come from the domain schema, not a fixed opinion, project, or
geographic record layout. A manual can declare numeric measurements and units;
a regulation can declare requirements; a public-comment schema can declare
sentiment and evidence. Fields absent from that domain are not invented as
empty consumer columns.

Each output header is the schema field's `title`, or the field name when no
title is set — rename a column by setting its `title`. `compilation.output`
prioritizes or excludes columns by those exact headers; unknown headers fail
validation. For example, a measurement schema whose `measurement_quote` field
has `"title": "Evidence"` can use:

```yaml
compilation:
  output:
    default_format: csv
    column_order: [manufacturer, rated_power_kw, Evidence]
```

`column_order` is a priority prefix, not an exact column selection; remaining
schema fields are retained. `exclude_fields` removes unwanted headers.
`compilation.normalization` keys (`state_column`, `county_fips: {state, county}`)
name schema fields instead.

When `discovery` declares `targets` and `partition_by`, compile switches to
roster mode: one row per target in roster order, extra target columns passed
through, and `sources`, `confidence`, and `evidence` columns per row. Nested
flattening uses the schema's existing `compilation.flattening` settings.
Canonical extraction JSON remains the source for lossless arrays/objects,
quality warnings, coverage, document identity, and lineage. Spreadsheet output
is a presentation, not a replacement for that record.

Runtime compilation settings override schema `$metadata.compilation` settings.
With no format setting, CLI and Python API emit CSV and Excel. Compilation and
`run --validate-config` validate the merged contract before scanning records,
or clearing outputs with `--fresh`. Excel batches
above 1,048,575 data rows or with cells over 32,767 characters fail before
either export; select CSV to preserve full evidence. Requested files are written
to staging before publication; caught writer/publication failures restore prior
output where possible. If restoration fails, recoverable backups are retained
and their paths appear in the error. This is not a crash-safe multi-file
transaction: do not run concurrent writers into the same output directory.

Semantic summaries and classifications belong in the extraction schema's
instructions and constraints. Roster compilation reconciles values across
documents deterministically. Deterministic code validates structure,
provenance, identity, and export contracts; JSON Schema validation, including
declared calendar-date formats, does **not** certify quotation authenticity,
sentiment, or geographical support. US county-FIPS derivation remains an
explicit, optional wide-output normalization, not a requirement of any domain.

Page/section selection and context truncation are preserved as limitations in
canonical records and extraction manifests. Incomplete searches only add to
previous good curated partitions; they never withdraw files. A seeded or capped
sample is not a recall or population estimate.

### Scale boundary

The compiler prepares one export frame for CSV/Excel, but global identity and
deduplication still require an in-memory frame. Complete exact/fuzzy keys use
indexed grouping rather than pairwise scans. Null-as-wildcard fuzzy matching
also uses indexed candidates while preserving ordered matching semantics;
highly ambiguous multidimensional identity buckets can still be expensive.
Prefer CSV for large batches; measure representative record sizes and
duplicate distributions before scaling. Schemas without
identity keys do not deduplicate; choose domain-appropriate keys explicitly
rather than inferring them in core code. Configured keys missing from both the
schema and compiled columns fail instead of silently deduplicating on fewer
fields. Atomic checkpoint snapshots avoid repeated whole-file writes, and
review caches avoid repeated parsing/model calls when grading inputs are
unchanged. Neither feature bounds acquisition latency or whole-pipeline
memory. Synthetic record, checkpoint, and mocked timeout tests do not establish
100,000-target, large-document, fuzzy-dedup, or live-API readiness.

## Validation

The `validation:` block (`models`, `references`, `judge`, `fields`,
`min_accuracy`, and rows-mode `key_fields`/`quote_field`) is strict: unknown
aliases, fields, or reference paths fail at load with a did-you-mean
suggestion. Challengers go through the real extract and compile path into
`validated/<alias>/`. Records, compiled tables, and judge verdicts are reused
while they stay fresh. A stale primary extraction or table is an error (run
`extract` or `compile`), and `--fresh` clears `validated/`. `validate --sample N`
draws a seeded blind labelling sheet; it is a sample, not a population estimate.
Accuracy carries a 95% Wilson interval.

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
model, and page-targeting examples. Copy only the sections you need;
placeholder models and schema paths must be replaced before those stages run.

```{literalinclude} ../config/ADVANCED.yaml
:language: yaml
:caption: config/ADVANCED.yaml
```
