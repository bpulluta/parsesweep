# Changelog

All notable changes to ParseSweep are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed (review dedup no longer discards a whole target)
- Redundant-copy detection among qualified documents is now computed in code from the
  verified review evidence, with no LLM call. Documents with equal scope, claim kind and
  content quote are copies; the one with the broadest coverage (ties: best rank, or with
  `prefer_newest` the latest edition) is kept. Before, one malformed LLM dedup answer
  marked every qualified document of the target as failed, and re-runs repeated the same
  failure, so large targets (31 qualified documents) kept nothing.
- Removed `document_review.deduplication.key_fields`. It only hinted the LLM comparison,
  so it is now rejected as an unknown key; delete it from run configs.
- Cache impact: no regrade, grade caches are untouched. Existing `*-dedup.json` review
  cache files are no longer read. Curated sets can shrink slightly where dedup is on,
  because exact evidence duplicates the LLM declined are now dropped; only newly selected
  documents extract. Review cost drops by one call per target, and dedup no longer counts
  toward `runtime.max_review_calls`.

### Upgrade impact vs main
- One paid re-extraction of every document: the consumed-input fingerprint
  (`captured-input-v1`), record contract 3.0.0 and the extraction behavior
  versions all change the fingerprint, and older records are not read.
- One paid re-location wherever page/section targeting is enabled
  (`captured-scope-v4`).
- One local re-OCR of scanned PDFs into the per-page cache (Tesseract, no LLM
  cost).
- No discovery regrade.
- No image cost for `input_mode: text` (the default).
- Older records are not read: there is no legacy layer.

### Docs (README refresh)
- `README.md` is shorter and ordered for first-time readers: value statement,
  what you get, install, quickstart, how it runs, your own domain, docs table.
  The outputs tree and default values moved to the docs it links. TEMPLATE is
  described as the search + review starter. `tests/test_readme_links.py` checks
  that every relative link in the README points at a tracked file.
Cost/cache impact: none.

### Docs (required keys, optional features, convergence loop, starters)
- `docs/config-reference.md`: new "Required keys, defaults and optional
  features" section (required keys per stage, optional features by need with
  cost notes, advanced keys).
- `docs/tuning.md`: new "Converge a new domain" loop; `fallback_queries` and
  `document_classifier.action: filter` named as cost levers.
- `config/TEMPLATE.yaml` is now a search + review starter: `max_per_target: 4`,
  `keep_top: 2`, `document_review` on, `validation` commented out. A test pins
  that it restates no built-in default. `config/README.md` names both starters;
  `config/example/run.yaml` says its cost is extraction only. Supersedes the
  comment edits of PR #28.
Cost/cache impact: none for existing domains; new domains copied from TEMPLATE
start with review on and validation off.

### Changed (built-in defaults are single-sourced)
- Review defaults (`keep_top`, `max_chars`, `deduplicate_redundant`,
  `prefer_newest`), classifier defaults (`min_required_matches`, `action`,
  `pdf_screening`), discovery's five per-operation network timeouts, the LLM
  client timeout, the Anthropic/Gemini fallback models, the compile output
  format and the provenance policy are now one named constant each instead of
  repeated literals. `tests/test_defaults_single_source.py` pins them.
- `--show-runtime-catalog` quotes the extraction concurrency and timeout from
  their constants, and lists `max_review_calls` and `max_search_calls` under
  `discovery.runtime`.
- `docs/config-reference.md` lists discovery's per-operation timeouts.
- Cost/cache impact: none (no behavior change).

### Changed (searches are cached by default)
- `discovery.search.cache` now defaults to `true` (`DEFAULT_SEARCH_CACHE`). Set
  `cache: false` for always-live searches; `--fresh` still skips reads.
- Cost/cache impact: fewer paid SerpApi calls on re-runs, tuning and
  dry-run-then-run; no discovery re-evaluation (cache settings are excluded
  from the discovery fingerprint); configs that set cache: true may drop it.

### Changed (600 s LLM timeout)
- The built-in LLM request timeout is 600 s (was 120 s). `LLM_TIMEOUT` and
  `extraction.timeout_seconds` still override it.
- The validation judge now defaults to the shared extraction concurrency
  (4 workers, was 1) when `extraction.max_concurrent` is unset.
- The docs warn that `extraction.timeout_seconds` and
  `pages.auto_locate.save_discovered` are part of the reuse fingerprint.
- Cost/cache impact: fewer paid full-prompt retries on long requests; no
  re-extraction (the reuse fingerprint records only a configured
  extraction.timeout_seconds). Judge concurrency is speed only; the judge
  cache is unchanged.

### Changed (priced provider fallback models)
- The Anthropic and Gemini environment fallbacks are now `claude-haiku-5-5`
  and `gemini-3.1-flash-lite`, and a test asserts every built-in default model
  has a known price in the pinned catalog.
- Cost/cache impact: only setups that rely on the Anthropic or Gemini
  environment fallback (no model in config, no ANTHROPIC_MODEL/GEMINI_MODEL)
  change model, so their documents are re-extracted and re-graded once (paid);
  configs that name models are unaffected.

### Removed (extraction.skip_existing)
- The YAML key `extraction.skip_existing` is gone. Reuse is always on and
  `extract --fresh` re-extracts. The Python API parameter
  `extract_documents(skip_existing=...)` is unchanged.

Cost/cache impact: none; a config that sets the key now fails validation;
delete the line. No local domain sets it.

### Removed (selection.require_supported_document)
- `discovery.selection.require_supported_document` is gone. The
  supported-document filter (rejects known-unreadable suffixes such as `.jpg`,
  `.zip`, `.gz`) is always on, because `false` only let unreadable downloads
  through.

