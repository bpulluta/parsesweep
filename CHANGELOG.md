# Changelog

All notable changes to ParseSweep are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed (response outcomes, one schema-feedback generation)
- Primary extraction now rejects length-terminated (`response_truncated`) and
  refused (`response_refused`) answers as explicit `model_response` failures
  instead of accepting them (a schema-valid `items: []` with
  `finish_reason=length` used to pass). They are never regenerated.
- An empty, non-JSON or schema-invalid answer gets at most one feedback
  generation: the original request, unchanged, plus the rejected answer and
  bounded path/rule diagnostics (no values). It draws on one per-document
  allowance of 4 physical attempts (`RETRY_ATTEMPTS`) shared with transport
  retries and the temperature fallback; the allowance is document-local and is
  not stored on the shared client. Both generations' cost and tokens are in the
  record and in failures. New `ResponseOutcomeError` and `AttemptAllowance`;
  `schema_diagnostics` is shared with reconciliation (its repair diagnostics now
  also name unexpected keys and expected types).
- The extraction prompt now says conversions apply only where a schema field
  description explicitly requires them, that unsupported conversions are
  `null`, and that evidence quotes stay verbatim.
- Discovery, reconciliation, locators, judges and transcription keep their
  previous response handling.
- Cost/cache impact: an invalid answer that used to fail now costs one more
  paid call (a full input resend; providers' prompt caching may discount it).
  The prompt text and repair behavior are not part of the extraction
  fingerprint, so no existing record is invalidated or regraded and no
  discovery cache changes; records produced before this change are reused until
  the Step 9 fingerprint change (use `--fresh` to re-extract).

### Changed (per-page PDF representation)
- PDF text extraction decides native text versus OCR per page instead of from
  an aggregate 500-character total, so a raster rate table can no longer be
  hidden behind a readable cover page. Each selected page is recorded as
  `native`, `ocr`, `blank` or `unreadable` with its original page number
  (`pages` in the extraction metadata). Blank pages are covered and never
  OCR-ed; a raster page OCR cannot read is `unreadable` and no longer counted
  in `extracted_pages`, so the record reports `parser_partial` with a warning.
  Mixed documents are parsed and cached together; `ocr_corrections` now touch
  OCR pages only, not native pages of the same document.
- The page locator's index OCRs raster pages, so scanned documents can be
  targeted instead of being held as unreadable.
- Cache/cost impact: OCR text caches written before this change lack per-page
  offsets and are treated as misses, so scanned PDFs are OCR-ed once more
  (local Tesseract, no LLM cost). Existing extraction records are not
  invalidated by this step alone: for a mixed PDF the new text differs but the
  source bytes do not, so such records are reused until the Step 9 fingerprint
  change (re-extract them with `--fresh` to pick up the OCR-ed pages). No
  discovery regrade; `pdf_inspection` is unchanged.

### Changed (scope budgeting, explicit locator)
- Targeting now triggers at `min(trigger_chars, max_context)`: a document too
  large for `max_context` is targeted even when under `trigger_chars` (it was
  previously held untargeted). A locator candidate index the route cannot
  admit is split in halves (at most 8 requests) instead of failing the
  document; any unsendable remainder holds it. Section/page cache entries are
  unchanged. Cost: a few extra locator calls only for oversized indexes.
- `extraction.max_context` is now enforced on every path, including CSV and
  `pages.range` scope, disabled targeting and unsupported types. Oversized
  input is held (`targeting_context_budget_exceeded`) and the primary is not
  called; `DocumentExtractor.extract` rejects it instead of slicing a prefix.
  Records that were truncated at `max_context` can no longer be produced.
- `pages.auto_locate.model` is required when auto-locate is enabled and must
  resolve to a different provider/model than `extraction.model`; the silent
  inherit-the-primary default is removed. Configs that enabled auto-locate
  without a model now fail validation. No record, fingerprint or cache impact;
  documents that were previously truncated will hold until a scope is chosen.

### Fixed (central request admission)
- `LLMClient.complete` now admits the exact final request (messages plus
  response schema) against `model_context_windows` before any provider
  attempt, for every caller, replacing the extract-only chars/4 check. Primary
  extraction and locator calls additionally use LiteLLM's pinned local catalog
  and send the route's output limit as `max_tokens`. Estimates are
  conservative (3 UTF-8 bytes/token). Unknown limits are not guarded: a
  custom route with no known output limit is sent without `max_tokens`
  (never blocked, never a guessed constant); truncation is rejected after
  the fact by the response-outcome check. A
  rejected request sends and meters nothing. Discovery, reconciliation and
  transcription get no catalog guard or output cap.
- No cache, record, or discovery impact; very large inputs on known routes
  now hold earlier with `context_window_exceeded`.

### Changed (test efficiency)
- The full offline suite and CI retain all distinct regression scenarios;
  two duplicate config-validation parameter cases are consolidated into
  existing stronger checks. Scheduling scale tests no longer collect
  unasserted allocation/timing diagnostics. TLS tests
  share immutable certificate files, keep isolated servers/call histories, and
  shut down without the default half-second polling delay.
- `pixi run --locked -e dev test-fast` is an optional local loop excluding
  explicit `integration`, `slow`, and `live` cases. The ordinary `test` task
  still runs real CLI/TLS/ffmpeg checks and the largest scale cases. Pytest
  defaults to concise output and rejects unknown markers; `-v` remains available.
- Runtime/config contracts and extraction/review caches are unchanged; no
  re-extraction, re-screening, or paid regrading is triggered.

### Fixed (extraction admission and outcomes)
- Preserved historical records no longer enter a current compilation.
  Extraction publishes an atomic `run_manifests/current_admission.json` with a
  per-document disposition (`new`, `reused`, `failed`, `deferred`) and the exact
  record digest for admitted records, scoped to the invocation (before `limit`)
  and merged with prior qualification. Failed or deferred documents keep their
  old bytes on disk but are not admitted; succeeded and verified-reused siblings
  of a partial run are. Previously a changed document that failed re-extraction
  compiled alongside new siblings with no warning. Extraction holds a per-output
  single-writer lock. Records judged reusable at preparation are re-hashed
  under that lock; one replaced in between fails that document instead of
  being admitted as reused. No cache or cost impact.
- Admission pointer is now version 2.0.0: each admitted entry also records the
  consumed `source_sha256` and `input_fingerprint`. Before rewriting records, a
  run marks its pending documents `deferred`, so an interrupted run excludes
  them from compilation instead of leaving new bytes under an old digest;
  `psweep extract` again re-admits them (documents finished before the
  interruption are re-extracted, since reuse proof is written only at run end).
  Pointers published by the earlier draft format are rejected as malformed:
  rerun `psweep extract` once. No discovery regrade.
- Compilation, dry-run, `compiled_table_fresh`, provenance and the build
  fingerprint share one selector (`select_compile_inputs`) that verifies and
  reads the same admitted bytes; broken, missing or contradicted admission fails
  closed. `analyze_compile_input_provenance` now takes the selection and the
  orphan-record warning is replaced by exclusion. Standalone directories without
  run evidence compile as `historical_unverified`. Compile manifests record
  `outputs.admission`.
- A compiled table with zero rows beside failed documents now has status
  `failed`; valid all-empty results remain successful.
- `run_accounting.json` is version 3.0.0: runs carry result and
  processed/succeeded/failed/item counts, extraction `status` follows the latest
  run (`completed`, `completed_with_errors`, `failed`, `unknown`), and a
  `current` block reports admitted/failed/deferred documents and items.
  `summary.documents_extracted` and `cost_per_document_usd` are replaced by
  `documents_billed`, `cost_per_billed_document_usd`, and current
  `documents_admitted`/`documents_failed`/`items_admitted`/
  `cost_per_admitted_document_usd` (null when unknown). Billed attempts are no
  longer presented as successful extractions.
- Cache/cost impact: no re-extraction, regrade or model call. Existing extraction
  directories must run `psweep extract` once to publish an admission before they
  compile (records invalidated by the source-identity change above are
  re-extracted then); compiled tables become stale once (fingerprint now includes
  the admission qualification).

### Fixed (extraction source identity)
- Extraction now consumes one verified version of each source. Preparation
  fingerprints the source digest; extraction stream-copies the document into a
  private, owner-only, per-document snapshot (removed on success, failure, or
  interruption), compares that digest with the prepared one, and reads only the
  snapshot (including text-cache and transcript evidence). A source that changed
  in between fails that document with `document_io/source_changed` before any
  paid request, and the record's `document.source_sha256` is the consumed
  digest rather than a later hash of the live path. Previously a document
  prepared as A but read as B could publish a B-derived payload under A's
  fingerprint and be reused when the file returned to A. Roster scans use the
  same verified version, including empty no-match decisions. Capture now precedes
  page/section targeting and freezes verified browser/API text, raw OCR, and
  matching-model transcripts; all consumers use that version. Retained fresh
  scopes are identity-checked again; stale-input failures preserve prior output.
  Fresh cleanup removes stale products only after all fresh documents succeed;
  an empty input inventory preserves previous outputs. Saved scopes exclude failed
  reused documents. Transcript acceptance is shared across preparation/capture,
  including cached-only audio; newly appearing derived text is checked against
  the selected reuse proof. Page locators no longer accept unbound page text.
  `build_extraction_record` and `write_extraction_record` require `source_sha256`.
  The writer returns `(item_count, record_sha256)` for the exact serialized bytes
  atomically published; manifests never rehash a later replacement as proof.
  Rollout: `captured-input-v1` fingerprints and manifest consumption proofs reject
  all pre-fix extraction records (later runs pay for re-extraction). Locator
  behavior `captured-scope-v4` invalidates old page/section caches (paid re-location
  only when needed), retaining original sidecar locations. Valid browser/API,
  raw OCR, and matching-model transcript caches remain usable; creation of derived
  caches does not invalidate the next run, but changed derived text does. Record
  contract remains 2.0.0; no discovery review-cache change or discovery regrade.

### Fixed (extraction publication)
- `write_extraction_record` now validates the finalized payload against the
  resolved domain schema after roster filtering and quote verification and
  before publishing. Previously a sole unsupported row removed by quote
  filtering published `items: []` under a `minItems: 1` contract. Such a
  document now fails with `schema_contract` and keeps its prior record. Direct
  writer callers must pass `payload_validator`. No cache impact for valid
  records: reuse is unchanged and nothing is re-extracted or regraded.

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
  document with no readable native PDF text is now held too (use a blank CSV
  row to force OCR of the full document).
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
