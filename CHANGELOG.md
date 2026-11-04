# Changelog

All notable changes to ParseSweep are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Configured legal-document markers use source titles and snippets as well as
  URLs, allowing opaque downloads on unknown hosts without jurisdiction
  allowlists. Generated query reasons remain excluded from source evidence.
- Confirmed successful SerpApi searches with no Google results no longer abort
  discovery or discard other queries' candidates. Provider failures remain
  explicit; empty queries are recorded with zero results.

### Added
- Public-only DNS-pinned acquisition, decoded response limits, verified local
  TLS integration tests, and an opt-in external smoke-test layer.
- Protected operating mode with an approved HTTPS model endpoint, private
  POSIX workspace/cache checks, and an HPC acceptance/runbook checklist.
- Compilation writer locks and interruption-recovery journals.
- Short discovery-only starter template, with expanded settings in
  `config/ADVANCED.yaml` and discovery-first onboarding documentation.
- Embedded documentation: NumPy docstrings rendered to a Sphinx site
  (autoapi + sphinx-click) and published to GitHub Pages via CI.
- Provider/model mismatch warning in `llm_factory.build_llm_client()` when
  `--provider azure` is used with the default OpenAI model.
- `CONTRIBUTING.md` and this `CHANGELOG.md`.

### Changed
- Municode recovery retains the requested code subtree and section headings,
  rather than including unrelated neighboring articles or losing citations.
  Amendment URLs are not misrepresented as current-code nodes.
- QA/QC reports read evidence from the actual compared model directory, not
  the canonical single-model extraction directory.
- Opted-in code-host headers also apply to initial acquisition; case-insensitive
  operator overrides take precedence on pages and API calls.
- Model transport uses client-scoped TLS for configured gateways and disables
  hidden automatic retries, preserving explicit temperature-compatibility retry.
- Extraction schema errors identify the violated schema rule without copying
  rejected record values into diagnostics or chained tracebacks.
- Full-pipeline approval also covers QA/QC extraction: `run --yes` no longer
  aborts at a second interactive prompt. Standalone `validate --yes` is available.
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
- CSV evidence is formula-escaped; XLSX evidence is literal while trusted
  QA/QC formulas and literal-cell dropdowns remain functional.
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
  `success`/`dry_run`/`reused` return 0, `failed`/`partial_failure` return 1
  (including failure with zero downloads), and genuinely `empty` returns 2.
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
  blocks alone do not enable them. Existing dependent domain configs pin their
  recovery settings. Search caching remains opt-in.
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
- README broken-image alt text.

## [2.0.1]

Baseline release: contract-first discover → extract → compile pipeline with
CLI, YAML config, and JSON-schema-driven extraction.

[Unreleased]: https://github.com/bpulluta/parsesweep/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/bpulluta/parsesweep/releases/tag/v2.0.1