Cost/cache impact: every domain's discovery fingerprint changes once (a request
field is gone). On the next discover each target is re-evaluated: cached
searches, verified local downloads and cached review grades are reused, so no
paid search or grading for documents already seen. Local configs that set the
key must delete the line.

### Added (effective defaults are visible)
- `--show-effective-config` on `discover`, `extract` and `compile` now also lists
  the built-in default of every setting you did not set, labelled `(default)`;
  unset limits print `none`. The defaults come from the same constants and
  `DiscoveryRequest` fields the run uses.
- `psweep config --show-runtime-catalog` re-levels keys: `document_review`,
  `selection`, `search`, `runtime`, `document_classifier`, `retention` and
  `extraction.max_context` are `optional`; `extraction.limit`,
  `extraction.provider`, `compilation.dry_run` and `compilation.report_format`
  are `advanced`. `targets` and `sources` note that one of them is required.
- Cost/cache impact: none.

### Added (cost visibility before paid stages)
- The `psweep run` plan lists the paid stages before anything runs: an upper
  bound on SerpApi queries (before the cache) with the cache state and
  `max_search_calls` ceiling, the review model with its downloads per target,
  `keep_top`, escalation model and `max_review_calls`, the extraction and
  page-targeting models, the reconciliation model, and a `Price unknown` row
  naming every resolved model with no catalog price.
- `run` warns before extraction when discovery ended partial, naming the number
  of incomplete targets in the checkpoint, and then continues as before.
  `execute_run_stages` takes an optional `on_partial` callback.

Cost/cache impact: none (output only).

### Fixed (estimate inventory and model)
- `psweep estimate <dir>` counts the documents extraction reads (no dot-directory
  sidecars such as `.text/`, `.pages/`, `.sections/`, no `.section_target.txt`)
  and samples them in path order, so the figure is stable across runs.
- `psweep estimate --config <run.yaml>` prices `extraction.model` resolved through
  the config's model registry; without it the environment model is still used.
- Docs state that `discover --dry-run` searches live (paid unless cached), that
  `document_classifier.action: warn` still sends keyword failures to paid review,
  and that `parser_partial` and `targeting_incomplete` records are re-paid on every run.

Cost/cache impact: none.

### Fixed (section targeting: short DOCX no longer calls the locator)
- A `.docx`/`.doc` whose file size exceeds `trigger_chars` but whose readable text
  does not now skips the section-locator call, as the docs already described
  (image-heavy documents were sent to the locator on raw size alone).

Cost/cache impact: fewer locator calls for image-heavy DOCX; no re-extraction of
already-targeted documents.

### Added (one `--domain` workspace override on every config-driven stage)
- `extract`, `compile`, `validate` and `run` now take the `--domain` that only
  `discover` had, so a second workspace (for example `aq_permits_va_s12` from
  `config/aq_permits_va/run.yaml`) is addressed the same way at every stage
  without `-o`, which recorded the wrong domain and broke compile and validate
  chaining. The override is applied once, in `load_runtime_config_file`, so
  every reader of the domain agrees; `run --domain` forwards it to every stage.
  `curate` and `gc` keep their positional domain argument (the same key).
  Without the option nothing changes. No cache or regrade impact.

### Fixed (search budget: a target's query slots run in order)
- Under `runtime.max_search_calls`, a fallback pass dispatched every query-slot job
  concurrently, but a target's jobs share one pre-reserved allocation. A slow
  "first query" job could lose its unit to the same target's "second query", so
  which query ran depended on thread timing (intermittent
  `test_fallback_budget_visits_first_query_for_every_owner_before_second`).
  Each target's jobs now run back to back in slot order while targets still run
  concurrently, so a tight budget deterministically visits every owner's first
  query before any second query.
- Cache impact: none. No regrade, no re-extraction, no new config keys.

### Fixed (discovery publication: final decisions apply in incomplete partitions)
- A shared partition left incomplete by one retryable failure used to publish
  additively and ignore every other decision, so the consolidated `curated/`
  and `download_index.csv` kept a document the run had finally rejected (a human
  `reject` via `psweep curate`), finally not selected (`keep_top`,
  `max_per_target`) or republished under a new name. Those three decisions now
  apply there too, in `discover` and in `curate`. Unchanged: a retryable failure
  withdraws nothing, a document the run did not decide stays, and "absent from a
  completed target" still waits for the partition to complete. User-visible
  effect: a later `discover` or `curate` can remove documents that previously
  lingered in `curated/`; their extraction records become historical through the
  existing admission rules and nothing under `extracted/` is deleted. No cache
  or regrade impact: no cache key, fingerprint or LLM call changes.

### Changed (document review: diagnosable grounding failures)
- A review grade whose quotes the sampled excerpt does not support now fails with
  the kind and the failing evidence paths (`invalid document grade evidence:
  ungrounded evidence.content_quote`; `contract` when the grade itself is
  malformed) instead of one generic message, in the audit as
  `review_initial_error`. The record's review audit
  (`review_initial_failure`, or `review_escalation_failure`) keeps the kind, the
  paths and each rejected quote truncated to 300 characters, in the local
  discovery manifest only. A run note counts the files that failed grounding.
  Grounding stays exactly as strict. No grade cache key, prompt or sampler
  change: no existing cache is invalidated and no regrade is triggered.

