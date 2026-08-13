# Changelog

All notable changes to ParseSweep are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Embedded documentation: NumPy docstrings rendered to a Sphinx site
  (autoapi + sphinx-click) and published to GitHub Pages via CI.
- Provider/model mismatch warning in `llm_factory.build_llm_client()` when
  `--provider azure` is used with the default OpenAI model.
- `CONTRIBUTING.md` and this `CHANGELOG.md`.

### Changed
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
- Standardized all docstrings to NumPy style — `ruff check src/` reports 0
  enforced pydocstyle (D) violations (was 58).
- README broken-image alt text.

## [2.0.1]

Baseline release: contract-first discover → extract → compile pipeline with
CLI, YAML config, and JSON-schema-driven extraction.

[Unreleased]: https://github.com/bpulluta/parsesweep/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/bpulluta/parsesweep/releases/tag/v2.0.1
