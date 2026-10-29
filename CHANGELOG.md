# Changelog

All notable changes to ParseSweep are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Short discovery-only starter template, with expanded settings in
  `config/ADVANCED.yaml` and discovery-first onboarding documentation.
- Embedded documentation: NumPy docstrings rendered to a Sphinx site
  (autoapi + sphinx-click) and published to GitHub Pages via CI.
- Provider/model mismatch warning in `llm_factory.build_llm_client()` when
  `--provider azure` is used with the default OpenAI model.
- `CONTRIBUTING.md` and this `CHANGELOG.md`.

### Changed
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
  enforcement. TLS verification remains an explicit user environment choice
  with unchanged SerpApi/download defaults of `false`.
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