### Changed (document review: one bounded grounding repair)
- When a reviewer's well-formed grade quotes text the sampled excerpt does not
  contain contiguously (a "cleaned" run-together word, `and/or`, a word completed
  across a `[...]` cut), review sends ONE same-model follow-up that repeats the
  rejected quote text and the failing evidence paths and restates the copy rule.
  The system never supplies, snaps or fuzzy-matches candidate text: the reply is
  verified by the same strict matcher. A second failure ends as an unverifiable document (next entry).
  The repair is metered and counts against the review attempt budget; it is not
  used when `escalate_model` is set or after a provider error. A run note counts
  repaired and failed grounding. Cost: at most one extra review call per failing
  grade. Cache: none; the first-attempt prompt and the grade cache key are
  unchanged, and a repaired grade is stored under the same key and re-verified on
  every read like any other grade.

### Changed (document review: unverifiable documents are terminal and never re-billed)
- A document whose grade still fails quote grounding after the repair (or, with
  `escalate_model`, after both routes) is now a settled `review_unverifiable`
  outcome (`review_failed`, `review_retryable: false`, `review_unverifiable:
  true`; the reason is shown in `review.csv` `llm_reason`) instead of a retryable
  failure. Before, the same inputs were re-billed on every discover run and the
  partition never completed. It is stored as a negative entry in
  `.review_cache/` under the grade's own key; it is never read as a grade. The
  document stays held (not curated); `psweep curate` with `human_decision`
  `keep`/`reject` resolves it. Any change to a grading input (model,
  description, prompt, keywords, `max_chars`, sampling id) changes the key, so
  the document is retried automatically. Provider, transport and attempt-budget
  failures stay retryable and write no entry. Worst case: 2 calls per unique
  document per grading-input version. Run notes count repaired and unverifiable
  documents. Cache: no grade cache key changes, no existing cache is
  invalidated and no regrade is triggered.

### Added (selected-page vision, opt-in)
- `extraction.input_mode: text | auto | vision` (default `text`; unknown values
  fail at load). `auto` sends images of PDF pages that neither native text nor
  OCR could read; `vision` sends images of every non-blank selected page. Images
  go through the same request path as text as labeled inline PNG blocks, with
  original page numbers. A route not known to accept images holds before any
  request; image tokens are estimated from PNG dimensions and counted against
  known windows. A record with unreadable pages is never reused, so `auto` and
  `vision` domains pay again on every re-run of such a document.
- Image-only content has no text to verify quotes against; records say so
  (`input` block, `image_only_pages` coverage and a warning). Native PDF
  transport is not implemented.

### Changed (record contract 3.0.0: input provenance)
- Every extraction record has an `input` block: `input_mode`, per-page PDF
  representation (`status`, `chars`, `image_fraction`, and `ocr_sha256` where OCR
  output was obtained), `visual_pages` and `render_dpi`. Coverage adds
  `unreadable_pages` and `image_only_pages`; the `parser_partial` warning names
  the unreadable pages. The always-false `coverage.context_truncated` key is
  removed. `contract_version` is `3.0.0`.
- OCR reuse is proved per page. A record is stale when a present OCR page entry
  disagrees with the digest the record used (or the record could not read that
  page); a missing entry still reuses with no provider call. Transcripts carry
  `derived_text_sha256` in the run manifest.
- The extraction fingerprint binds the prompt/repair behavior version for every
  document and the OCR and page-representation versions for PDFs only; the page
  locator fingerprint binds the same PDF versions.

### Changed (response outcomes, one schema-feedback generation)
- Primary extraction rejects length-terminated (`response_truncated`) and
  refused (`response_refused`) answers as explicit `model_response` failures
  instead of accepting them (a schema-valid `items: []` with
  `finish_reason=length` used to pass). They are never regenerated.
- An empty, non-JSON or schema-invalid answer gets at most one feedback
  generation: the original request, unchanged, plus the rejected answer and
  bounded path/rule diagnostics (no values). It draws on one per-document
  allowance of 4 physical attempts shared with transport retries and the
  temperature fallback; the allowance is document-local. Both generations' cost
  and tokens are in the record and in failures. `schema_diagnostics` is shared
  with reconciliation, whose repair diagnostics also name unexpected keys and
  expected types.
- The extraction prompt says conversions apply only where a schema field
  description explicitly requires them, that unsupported conversions are
  `null`, and that evidence quotes stay verbatim.
- Discovery, reconciliation, locators, judges and transcription keep their
  response handling.

### Changed (per-page PDF representation and OCR cache)
- PDF text extraction decides native text versus OCR per page instead of from
  an aggregate 500-character total, so a raster rate table can no longer be
  hidden behind a readable cover page. Each selected page is recorded as
  `native`, `ocr`, `blank` or `unreadable` with its original page number. Blank
  pages are covered and never OCR-ed; a raster page OCR cannot read is
  `unreadable`, is not counted in `extracted_pages`, and makes the record
  `parser_partial` with a warning. `ocr_corrections` touch OCR pages only.
- The page locator's index OCRs raster pages, so scanned documents can be
  targeted instead of being held as unreadable.
- OCR is cached once per page (`.text/<file>.ocr/<page>.json`), keyed by the
  source SHA-256, page number and OCR engine/render version. The locator index,
  page-range runs and whole-document extraction share it, so a raster page is
  OCR-ed once per source. Entries are raw text written atomically;
  `ocr_corrections` are applied on every read. A corrupt, tampered or
  other-source entry is a miss.
- Discovery carries the OCR page entries with their document: they are linked
  into `curated/`, inherited by a same-SHA re-download (never by a different
  file), protected with a kept or held document, and removed with the document.
  A scanned PDF OCR-ed during discovery review is not OCR-ed again by extraction.

