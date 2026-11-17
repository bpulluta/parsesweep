# Changelog

All notable changes to ParseSweep are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
