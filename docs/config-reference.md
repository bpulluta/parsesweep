# Configuration Reference

This page is the single reference for `run.yaml`. Every key, default and rule
below is taken from the loaders that enforce them; the
[Command Reference](commands/index.md) renders every CLI option from the live
command definitions.

## The run config

A domain lives in `config/<domain>/`:

```text
config/<domain>/
├── run.yaml       # this file: runtime behavior for every stage
├── schema.json    # extraction contract (see Schema Authoring)
├── targets.csv    # optional roster: one row per target
└── samples/       # optional human reference sheets for validation
```

Only the config passed with `--config` is loaded. Each command runs its own
section and validates all supplied sections. Loading is strict at every level:
unknown keys fail with a did-you-mean suggestion naming the file and key path;
quoted booleans (`"false"`), invalid regexes, non-http(s) URLs and out-of-range
limits fail before any work starts. Relative `targets`, `pages.csv` and
`validation.references` paths resolve against the config directory. Credentials never go in YAML; set them in the environment or `.env`
(see [Environment variables](#environment-variables)).

Check a config without network calls or run artifacts:

```bash
pixi run psweep run --config config/<domain>/run.yaml --validate-config
pixi run psweep discover --config config/<domain>/run.yaml --show-effective-config
```

`config/TEMPLATE.yaml` is a valid starting point covering all four stages:

```{literalinclude} ../config/TEMPLATE.yaml
:language: yaml
:caption: config/TEMPLATE.yaml
```

`config/example/` is the smallest runnable domain: three targets with known
`urls`, so it needs no search key.
[Getting Started](getting-started.md#3-run-the-example) runs it.

### Top-level keys

| Key | Default | Meaning |
|-----|---------|---------|
| `domain` | required | Names the run; every output is derived under `outputs/<domain>/`. |
| `schema` | `schema.json` beside the config | Extraction schema path. |
| `models` | — | Alias → model-name map. Stages reference aliases (`primary`, `secondary`, …) so a model is changed in one place. |
| `model_context_windows` | — | Model name → context window (positive integer) for models the runtime does not know. |
| `reasoning_models` | — | Model names to treat as reasoning models (sent without `temperature`; JSON mode stays on). |
| `discovery` | — | [Discovery](#discovery). |
| `extraction` | — | [Extraction](#extraction). |
| `compilation` | — | [Compilation](#compilation). |
| `validation` | — | [Validation](#validation); `run` adds the validate stage when present. |

## Discovery

Discovery finds, downloads and curates documents. It needs no schema and no
LLM credentials unless `document_review` is configured; search needs
`SERPAPI_API_KEY`.

### Inputs

| Key | Meaning |
|-----|---------|
| `targets` | `.csv`/`.json` path, inline list of rows, or `{cross_product: {dimensions: [...]}}`. |
| `sources` | Shared http(s) URLs saved under `_shared/`; bypass relevance selection, but obey explicitly enabled PDF screening. |
| `queries` | Search templates: `{field}` is required, `{field?}` optional. Search runs only when `queries` or a target `query` is set. |
| `partition_by` | Target fields that lay out curated output as `by_<f1>/<v1>/by_<f2>/<v2>/`. Without it a target's files go under `_targets/<id>/`. |

Each target row is one target. Every column except `urls` and `query` is
metadata: it fills query templates, partitions output and is recorded as
provenance. `urls` lists the target's own documents or pages (a list, or `a|b`
in a CSV cell) and bypasses relevance selection, but obeys explicitly enabled
PDF screening; `query` replaces `queries` for that row. Discovery never infers geography from query text. CSV headers
may carry a UTF-8 BOM.

A target's identity is its `partition_by` values (every metadata column when
`partition_by` is unset). Identity names the target in checkpoints, review
prompts and curated ownership, so it must be unique: duplicate rows fail with
both row locations. `--target-limit/-n N` runs only the first N targets.

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

### `search`

| Key | Default | Meaning |
|-----|---------|---------|
| `max_results` | `10` | Unique candidates kept **per rendered query** (live, cached and news results together). No hidden pagination or refill. |
| `cache` | `false` | Cache results on disk; a hit makes no live call. |
| `cache_ttl_minutes` | `0` | Cache expiry; `0` never expires. |
| `params` | — | Extra SerpApi parameters sent verbatim (`{engine: bing}`, `{tbm: nws}`); `q`, `num`, `api_key` are reserved. |
| `fallback_queries` | absent/off | One additional set of target templates, only for targets below `document_review.keep_top` distinct automatically qualified sources. Requires `document_review.document_description`. |

For Google, `params: {udm: web}` (the "Web" tab) is recommended: without it
Google often ignores quoted names and `site:`. `site:`/`filetype:` are hints,
not guarantees. Tracking parameters (`utm_*`, `gclid`, `srsltid`, …) are
dropped from candidate URLs.

### `crawl`

| Key | Default | Meaning |
|-----|---------|---------|
| `provider` | `http` | `http` or `selenium` (needs Chrome; see [Security](security.md)). |
| `max_depth` | `0` | Link depth from each HTML candidate; `0` disables crawling. |
| `max_pages` | `50` | HTML pages fetched for the **whole run**. |
| `allowed_domains` | source hosts | Host scope for crawled links and downloads. |
| `include_url_patterns` | — | Regexes; matching links are treated as documents. |
| `include_link_text_patterns` | — | Regexes on link text; when set, only matching links are followed and each matching page is kept. |
| `browser_downloads` | `false` | Download through headless Chrome. |
| `browser_escalation` | off | `true` or `{enabled, settle_seconds, min_shell_chars, min_rendered_chars}`: re-fetch JS shells and refused (401/403/429/451) pages and downloads in a browser. Thresholds alone do not enable it. |
| `code_host_adapters` | off | `true` or `{enabled: true}`: fetch code-host content (Municode) through its API. |
| `request_headers` | — | Extra HTTP headers on every request (pages, robots.txt, APIs). Never credentials. |

When `max_depth > 0`, every HTML candidate (configured URL or search result) is
fetched once to look for links; crawled links are ordinary candidates, except
query variants of a configured page (its pagination), which belong to that page.

### `runtime` and `retry_policy`

| Key | Default | Meaning |
|-----|---------|---------|
| `runtime.max_files` | unbounded | Download budget per run; configured URLs never count. |
| `runtime.timeout_seconds` | engine default | Request timeout, and the stall limit (time without data) for file downloads. |
| `runtime.max_concurrent_downloads` | `2` | Parallel downloads. |
| `runtime.min_request_interval_ms` | `0` | Minimum delay between outbound requests. |
| `runtime.max_review_calls` | unbounded | Physical review LLM attempts per target per run, including escalation, transport retries, malformed-grade reattempts and comparative deduplication. Cache reuse consumes none. |
| `runtime.max_search_calls` | unbounded | Physical live search attempts across the run, including retries and fallback/recovery searches. Cache reuse consumes none. |
| `retry_policy.max_attempts` | `3` | Total attempts per request. |
| `retry_policy.initial_backoff_seconds` | `1.0` | Attempt N waits `initial · 2^(N−2)` … |
| `retry_policy.max_backoff_seconds` | `8.0` | … capped here. |

### `selection` and `link_prioritization`

Selection decides which search results and crawled links are downloaded.
Configured URLs (`sources`, target `urls`) bypass it and never count against a
target's caps.

| Key | Default | Meaning |
|-----|---------|---------|
| `max_per_target` | `1` | Acquisition count per target. Must be ≥ `document_review.keep_top`. |
| `max_per_host_per_target` | `0` (off) | Host-diversity cap per target. |
| `require_any` | — | Candidate text must contain one of these terms. |
| `exclude` | — | Terms that disqualify a candidate (also checked on the final URL at download). |
| `exclude_url_patterns` | — | Case-insensitive regexes on the URL. |
| `exclude_text_patterns` | — | Case-insensitive regexes on candidate text. |
| `exclude_draft` | `true` | Apply `draft_patterns`. |
| `draft_patterns` | — | Domain-authored draft/non-final regexes (there is no built-in list). |
| `require_supported_document` | `true` | Keep only [eligible URLs](#document-types-and-limits). |
| `target_require_any` | — | Templates rendered per target (`"{jurisdiction}"`); the candidate must match one. |
| `target_require_all` | — | Rendered templates the candidate must all match. |
| `target_exclude` | — | Rendered templates that disqualify a candidate. |

| `link_prioritization` key | Default | Meaning |
|-----|---------|---------|
| `mode` | `heuristic` | `heuristic` ranks candidates before download; `off` disables ranking. |
| `top_k` | `0` (no cap) | Optional global ranking cap. |
| `keywords` | `document`, `download`, `guide`, `report` | Terms that raise a candidate's score; replace the fallback. |
| `domain_scores` | — | Domain → score adjustment (`{"municode.com": 0.2, ".gov": 0.15}`). A hostname matches itself and its subdomains; a leading-dot suffix matches every host ending in it; negative scores demote. |
| `shopping_path_keywords` | — | URL path terms that lower a candidate's score. |

The heuristic score weighs file type (35%), `domain_scores` (25%) and keywords
(25%); PDFs are preferred to web pages. Ranking is a boost, never a scope: for
strict sources combine `crawl.allowed_domains` with `include_url_patterns`.
Positive hostname entries (not `.gov`-style suffixes) also name the code hosts
for the two post-review retries of targets left without a curated document: a
SerpApi `site:` search under the code-host path already found, and a browser
crawl of JavaScript-shell code-host pages. Both run only with
`document_review` and `document_classifier.nice_to_have_keywords`; the browser
crawl also needs a browser (`crawl.browser_escalation` or
`crawl.browser_downloads`).

### `document_classifier`

| Key | Default | Meaning |
|-----|---------|---------|
| `required_keywords` | — | Terms looked for in the first pages of each download. |
| `nice_to_have_keywords` | — | Terms that raise the score; also the default `document_review.keywords`. |
| `min_required_matches` | `1` | Required terms that must appear. |
| `action` | `warn` | `warn` records the keyword result; `filter` rejects failing downloads (never configured URLs). |
| `pdf_screening` | `"off"` | `"off"` skips PDF revision screening; `warn` records evidence; `hold` keeps flagged or incompletely inspected PDFs out of automated curation pending human review. Quote `"off"` in YAML so it is not parsed as a boolean. |

PDF screening is independent of keyword relevance and applies to configured URLs
as well as discovered PDFs. Other formats are unchanged. For example:

```yaml
discovery:
  document_classifier:
    pdf_screening: hold
```

The inspector checks all native PDF pages for strikeout annotations and thin
strokes/filled rectangles crossing text. Highlights are recorded as weak evidence
and do not cause a hold on their own; colored text alone is not a revision signal.
It also reuses `selection.draft_patterns` (when `selection.exclude_draft` is true)
against title/subject/keywords metadata and full-line short headers, not arbitrary
body sentences. There is no built-in draft vocabulary. Scanned/image-heavy,
encrypted, corrupt, or otherwise incompletely inspected PDFs are held in `hold`
mode, without OCR/vision calls. Text is never edited to remove crossed-out clauses.

The manifest's `document_eligibility` contains versioned, content-hashed evidence,
page coverage and limitations. `review.csv` and `download_index.csv` summarize
eligibility and effective `curated_selected`. A hold is not a download/provider
failure and does not consume a review top-N slot. Set `human_decision` to `keep` or
`reject` in the existing ledger and run `psweep curate --config …`; explicit human
decisions override automated screening. Unresolved held originals and text caches
are retained even with `retention.documents: curated` or `none` so they can be reviewed.

Policy or detector changes invalidate discovery completion reuse. Existing curated
files are reconciled on the next discovery run; if partial acquisition would retain
an unchecked prior PDF, publication stops rather than silently passing it to
extraction. Complete discovery and resolve holds before extracting from that corpus.
Direct-path extraction and extraction without rediscovery deliberately bypass this
screening; it is not a second extraction filter.

**No findings does not mean legally final or currently in force.** An adopted
amendment can have strikeouts, and a clean-looking proposal can have none. Holds
request human verification, not a determination of legal status. Use `warn` or
leave screening off in domains that need proposals and change histories.

### `document_review`

Optional, paid LLM curation; it runs only when `document_description` is set.

| Key | Default | Meaning |
|-----|---------|---------|
| `document_description` | — | What a correct document is; rendered separately per target with required `{field}` and optional `{field?}` templates. Missing required/malformed tokens fail preflight. |
| `context_fields` | identity only | Extra roster metadata columns supplied as scope hints (e.g. organization, address). Names must exist; blank hint values are omitted. Does not change `partition_by` or target IDs. |
| `model` | [default model](#environment-variables) | `models:` alias or model name. |
| `escalate_model` | absent/off | One stronger semantic grading decision for grade-2 ambiguity or failed/malformed initial grading, subject to actual-attempt budgets. |
| `keep_top` | `1` | Primary documents kept per target. |
| `coverage` | absent/off | Domain-defined label → evidence-description map. Descriptions support target templates. Verified labels guide complementary source selection and trigger fallback for missing coverage. Labels use letters/digits/`_`/`-`, starting with a letter. |
| `max_chars` | `12000` | Text sent per document. |
| `keywords` | classifier keywords | Terms used to pick the excerpt sent for review. |
| `deduplicate_redundant` | `true` | Drop redundant copies before keeping the top ones. |
| `deduplication.key_fields` | — | Fields that identify redundant copies (e.g. `[path_basename, review_doc_kind]`). |

The reviewer grades each document 0–4 against an anchored rubric. Grades3–4
must satisfy every mandatory requirement in `document_description`; 'partial'
means missing optional detail, not missing required evidence. The reason quotes
the deciding source content or identifies what is absent. In
`review.csv`, `relevance` = grade / 4 and `llm_is_primary` = grade ≥ 3;
selection keeps at most `keep_top` non-redundant primaries per target. Without
coverage, grades and reproducible provenance break ties. With coverage, newly
covered verified labels take priority, then grade; supported publication dates
break ties only for matching scope/claim-kind and compatible date precision.
Grades are cached by content and grading inputs in
`discovered/.review_cache/` and reused across runs. Recordings are graded on
their transcript: a run's copy inherits the curated copy's transcript (matched
by SHA-256) before review. A recording not yet transcribed (first run) is kept
without review and transcribed at extraction. Edit `human_decision` (`keep`/`reject`) in `review.csv` and run
`psweep curate --config …` to republish the curated selection; the next
`psweep extract` withdraws the records of documents it dropped.

### Automatic qualification and bounded recovery

Review receives structured target identity plus configured context fields as
untrusted hints, never proof of a source fact. Templates change the description
per target without changing the data contract. Source text must establish the
specific entity/scope. Grades0–1 reject; grades3–4 qualify; grade2 can escalate
when `escalate_model` is configured. Exhausted provider routes are not repeatedly
hammered during that run: eligible escalation is used or work remains pending.
Successful initial grades are reused when a failed escalation resumes.
Document/request-specific rejections do not disable unrelated requests on that
route. Comparative deduplication can also use the configured escalation route
after an exhausted initial-route failure, with both attempts budgeted and metered.

With `search.fallback_queries`, discovery runs at most one extra stage for
weak targets below `document_review.keep_top` distinct qualified sources or
missing configured coverage labels, including targets with zero downloads.
It does not infer field-level gaps from
schema field names. Pending search/download/review failures must finish first;
provider outages do not launch search loops. Combined initial/fallback sources
are deduplicated by exact content/final URL, globally top-N selected and subject
to host diversity. Configured target URLs explicitly bypass relevance review
and are not counted as automatically verified evidence.

```yaml
discovery:
  queries: ['"{name}" "{area?}" technical specification']
  search:
    cache: true
    fallback_queries: ['"{name}" "{area?}" manufacturer installation record']
  document_review:
    document_description: "Direct specification evidence for {name} at {address?}; not a sibling asset."
    context_fields: [organization, address]
    model: secondary
    escalate_model: primary
    keep_top: 3
    coverage:
      specification: "An explicit capacity and unit for {name}."
      applicability: "A direct statement of which variant/site the specification covers."
  selection:
    max_per_target: 8
    max_per_host_per_target: 2
  runtime:
    max_review_calls: 12
    max_search_calls: 200
```

Limits above are examples, not a recommendation for every domain. Review-call
limits apply per target per run; search limits apply across the run. Both count
physical requests/retries, including failures with unknown pricing, and cache
hits are free. Guard exhaustion sends no request and leaves resumable pending
work. These bound attempts, not dollars when provider pricing/usage is unknown.

Adaptive search persists query results even when ordinary `search.cache` is
disabled (reported in notes) so interrupted stages can resume without repaying
valid queries. Configured TTL remains honored; expired responses may need a
new request. Completed unchanged targets still skip work. Low source coverage
after exhausting fallback is reported separately from an interrupted failure.
Adaptive dry runs/config checks make no paid calls. Without fallback,
discovery dry-run search semantics remain unchanged.

### Grounded review evidence and bounded selection

The existing grading call returns `review_evidence`: identity/content quotations,
quoted scope and claim-kind descriptions, optional quoted source-publication date
and precision, and quoted configured coverage labels. Accepted grades3–4 require
identity/content and scope/claim evidence that occurs in an actual sampled segment.
Quotes cannot bridge disconnected sample windows or borrow target metadata.
Invalid/fabricated evidence follows the existing failed-review/escalation path;
old grade contracts are invalidated by prompt/schema identity. This verifies text
occurrence, not entailment, factual truth or document-wide completeness.

Source dates are calendar-valid ISO values with day/month/year precision, tied
to quoted text; unknown dates remain unknown. Download timestamps never substitute
for publication dates. Direct/reported statements and scope stay source-specific
hints in the extraction record and reconciliation input; they are not automatically
promoted to achieved facts or a domain-independent authority ranking.

Comparative dedup uses verified source evidence and requires references for keep/drop
decisions. Complementary labels, differing scopes/history, conflicting claims or
unsupported/cyclic decisions cannot silently erase a source. Structural keys group
comparisons but differing content is not dropped on key equality alone. Exact-byte
and URL mirrors remain cheap to deduplicate.

Finite `runtime.max_files` allocates new ordinary fetch opportunities round-robin
across owners, retaining their ranking. Configured/reused outcomes stay free;
unbounded file acquisition retains its existing order. Fallback queries likewise
run by query slot across weak targets before the next slot, preventing early targets
from consuming every remaining request. Partial budgets/HTTP/provider failures
remain pending; exhausted missing coverage is distinct and reported in compact
target summaries. One source can cover multiple labels; labels do not bypass
mandatory qualification or automatically exceed `keep_top`/host limits.

Checkpoint verification memoizes actual hashes only within a load by file identity
and stat fingerprint, so shared artifacts are hashed once rather than once per owner.
It rejects symlinks, missing/changed files and changes during hashing; there is no
persisted stat-only trust across runs. Disk review caches avoid retaining duplicate
successful decisions in long-lived memory; direct no-cache reviewer use can still
retain decision/failure memos for its instance lifetime.

No manually verified per-location URLs/aliases are required. Domain-specific
source preferences, scope and query wording stay in YAML; schema fields and
validity rules remain the extraction data contract. Qualification grades and
quote presence do not independently establish truth or an accuracy threshold.

### `policy` and `retention`

| Key | Default | Meaning |
|-----|---------|---------|
| `policy.robots_mode` | `warn` | `ignore`, `warn` or `enforce` robots.txt (matched on the `ParseSweep` token). |
| `policy.tos_mode` | `warn` | `ignore`, `warn` or `enforce` terms-of-service checks. |
| `policy.acknowledged_tos_domains` | — | Hosts whose terms you have reviewed. |
| `retention.documents` | `all` | `all` keeps every download; `curated` deletes the run's non-curated downloads; `none` also deletes the run's documents and curated copy (rejected by `run` when extraction follows). |

`curated` and `none` keep unresolved PDF-screening holds as well as the files and
text caches of incomplete targets and sources, which the next run resumes from.
Superseded run copies are removed once the checkpoint points at the retained
current copy. Fully reused runs delete nothing.

Strict robots/ToS enforcement covers HTTP acquisition only and fails before any
browser navigation. The default `User-Agent` is
`Mozilla/5.0 (compatible; ParseSweep/<version>)`.

## Extraction

Extraction reads `outputs/<domain>/discovered/curated/` (or a path passed
positionally) and writes one JSON record per document to
`outputs/<domain>/extracted/`. It needs LLM credentials.

| Key | Default | Meaning |
|-----|---------|---------|
| `model` | [default model](#environment-variables) | `models:` alias or model name. |
| `provider` | `auto` | `openai`, `azure`, `anthropic`, `gemini` or `auto`. |
| `transcription_model` | — | `models:` alias that transcribes audio; required when audio is present. |
| `limit` | — | Process only the first N documents. |
| `skip_existing` | `true` | Reuse verified records (see [Reuse](#runs-reuse-and-exit-codes)). |
| `max_context` | `600000` | Characters of document text sent per call. |
| `max_concurrent` | `4` | Documents extracted in parallel (also concurrent transcription chunks). |
| `timeout_seconds` | `120` (`LLM_TIMEOUT`) | Per-request LLM timeout. |
| `ocr_corrections` | — | `[{pattern, replacement, ignore_case}]` regex fixes applied to OCR text only. |
| `pages` | — | Page selection for large documents (below). |

### Page selection

```yaml
extraction:
  pages:
    csv: page_ranges.csv          # file_path,start_page,end_page (1-based, inclusive)
    auto_locate:                  # LLM fallback for documents the CSV does not cover
      section_description: "the residential rate schedules"
      trigger_chars: 200000       # default; only larger documents are targeted
      max_selected_pages: 30      # default; total, referenced pages included
      keywords: [rate schedule]   # optional cheap pre-filter
      model: secondary            # optional locator model
      save_discovered: true       # write found ranges for review
```

`pages` accepts only `csv`, `auto_locate` and `range`, and `auto_locate` only
the keys shown plus `enabled`; anything else fails validation. `pages.csv`
resolves against the config folder (the `--pages-csv` CLI flag against the
working directory). A row's `file_path` matches a document by path, by path
relative to the input folder, or by file name; give a file one row per range.
CSV rows win; leave both pages empty to force the full document. `pages.range`
(or `--pages 27,68-73`) selects pages for a single file: comma-separated pages
and inclusive ranges.

Targeting normally runs within each bounded extraction worker, so a slow
locator doesn't hold up every other document. A fresh run with existing output
performs bounded targeting preflight before deleting old records. Its `model`
is independent of `extraction.model`; when omitted it inherits that model.
Target-scoped selections retain subject identity/header context. The subject
comes from the last configured `partition_by` field (the roster's name field),
not from parsing a display label or guessing metadata fields. Discovery labels
and metadata remain untrusted scope hints, never factual evidence. Cache
identities include the configured identity, subject hints and locator behavior
version. HTML/HTM targeting uses readable
section text against `trigger_chars`, not raw markup size, so scripts/styles
and navigation do not turn a short article into a paid locator call. Locator
failures fall back to full-document extraction, subject to `max_context`.

`auto_locate` shows the locator model the opening lines of every page and asks
for the pages of the described section. It then reads those pages in full and
adds the parts they refer to elsewhere in the document, such as "charges
specified in Tariff Sheet 75" or "riders on Sheet 80", and repeats with the
added pages until nothing new is referenced. `max_selected_pages` caps the
total. Relevant pages it leaves out are logged as a warning and recorded in the
record's `coverage.omitted_page_ranges`. The extracted text marks each skipped
stretch with `[Omitted pages: 28-67]`.

Selections are cached in `.pages/` beside the document. If the section cannot
be located, the full document is extracted. If a reference pass fails, the
pages found so far are used and the document is located again on the next run.
With `save_discovered`, `extracted/discovered_page_ranges.csv` (the `pages.csv`
columns, one row per range) lists the pages behind every current record, reused
records included. Non-PDF selections are saved under `extracted/.sections/` as
each document finishes; completed section text is then released from memory,
whether saving is enabled or not.

### LLM calls

Every transient provider failure — HTTP 408, 429 and 5xx, timeouts and dropped
connections — is retried up to 4 attempts in total with exponential backoff
(2 s, capped at 30 s). TLS failures and other 4xx errors are terminal. Each
retry logs a warning with the model, the failure category and the attempt,
prefixed with the document being processed; provider error text is never
logged.

### Roster-anchored extraction

When the schema's `identity.deduplication.key_fields` equal
`discovery.partition_by`, extraction runs in roster mode: entity names in the
text are matched exactly (after normalization) against the roster's last
`partition_by` field. A name shared by targets in different states is assigned
by state evidence near the match, falling back to the whole document:
addresses (`, AZ` or `AZ 85001`), full state names, state-labelled table cells
(`State: CA`), or a state code alone on the line after a line that is exactly a
roster name. Without evidence every candidate is kept. Names unique to one
target are never dropped. Partial overlap between `key_fields` and
`partition_by` is an error.

### Audio

Recordings (`.mp3 .m4a .wav .ogg .flac`) are documents. Set
`extraction.transcription_model` to a `models:` alias of a transcription
model. ffmpeg (installed by pixi) transcodes each recording to mono 16 kHz MP3
and splits it into ~5-minute chunks cut at the quietest point; chunks are
transcribed in parallel and each starts with an exact `[hh:mm:ss]` offset.
Measured cost is about **$0.22 per audio hour**. Transcripts are cached beside
the recording in `.text/<filename>.txt` (with `.meta.json`), like OCR text, so
each file is transcribed once.

## Compilation

Compilation merges `outputs/<domain>/extracted/` into
`outputs/<domain>/compiled/<domain>.csv` / `.xlsx`. By default it makes no LLM
calls; optional roster reconciliation below uses paid calls only for disputed
or schema-inconsistent answers.

| Key | Default | Meaning |
|-----|---------|---------|
| `output.default_format` | `both` | `csv`, `excel` or `both`. Validation needs a CSV. |
| `output.column_order` | — | Priority prefix of output headers; other columns follow. |
| `output.exclude_fields` | — | Headers to drop. |
| `output.freeze_columns` | — | Excel frozen columns. |
| `output.auto_width` | — | Excel column auto-width. |
| `output.annotation_column` | `Notes` | Column that deduplication annotates. |
| `output.center_align_columns` / `center_align_max_length` | — | Excel centering. |
| `normalization.state_column` | — | Field normalized to a state abbreviation. |
| `normalization.county_fips` | — | `{state, county}` fields used to add a county FIPS code. |
| `severity_tokens` | — | `{high: [...], medium: [...]}` field-name tokens that rank suspicious dedup groups. |
| `dry_run` | `false` | Preview deduplication without writing. |
| `report_format` | `text` | Dry-run report: `text` or `json`. |
| `fail_on_suspicious` | `none` | `low`, `medium` or `high`: fail the dry run at this severity. |
| `provenance_policy` | `warn` | `warn` or `fail` on provenance warnings. |

Headers are the schema field's `title` (or its name); unknown headers fail.
Row shape (`$metadata.compilation.flattening`) and deduplication keys belong in
the schema — see [Schema Authoring](schemas/best-practices.md).

**Roster mode.** When `discovery` declares `targets` and `partition_by`,
compile emits one row per target in roster order. Agreeing values win; conflicts
prefer target-scoped sources, then the value backed by most sources; missing
values (null, blank, or an empty list or object) are `NA`. A list of primitives
becomes one comma-joined cell; other lists and objects become compact JSON.
Extra target columns pass through, plus `sources`, `confidence` (`high` ≥ 2
agreeing sources, `medium`, `low` on conflict, `none`) and `evidence`
(`{field: {value, url}}`, adding the `quote` of each
[evidence field](schemas/best-practices.md#evidence-fields) and
`conflict: true` when sources disagree).

Excel batches above 1,048,575 rows or with cells over 32,767 characters fail
before export; use CSV for large outputs.

### Optional evidence reconciliation

`compilation.reconciliation` is opt-in and requires roster mode. It selects
existing source observations, rather than generating replacement facts. Keep
fields, descriptions and ordinary JSON Schema constraints in the schema; put
resolution behavior only in YAML:

```yaml
compilation:
  reconciliation:
    enabled: true
    model: primary
    source_date_field: source_provenance.document_date
    field_groups:
      - [amount, unit]
      - [status, status_description]
    instructions: >-
      Prefer statements about the queried asset over related assets. Distinguish
      achieved history from superseded plans and abstain where scope is unclear.
```

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | Enable atomic, evidence-preserving roster reconciliation. |
| `model` | extraction model | Existing `models:` alias or concrete model. |
| `source_date_field` | absent/unknown | Exact root-relative payload path to the source's publication/update date, optionally a `{value, quote}` field. Invalid or absent dates remain unknown; extraction time is never used as publication time. |
| `field_groups` | singleton item fields | Disjoint lists of exact main-item field names selected together from one original item. Identity fields cannot be grouped. Structured/evidence values are already indivisible. |
| `instructions` | generic evidence rules | Domain-authored interpretation guidance, not new fields or generated facts. |
| `max_context` | `100000` | Character budget for full prompt, competing observations, schema and response contract. Oversized targets remain unresolved; competitors are not silently truncated. |

Agreeing schema-valid answers need no model call. Disputed units or an invalid
assembled item trigger one logical request per target (transient provider
retries still apply). The model returns supplied observation IDs or abstains;
IDs, atomic selections and assembled item constraints are validated before
export. A structurally invalid decision gets at most one schema-guided repair
request using the same evidence and budget; valid abstentions are not retried.
Failure diagnostics name fields/constraints without echoing provider content.
Both requests and provider retries are included in metered accounting.
Unresolved claims are `NA`, not an inconsistent majority-vote fallback.
Nonnullable requirements that prevent abstention remain explicit failures.
A partial result exits 3; no usable answers with failures exits 1. Dry runs and
config checks make no calls or cache writes and report pending targets.

Per-field `evidence` retains value/quote/URL/conflict and adds source date,
observation ID and resolution state. `_reconciliation` contains the reason,
unresolved units and a `decisions/<hash>.json` alternatives reference relative
to `outputs/<domain>/.reconciliation/`. These sidecars retain original competing
observations without growing spreadsheet cells past Excel limits. Confidence
is support bookkeeping, not certified truth or automatic model confidence.

Verified content-addressed decisions and durable attempt receipts live in
`.reconciliation/`; identity includes the observations, schema, instructions,
groups, resolved model/routing and algorithm version. Cached choices are
revalidated and reused without credentials or new spending; cached abstentions
remain visibly unresolved. Provider/budget/invalid-answer failures are not
reused as authoritative decisions. `compile --fresh` replaces exported tables,
not verified decisions. Actual calls, retries, unknown-price calls and failures
are included in accounting, even when a later export fails.

This does not automatically discover missing sources, turn publication recency
into source authority, distinguish phases absent from the contract, or detect
semantic contradictions expressed only in prose when all supplied values agree.
Group related facts and describe scope/phase distinctions in the existing
schema and YAML instructions; rows mode intentionally rejects this option.

## Validation

`validate` scores the primary table (from `extraction.model`) against
contender models and human reference sheets. `run` adds it when a
`validation:` block exists.

| Key | Meaning |
|-----|---------|
| `models` | Contender `models:` aliases, extracted and compiled into `validated/<alias>/`; must differ from `extraction.model` and each other. |
| `references` | 0..N human CSV sheets (config-relative) whose headers are compiled titles; labelled by file stem. |
| `judge` | `models:` alias that rules on disputed cells only; omit to skip judging. |
| `fields` | Scored subset (default: every scorable field). |
| `min_accuracy` | Gate in (0, 1]: exit 1 when the primary falls below it against any reference. Needs `references`. |
| `key_fields`, `quote_field` | Rows mode (no roster) only: row identity (required there) and each row's verbatim quote. |

### Precision, coverage and reference labels

Validation reports populated-answer precision (`correct_populated /
assessed_populated`), answer coverage (`populated_answers / answer_opportunities`)
and reference-supported recall alongside existing assessed-cell accuracy.
Unknown labels do not reward abstentions. In reference CSVs:

- A populated cell is assessed by default; explicit `NA` labels known absence.
- An empty cell is unlabelled, unless optional `<compiled title>__assessed=true`
  explicitly labels absence.
- `<compiled title>__assessed=false` excludes any value as unknown/unassessed;
  no domain-specific magic unknown word is reserved. Nonblank masks must be
  `true` or `false`.

`min_accuracy` remains an assessed all-cell accuracy gate, not populated-answer
precision. Model agreement is labelled separately and is not ground truth.
Counts include missing/no-answer targets and expose unassessed populated claims,
reference contradictions and evidence conflicts. Exact-target source precision
still needs a separate sampled source audit; reviewer labels alone cannot certify it.

Per-field Wilson intervals are provided in roster mode (one cell per target).
Pooled cell intervals are null because fields/rows from one target are correlated.
Target-level summaries provide answer/no-answer/partially assessed counts and
an all-answered correctness interval over fully assessed answered targets.
Those denominators must be read with coverage; blanks cannot establish an 85%
precision claim. Processing time and billed extraction cost per target are
amortized record-only means, not complete discovery spend or target wall time.

For a defensible rollout, freeze a representative random/stratified holdout
without manually seeded references, separate it from tuning cases, and label
source-supported answers. Report status/milestone results and coverage separately,
with denominators and uncertainty. Architecture or provider/model choice is not
proof of greater-than-85% performance.

At least one of `models` or `references` is required. Cells where every
contender agrees are not judged; the judge sees only disagreements, checking
each candidate against its quote. Misses are labelled `no_docs` (no curated
document) or `extraction`. Accuracy carries a 95% Wilson interval.
`validate --sample N [--seed S]` writes a blank, seeded labelling sheet with no
LLM calls. Outputs in `outputs/<domain>/validated/`: `cells.csv`,
`summary.csv`, `summary.json`.

## Document types and limits

Supported inputs (discovery downloads exactly these; extraction reads them):

| Kind | Extensions | Read as |
|------|-----------|---------|
| Documents | `.pdf` `.doc` `.docx` `.txt` | Text, with OCR for scanned PDFs |
| Web pages | `.html` `.htm` | Main text |
| Data | `.csv` `.xlsx` `.json` | One `Header: value \| …` line per record (CSV, each sheet, JSON record arrays such as Socrata/ArcGIS/GeoJSON/CKAN); other JSON as indented text |
| Audio | `.mp3` `.m4a` `.wav` `.ogg` `.flac` | Transcript ([Audio](#audio)) |

**URL eligibility** (`selection.require_supported_document`): a URL with a
supported extension, no extension, or a suffix unknown to Python's MIME table
(`.php`, `.aspx`, `.cfm`, …) is eligible — the response MIME type decides what
it is. A suffix of a known unsupported type or encoding (`.jpg`, `.zip`, `.gz`)
is rejected before download.

**Byte limits.** Files streamed to disk stop at 1 GiB decoded
(`PSWEEP_MAX_RESPONSE_BYTES` overrides it) and abort after
`runtime.timeout_seconds` without data. Pages, search and API responses read
into memory stop at 50 MiB and must finish within the timeout. An oversized
response is a failed download (`partial_failure`), never retried; raise the
limit and rediscover with `--fresh`.

## Runs, reuse and exit codes

| Status | Exit | Meaning |
|--------|------|---------|
| `success`, `reused`, `dry_run` | `0` | Done, verified reuse, or a valid plan. |
| `failed` | `1` | The stage failed. |
| `empty` | `2` | Nothing usable and no failure. |
| `partial_failure` | `3` | Usable output plus recorded errors. |

`run` stops at 1 or 2, continues past 3, and exits 3 at the end when any stage
was partial. Exit codes do not depend on `-q`/`-v`.

**Discovery resume.** The checkpoint (`discovered/checkpoint.json`) records
each target and shared source separately, keyed by the effective config
fingerprint and SHA-256 evidence of its files. Adding a target fetches only
that target; changing a target's `urls` or rendered queries re-runs it.

- A **complete** item makes no requests on the next run. Fully reused runs write
  an audit snapshot, not another `documents/` or `curated/` copy.
- Extraction/page-locator model changes do not invalidate discovery. Only the
  resolved model used by configured `document_review` affects its fingerprint.
- When acquisition settings or a target's inputs change, discovery re-evaluates
  that work but reuses matching URL/owner downloads from verified local bytes;
  it fetches only new, missing or changed documents. Classification and review
  are re-applied under the current settings. Use `--fresh` to fetch remote
  changes at already-downloaded URLs, including changed redirect destinations.
- An item left **incomplete** — transient failure, unfinished crawl or
  search, `max_pages` or `max_files` reached — publishes what it has
  additively, and the next run re-fetches the listing, reuses every earlier
  download whose SHA-256 still matches (no request, no `max_files` slot),
  skips terminal outcomes (e.g. 404) and fetches only the rest. A capped
  listing converges batch by batch.
- **Transient** failures (timeouts, connection errors, truncated streams, HTTP
  408/429/5xx) are retried in the run and leave the item incomplete. **Bot
  challenges** (429/5xx with `cf-mitigated`, `x-vercel-mitigated` or
  `x-amzn-waf-action`) are retried in the run but are final across runs. TLS
  errors and other 4xx responses are terminal.
- `discover --fresh` ignores the checkpoint and search cache, re-checks
  completed listings (new children of a completed listing need it), and
  replaces the curated partitions of the targets it processes.

**Extraction reuse.** A record is reused only when source, schema, resolved
model and settings, and record bytes still match; otherwise it is re-extracted.
Reuse needs no credentials. A record whose source document no longer exists
under the input folder is withdrawn: extract deletes it, names it in the
summary and lists it under `withdrawn` in the run manifest, so compile drops
its rows. Records from other input folders are left alone, and `validate`
reports a primary with records to withdraw as stale. `extract --fresh` resets
the output after a successful preflight. `compile --fresh` clears stale
compiled files; `validate --fresh` clears `validated/`. `run --fresh` passes
`--fresh` to every stage.

**Caches.** OCR text and transcripts: `.text/<filename>.txt` + `.meta.json`
beside the document, verified by size, mtime and SHA-256 and carried to a
re-downloaded file with the same SHA-256. Page/section targeting: `.pages/`,
`.sections/`. Review grades: `discovered/.review_cache/`. Delete a cache
directory to force that work again.

### Outputs

```text
outputs/<domain>/
├── discovered/
│   ├── checkpoint.json
│   ├── curated/                 # consolidated input to extraction
│   └── runs/<run_id>/           # manifest.json, download_index.csv, review.csv,
│                                # documents/, curated/ (latest -> newest run)
├── extracted/                   # one JSON record per document + manifests
├── compiled/                    # <domain>.csv/.xlsx, run_accounting.json
└── validated/                   # cells.csv, summary.csv, summary.json, <alias>/
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `SERPAPI_API_KEY` | Search. |
| `LLM_BASE_URL` + `LLM_API_KEY` | Any OpenAI-compatible endpoint (highest priority). |
| `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION` | Azure OpenAI. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | Direct providers. |
| `LLM_MODEL` (with `LLM_BASE_URL`), `AZURE_OPENAI_MODEL`, `ANTHROPIC_MODEL`, `GEMINI_MODEL`, `OPENAI_MODEL` | The active provider's default model, used when the config names none. If unset, Anthropic uses `claude-3.5-sonnet`, Gemini `gemini-1.5-pro`, and every other provider `gpt-4o-mini`. |
| `LLM_TIMEOUT` | LLM request timeout (seconds). |
| `PSWEEP_MAX_RESPONSE_BYTES` | Streamed-download byte limit. |
| `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` | Approved CA roots. |

TLS verification is always on by default; the `*_SSL_VERIFY`, protected-mode
and browser variables are described in [Security](security.md).