### Changed (scope budgeting, explicit locator)
- Targeting triggers at `min(trigger_chars, max_context)`: a document too large
  for `max_context` is targeted even when under `trigger_chars`. A locator
  candidate index the route cannot admit is split in halves (at most 8
  requests); any unsendable remainder holds the document.
- `extraction.max_context` is enforced on every path, including CSV and
  `pages.range` scope, disabled targeting and unsupported types. Oversized
  input is held (`targeting_context_budget_exceeded`) and the primary is not
  called; input is never sliced to a prefix.
- `pages.auto_locate.model` is required when auto-locate is enabled and must
  resolve to a different provider/model than `extraction.model`; the silent
  inherit-the-primary default is removed. Configs that enabled auto-locate
  without a model now fail validation.

### Fixed (crawl budget starved by excluded links)
- Linked documents that selection eligibility (exclusion terms, URL/text
  patterns, draft filters) drops after crawling no longer consume the raw-link
  budget of `runtime.max_files`. Previously a listing whose first links were all
  excluded could exhaust the budget on every run and never reach a later wanted
  link; the crawl now widens its budget by the number of dropped links, so
  repeated runs converge. No fingerprint or cache change; a budget-limited
  listing may fetch more pages in the same run.

### Fixed (review ledger no longer invents an LLM verdict)
- `review.csv` leaves `llm_selected` blank for documents no relevance review
  graded, instead of defaulting it to `True` for every downloaded file. Use
  `curated_selected` for what was effectively published. `curate` already treats
  a blank `llm_selected` as "no verdict". Ledger-only change: no fingerprint,
  cache, regrade or cost impact.

### Fixed (conflicting page scopes fail at load)
- `extraction.pages.csv` together with `extraction.pages.range` is now a config
  error instead of silently ignoring the range. Configs that set both must drop
  one. Load-time validation only: no fingerprint, cache or cost impact.

### Fixed (central request admission)
- `LLMClient.complete` admits the exact final request (messages plus response
  schema) against `model_context_windows` before any provider attempt, for
  every caller. Extraction and locator calls additionally use LiteLLM's pinned
  local catalog and send the route's output limit as `max_tokens`. Estimates
  are conservative (3 UTF-8 bytes per token). Unknown limits are not guarded: a
  custom route with no known output limit is sent without `max_tokens`, and
  truncation is rejected after the fact by the response-outcome check. A
  rejected request sends and meters nothing. Discovery, reconciliation and
  transcription get no catalog guard or output cap. Very large inputs on known
  routes hold with `context_window_exceeded`.

### Changed (test efficiency)
- The full offline suite and CI retain all distinct regression scenarios;
  two duplicate config-validation parameter cases are consolidated into
  existing stronger checks. Scheduling scale tests no longer collect
  unasserted allocation/timing diagnostics. TLS tests share immutable
  certificate files, keep isolated servers/call histories, and shut down
  without the default half-second polling delay.
- `pixi run --locked -e dev test-fast` is an optional local loop excluding
  explicit `integration`, `slow`, and `live` cases. The ordinary `test` task
  still runs real CLI/TLS/ffmpeg checks and the largest scale cases. Pytest
  defaults to concise output and rejects unknown markers; `-v` remains available.

### Fixed (extraction admission and outcomes)
- Preserved historical records no longer enter a current compilation.
  Extraction publishes an atomic `run_manifests/current_admission.json` with a
  per-document disposition (`new`, `reused`, `failed`, `deferred`) and the exact
  record digest, source digest and input fingerprint for admitted records,
  scoped to the invocation (before `limit`) and merged with prior qualification.
  Failed or deferred documents keep their old bytes on disk but are not
  admitted; succeeded and verified-reused siblings of a partial run are.
  Previously a changed document that failed re-extraction compiled alongside
  new siblings with no warning. A run marks its pending documents `deferred`
  before rewriting records, so an interrupted run excludes them until
  `psweep extract` is rerun. Extraction holds a per-output single-writer lock,
  and records judged reusable at preparation are re-hashed under it.
- Compilation, dry-run, `compiled_table_fresh`, provenance and the build
  fingerprint share one selector that verifies and reads the same admitted
  bytes; broken, missing or contradicted admission fails closed, and excluded
  records are a count rather than a warning. Standalone directories without run
  evidence compile as `historical_unverified`; a directory with run manifests
  but no admission is refused until `psweep extract` publishes one. Compile
  manifests record `outputs.admission`.
- A compiled table with zero rows beside failed documents has status `failed`;
  valid all-empty results remain successful.
- `run_accounting.json` is version 3.0.0: runs carry result and
  processed/succeeded/failed/item counts, extraction `status` follows the latest
  run (`completed`, `completed_with_errors`, `failed`, `unknown`), and a
  `current` block reports admitted/failed/deferred documents and items.
  `summary.documents_extracted` and `cost_per_document_usd` are replaced by
  `documents_billed`, `cost_per_billed_document_usd`, and current
  `documents_admitted`/`documents_failed`/`items_admitted`/
  `cost_per_admitted_document_usd` (null when unknown). Billed attempts are no
  longer presented as successful extractions.

### Fixed (extraction source identity)
- Extraction consumes one verified version of each source. Preparation
  fingerprints the source digest; extraction stream-copies the document into a
  private, owner-only, per-document snapshot (removed on success, failure, or
  interruption), compares that digest with the prepared one, and reads only the
  snapshot, including text-cache and transcript evidence. A source that changed
  in between fails that document with `document_io/source_changed` before any
  paid request, and the record's `document.source_sha256` is the consumed
  digest. Previously a document prepared as A but read as B could publish a
  B-derived payload under A's fingerprint and be reused when the file returned
  to A.
- Capture precedes page/section targeting and freezes verified browser/API
  text, raw OCR and matching-model transcripts; roster scans, no-match
  decisions, locator caches and reuse all use that version. Fresh preflight
  keeps captures bounded, rechecks retained scopes, and preserves prior output
  on a mismatch; stale products are cleaned only after all fresh documents
  succeed, and an empty input inventory preserves previous outputs.
- The record writer returns the item count and SHA-256 of the exact bytes it
  published; manifests never rehash a later replacement as proof.

### Fixed (extraction publication)
- `write_extraction_record` validates the finalized payload against the
  resolved domain schema after roster filtering and quote verification and
  before publishing. Previously a sole unsupported row removed by quote
  filtering published `items: []` under a `minItems: 1` contract. Such a
  document now fails with `schema_contract` and keeps its prior record.

### Security
- Locked `soupsieve` (transitive via `beautifulsoup4`) moves from 3.0.4 to 4.1.2, fixing CVE-2026-126190 (polynomial-time descendant-combinator matching) reported by the dependency audit. `pixi.lock` only; no other package moved, no config, cache or cost impact.

### Fixed (extraction targeting safety)
- A running header naming the target no longer makes every page mandatory:
  only its earliest page is kept, body and wrapped identity pages stay. A
  176-page tariff with the utility name in every header could never be targeted
  under a 60-page cap and fell back to the whole document, truncated.
- Automatic targeting fails closed. A governed document with no usable
  selection, readable text over `trigger_chars` and no selection, or a final
  input (source hints and candidate context included) over `max_context` is held
  before the extraction request (`targeting_no_usable_scope`,
  `targeting_context_budget_exceeded`, category `targeting`) instead of sending
  the whole document or a silently sliced prefix. A hold ends the extract stage
  `failed` (exit 1), so `run` does not compile historical records. A
  PDF with no page readable as text or OCR is now held too.
- `--fresh` runs these checks before deleting output and keeps previous records
  when a document is held; blocked documents report no primary calls or tokens.
- The final selected reference frontier is read even when the page cap is full,
  so its dependencies are reported as omitted (one extra locator call per
  locate); a later smaller reference range can reclaim pages from an omitted
  larger one.
- A page-range answer with any malformed member is rejected whole rather than
  using the valid remainder. A reference-pass failure keeps the established pages,
  records `coverage.targeting_incomplete` and is never reused.
- A CSV row that is blank (full document) is now distinct from no row in the
  record fingerprint and skips targeting for HTML/DOC/DOCX/TXT as it did for PDF.
- Cache impact: `LOCATOR_BEHAVIOR_VERSION` is bumped, which invalidates every
  `.pages/`/`.sections/` selection and every extraction-record fingerprint in
  runs with `pages.auto_locate` enabled (including small, CSV-mapped and
  unsupported files), so the next run re-locates and re-extracts those
  documents (paid). Domains without targeting keep their fingerprints.

### Changed (breaking config)
- Discovery `selection` target rules are renamed:
  `target_identity_require_any_templates` → `target_require_any`,
  `target_identity_require_all_templates` → `target_require_all`,
  `target_identity_exclude_any_templates` → `target_exclude`.
- Removed `selection.relevance_require_legal_marker_terms` (use
  `selection.require_any`) and `selection.relevance_allowed_domain_patterns`, a
  ranking boost (use `link_prioritization.domain_scores`, e.g.
  `{"example.gov": 0.15}`). Old keys fail as unknown.
- `extraction.pages.csv` resolves relative to the config file's folder, like
  `targets` and `validation.references`; `--pages-csv` stays relative to the
  working directory.
- `extraction.pages` accepts only `csv`, `auto_locate` and `range`, and
  `auto_locate` only its documented keys; unknown keys fail.
- `--pages` and `extraction.pages.range` take comma-separated pages and
  inclusive ranges (`27,68-73`); the `100:200` and `100,200` range forms are
  removed.
- `pages.auto_locate.max_selected_pages` caps the total selection, referenced
  pages included.
- Extraction record contract 2.0.0: `coverage.selected_page_range` becomes
  `coverage.selected_page_ranges`, a list of `[start, end]` pairs, and
  `coverage.omitted_page_ranges` is added. Existing records are re-extracted on
  the next run.
- Schema `identity.deduplication.strategy` and `comparison_mode` (never used)
  are removed and rejected as unknown keys.
- `config/ADVANCED.yaml` is removed; `config/TEMPLATE.yaml` is a short, valid
  starter covering all four stages, and every key is documented once in
  `docs/config-reference.md`.

### Fixed
- Review evidence quotes may contain literal dot leaders (`Applies ........ 12`);
  they must still occur contiguously on whole words, and quotes with no words are
  rejected.
- A fenced JSON reply followed by prose, or missing its closing fence, now parses,
  including string values that contain backticks.
- PDF screening inspects PDFs that are encrypted with only an owner password
  instead of holding them; PDFs that need a password, or that MuPDF cannot
  decrypt, are still held. The inspector version is now 2, so screening-enabled
  domains re-screen their PDFs once.
- Large runs no longer retain completed section text or failed-review traceback
  graphs. Review hashes each uncached source once; reconciliation reuses bounded
  worker clients while retaining per-target attempt receipts.
- Locator subject anchoring uses configured identity rather than comma-parsing
  display labels or guessing from metadata; names containing commas stay intact.
- Source-review qualifications now include sampled-segment verified identity/content,
  scope, claim-kind and optional publication evidence. Conservative comparison keeps
  complementary/conflicting observations; optional YAML coverage guides selection.
- Capped new downloads and fallback queries distribute work fairly across owners;
  checkpoint loads hash stable shared artifacts once per load without cross-run trust.
- Source-review grades3–4 require mandatory domain-described evidence even for
  partial matches; deciding source content must be cited rather than inferred.
- Aggregate search accounting includes fallback attempts, not initial queries alone.
- Invalid reconciliation selections receive at most one schema-guided repair;
  diagnostics identify required fields/constraints and all calls remain metered.
  Valid abstentions are not retried and unsupported claims are never generated.
- Locators retain target identity/header context rather than selecting unrelated
  sections by keyword density alone. Subject/version-aware caches invalidate
  old selections; targeting runs inside bounded extraction document workers.
- HTML section targeting checks readable content against `trigger_chars` after
  removing markup, scripts/styles and navigation, avoiding unnecessary locator
  calls for short articles on large web pages.
- Discovery resumes verified downloads from completed targets when acquisition
  settings change, fetching only new/missing/changed files unless `--fresh` is
  passed. Extraction-only model aliases no longer invalidate discovery; its
  fingerprint includes only the resolved discovery-review model.
- `filelock` is overridden to 4.2.x (CVE-2026-112909, CVE-2026-112910);
  LiteLLM's `<4` cap only affects its proxy CLI. The dependency audit is clean.
- `selection.require_any` matches source URLs, titles and snippets, so opaque
  downloads on unknown hosts remain eligible. Generated query reasons are not
  source evidence.
- Recordings found again by a later run are graded by document review: the
  run's copy inherits the curated transcript (matched by SHA-256), so each file
  is transcribed once.
- A fully reused discovery run no longer creates an empty `documents/` folder.
- Document review errors name the provider error category.
- Roster state evidence also reads state-labelled table cells and a state code
  alone after an exact roster-name line, so same-name targets in shared lists
  resolve to the right state.
- URLs with suffixes unknown to Python's MIME table (`.php`, `.aspx`, `.cfm`)
  are server routes decided by the response MIME; only known non-document
  types or encodings (`.jpg`, `.zip`, `.gz`) are rejected.
- LLM calls retry every transient provider failure (408/429/5xx, connection
  errors) through the shared retry helper; TLS failures stay terminal.
- LLM retries are no longer silent: each one logs a warning with the model,
  the sanitized failure category and the attempt, e.g. `tariffs.pdf: gpt-4o
  did not finish within the 120s timeout; retrying in 2s (attempt 2/4)`. Log
  lines emitted while a document is located, extracted or transcribed start
  with its name. Exhausted timeouts report `timed out` instead of
  `provider failure`.
- Extraction reports each document as soon as it finishes, so one slow
  document no longer hides the others. Every running document has a live row
  with its own timer, and each finished document's line shows its time.
- `--debug` raises only ParseSweep's own loggers to DEBUG; third-party
  libraries stay at warnings.
- `discovered_page_ranges.csv` (`pages.auto_locate.save_discovered`) lists the
  page range behind every current record. It was rewritten with only the
  documents located in the latest run, so reused records lost their ranges,
  and a run that located nothing left removed documents listed.
- Extract withdraws records whose source document no longer exists under the
  input folder, e.g. a document rejected in `review.csv` and dropped by
  `psweep curate`. Their rows used to stay in the compiled table. Withdrawn
  records are named in the extraction summary and listed under `withdrawn` in
  the run manifest; `validate` reports a primary with records to withdraw as
  stale, and challengers withdraw theirs.
- Reasoning models (`gpt-5*`, o-series, `reasoning_models`) get JSON mode like
  every other model; only `temperature` is withheld. Without it, review,
  extraction and judge replies intermittently came back as malformed JSON.
- Error records label HTTP failures `network` / `http_<status>` and connection
  failures `network` / `network_error` instead of `internal`.
- Sampled bot challenges are retried within a run and final across runs.
- Confirmed successful SerpApi searches with no Google results no longer abort
  discovery or discard other queries' candidates. Provider failures remain
  explicit; empty queries are recorded with zero results.
- Compiled cells render lists and objects by one rule: a list of primitives is
  one comma-joined cell, other lists and objects are compact JSON. Roster
  cells, nested object fields and context fields previously showed Python
  reprs. In roster mode an empty list or object is `NA`, like a null.

### Added
- `discovery.document_review.prefer_newest` (opt-in, default `false`) for domains
  where only the latest edition counts: grading asks for the effective date too,
  provably older dated editions are never selected, undated ones are kept and
  reported, and deduplication keeps the newest copy. It is rejected with `coverage`
  or without `document_description`. Other domains keep their prompts and grade caches.
- Automatic discovery qualification with per-target review description/context,
  optional stronger-model escalation, one weak-target fallback-query stage and
  physical search/review attempt budgets. Cache/checkpoint resume retains
  successful work; domain meanings stay in YAML and verified URLs are optional.
- Validation populated-answer precision, coverage, reference-supported recall
  and target-level summaries. Reference blanks are unlabelled; explicit NA or
  `<title>__assessed=true` labels absence. Pooled dependent-cell intervals are
  not reported as independent observations; `min_accuracy` remains assessed accuracy.
- Optional roster `compilation.reconciliation` keeps YAML-defined field groups
  atomic and selects existing source observations for contested/schema-invalid
  targets. Selection-only replies, abstention, bounded requests, dated evidence,
  content-addressed decisions and durable attempt receipts preserve provenance
  and costs. Compilation remains model-free by default and during dry runs.
- Page targeting follows references. After locating the section, the locator
  reads the selected pages and adds the parts they refer to elsewhere in the
  document (e.g. "charges specified in Tariff Sheet 75"), then the parts those
  pages refer to, within `max_selected_pages`. A selection can be several page
  ranges; skipped stretches are marked `[Omitted pages: 28-67]` in the
  extracted text. Relevant pages left out by the budget are logged and recorded
  in `coverage.omitted_page_ranges`.
- `pages.csv` accepts several rows per file, one per range;
  `discovered_page_ranges.csv` writes one row per range.
- `psweep run --debug` forwards `--debug` to every stage. LLM calls log
  their prompt size, timeout, response time and token counts at debug level.
- Public-only DNS-pinned acquisition, decoded response limits, verified local
  TLS integration tests, and an opt-in external smoke-test layer.
- Protected operating mode with an approved HTTPS model endpoint, private
  POSIX workspace/cache checks, and an HPC acceptance/runbook checklist.
- Compilation writer locks and interruption-recovery journals.
- Discovery-first onboarding documentation and a source-patterns guide
  (`docs/sources.md`).
- `config/example/`: a runnable three-county demo that needs no search key.
- Embedded documentation: NumPy docstrings rendered to a Sphinx site
  (autoapi + sphinx-click) and published to GitHub Pages via CI.
- Provider/model mismatch warning in `llm_factory.build_llm_client()` when
  `--provider azure` is used with the default OpenAI model.
- `CONTRIBUTING.md` and this `CHANGELOG.md`.
- `psweep validate` and the `validation:` config block replace QA/QC. The
  compiled primary is scored against challenger `models` (real extract and
  compile) and human `references`, with an optional `judge` and a
  `min_accuracy` gate (exit 1). The command writes `cells.csv`, `summary.csv`
  and `summary.json`. `--sample N` writes a blind labelling template. `run`
  appends validate after compile when the block exists.
- Compile manifests record `build_fingerprint` and `table_sha256`, so stale
  tables are detected. Extraction requires credentials only when documents are
  pending.

### Changed
- Municode recovery retains the requested code subtree and section headings,
  rather than including unrelated neighboring articles or losing citations.
  Amendment URLs are not misrepresented as current-code nodes.
- Opted-in code-host headers also apply to initial acquisition; case-insensitive
  operator overrides take precedence on pages and API calls.
- Model transport uses client-scoped TLS for configured gateways and disables
  hidden automatic retries, preserving explicit temperature-compatibility retry.
- Extraction schema errors identify the violated schema rule without copying
  rejected record values into diagnostics or chained tracebacks.
- Comparative document review includes its complete response schema in the
  model prompt, including required per-group reasons, before strict validation.
- Extensionless downloads with missing/generic binary MIME are accepted as PDF
  only when their streamed bytes start with a PDF signature; sniffed bytes and
  response size limits are preserved.
- Search-driven discovery uses the built-in HTTP SerpApi connector throughout;
  startup no longer checks for the retired optional SerpApi SDK.
- Python minimum is 3.12; refreshed Python/native locks and release CI.
  Scheduled dependency audits remain fail-closed when advisories are unresolved.
- TLS verification defaults to on. Explicit public-mode false settings remain
  available with interception-risk warnings; protected mode rejects them.
  Prefer system/approved CA roots. Acquisition ignores proxies and `.netrc`.
- A single Click application replaces the Typer migration bridge. Commands,
  interactive menu, subprocess entry point, and exit semantics are retained.
  Pipeline subprocesses use the active interpreter, never an ambient Pixi
  executable that could select another environment or fail outside the repo.
- Browser discovery requires external isolation and a preinstalled driver;
  it never disables Chrome's sandbox and is prohibited in protected mode.
- Model credentials are per-client rather than process-global; provider
  exceptions and raw model responses are not copied into application logs.
- CSV evidence is formula-escaped; XLSX evidence is literal.
- Compilation rejects malformed payloads before publication, expands nested
  rows from schema rather than item order, and reports incomplete provenance.
  Protected compilation requires canonical records and complete provenance.
  Missing records fail without touching exports; valid zero-item `--fresh`
  results use the same locked, recoverable publication path.
- Extraction CLI/API share one workflow, model resolution, selection and outcomes.
  Omitted models inherit environment defaults; explicit providers and generic
  endpoint overrides no longer lose routing or borrow another provider's key.
- Extraction reuse validates source/schema/effective settings and record-byte
  evidence. Stale, invalid, or older unverifiable JSON is re-extracted. `--fresh`
  resets the entire resolved output only after preflight; filtered/limited runs
  rebuild only selected inputs. Invalid requests preserve prior output.
- Extraction exits 0 for success/verified reuse, 1 for operational or partial
  failure, and 2 for empty selection; valid zero-item payloads still succeed.
  Full-pipeline drivers stop before downstream stages after failed extraction.
- Retired inert extraction profile/category controls, YAML schema-pack unwrapping,
  and unreachable long-form model/model-default abstractions; use JSON schemas
  and string aliases. Core record schema now declares already-emitted item count
  and coverage, plus optional input fingerprint; payload contracts stay closed.
- OCR caches hold raw text and apply current corrections without repeated OCR;
  incompatible corrected caches invalidate. Page/section products verify content
  and effective model/settings; section pairs use full filenames and text digests.
- Discovery YAML now uses only canonical `search`, `search.max_results`, and
  selection `max_per_target`/`require_any`/`exclude`. Migrate retired `seeker`,
  `browser`, `max_results_per_query`, `primary_per_target`,
  `relevance_require_any_terms`, and `relevance_exclude_any_terms` to their
  named replacements (`browser_mode` for `browser`); aliases fail with
  actionable errors even when canonical keys coexist.
- Retired no-op `document_review.action`: originals remain in `documents/`
  and selected curation is materialized separately; remove this setting.
- Implicit adjacent discovery/extraction/compilation/validation overrides
  beside `run.yaml`, `run.yml`, or `run.json` are retired. Move settings into
  the corresponding sections of the selected config and remove adjacent
  files, or load a section-wrapped config explicitly with `--config`.
- Discovery geography now requires explicit `state`/`jurisdiction` fields or
  `partition_by`; query text does not infer geography. There is no built-in
  US discovery alias table. Configure `jurisdiction_aliases` where needed.
- Domain policy replaces built-in legal draft, vendor authority, and shopping
  lists. Configure `draft_patterns`, `link_prioritization.domain_scores`, and
  `shopping_path_keywords`; configured ranking keywords replace the generic
  document-path fallback. Concrete provider/vendor source filters remain
  domain-config choices, not automatic parser-derived settings.
- Discovery outcomes have verbosity-independent exit codes: error-free
  `success`/`dry_run`/`reused` return 0, `failed` returns 1 (including failure
  with zero downloads), genuinely `empty` returns 2 and `partial_failure`
  returns 3; `run` continues past partial stages and exits 3 at the end.
- Checkpoint v2 reuse requires matching effective discovery config and artifact
  digest evidence, including curated files; changed/missing artifacts and v1
  entries reacquire. Publication locks reject concurrent corpus writers and
  journal recovery handles interrupted discovery curated/provenance swaps.
  This is not a general pipeline transaction or large-scale certification.
- Strict robots/ToS enforcement cannot cover browser redirects/subresources
  and fails before unsupported browser navigation; use HTTP acquisition for
  enforcement. TLS verification is on by default; protected mode rejects
  insecure environment settings.
- OCR sidecars now use `.text/<filename>.txt` and `<filename>.meta.json`, and
  reviewer sidecars use `.review/<filename>.json`, avoiding same-stem format
  collisions. Old stem-only caches are not reused. Native extraction, OCR
  correction rules, and page-targeting behavior are unchanged.
- Query-driven discovery infers search enablement when omitted; explicit
  config/CLI disable still wins. HTML and extensionless documents are eligible
  without disabling the supported-document filter; real MIME gates downloads.
- Browser recovery and code-host adapters are now explicit opt-ins; threshold
  blocks alone do not enable them. Search caching remains opt-in.
- Discovery validates nested options, booleans, regexes, and resource limits
  before execution. Quoted booleans and previously ignored `pipeline`,
  `scoring`, `dedupe`, `file_filters`, and `keyword_validation` blocks are
  rejected; remove those no-op blocks from existing configs.
- Direct discovery requests now share the CLI's two-download concurrency
  default (previously five for direct requests).
- Single-source-of-truth defaults: every user-facing default is one named
  constant referenced by the dataclass field, the CLI option, the CLI
  resolution fallback, and (for policies) the evaluator signature, so `--help`
  provably matches runtime behavior.
- `extract --model` now shows its default in `--help` (`show_default=True`).
- `--max-context` default raised from 400000 to 600000 to match the config
  template's recommendation.
- `discover` Choice options (`--partition-mode`, `--robots-policy-mode`,
  `--tos-policy-mode`) now show their defaults in `--help`.
- Robots and ToS policy default is now `warn` everywhere (previously the
  effective default drifted between `ignore` and `warn` across layers).
- Docs toolchain switched from MkDocs to Sphinx.

### Removed
- Bundled domain configs are removed from the repository: `config/` ships only
  `README.md`, `TEMPLATE.yaml` and `example/`, and domain folders you create
  there are git-ignored.
- The unused `psweep.utils.logger` module and the empty `psweep.core` package.
- The README image.

### Fixed
- Preserve selected pages across native/pypdf/OCR fallbacks, actual parsed scope,
  nested schema identifiers, declared main-array counts, and distinct source IDs.
- Prevent empty discovery-index selection falling back to directory extraction;
  include mixed root/nested documents, exclude generated sidecars, reject output
  collisions/overlap, and apply limits after validated reuse consistently.
- Atomically publish validated records/manifests through shared file primitives;
  retain returned usage through parsing/schema/write failures and include locator
  and retry calls without claiming unknown billing is measured zero.
- OCR cache writes stage temporary files and replace metadata last. Readers
  verify source size/mtime and source/text SHA-256 digests, treating partial,
  stale, or mismatched pairs as misses. Curation uses the canonical cache-path
  helper to carry full-filename pairs with selected documents.
- Enforce stable, deduplicated search limits locally per query for live, cached,
  and news results; reject provider errors before caching and protect reserved
  query/count/credential parameters from passthrough overrides.
- Apply explicit source/URL scope before selection and at final redirects,
  preserve search title/snippet evidence, and distinguish filtered results from
  missing credentials or empty/provider-failed searches.
- Discovery request headers reach the engine, explicit search disables and
  CLI domain overrides are honored, and validation-only execution uses the
  same input-plan check as `discover` and `run` before side effects.
- Run config helpers propagate validation errors instead of falling back to
  unvalidated YAML.
- Standardized all docstrings to NumPy style — `ruff check src/` reports 0
  enforced pydocstyle (D) violations (was 58).

## [2.0.1]

Baseline release: contract-first discover → extract → compile pipeline with
CLI, YAML config, and JSON-schema-driven extraction.

[Unreleased]: https://github.com/bpulluta/parsesweep/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/bpulluta/parsesweep/releases/tag/v2.0.1
