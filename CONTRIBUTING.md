# Contributing to ParseSweep

Thanks for helping improve ParseSweep. This guide covers local setup, the
branching model, testing, building the docs, and authoring schemas.

## Setup

ParseSweep uses [pixi](https://pixi.sh) to manage every environment. There is
no `pip install` step — pixi resolves the default, `dev`, and `docs`
environments from `pixi.toml`.

```bash
# Install pixi if you don't have it
curl -fsSL https://pixi.sh/install.sh | bash

# Clone and install
git clone https://github.com/bpulluta/parsesweep.git && cd parsesweep
pixi install
```

Configure LLM credentials in a `.env` file (see
[Getting Started](docs/getting-started.md)) before running the pipeline.

## Branching

- Always work on a feature branch — never commit directly to `main`.
- Name branches by intent, e.g. `feat/…`, `fix/…`, `docs/…`, `refactor/…`.
- Keep changes phase-gated and test-first. Open a PR against `main`.

## Testing

Run the full suite before opening a PR:

```bash
pixi run -e dev pytest
```

Lint and format with ruff (docstrings are enforced NumPy-style):

```bash
pixi run -e dev ruff check src/
pixi run -e dev ruff format --check src/
```

A change is not done until tests pass and `ruff check src/` reports 0
violations. If a test is skipped or a check is blocked, say so explicitly in
the PR.

## Building the docs

Docs live in `docs/` and build with Sphinx in the `docs` environment. The API
reference is generated from NumPy docstrings (autoapi) and the CLI reference
from the live Click objects (sphinx-click), so **the code is the source of
truth** — write clear docstrings rather than separate prose.

```bash
pixi run -e docs docs-build     # sphinx-build -W (warnings are errors)
pixi run -e docs docs-serve     # serve docs/_build/html at :8000
pixi run -e docs docs-clean     # remove docs/_build
```

The build runs with `-W`, so a broken docstring or dead cross-reference fails
the build (and the CI check). Fix warnings; don't suppress them.

## Authoring schemas

Each domain is defined by two files:

| File | Role |
|------|------|
| **Schema** (JSON) | *What* to extract — field names, types, descriptions |
| **Config** (YAML) | *How* to run — paths, page targeting, dedup, output |

Guidelines:

- Don't duplicate information between the schema and the config.
- Keep user-facing config minimal — derive what you can from what's already
  specified.
- Field descriptions are the LLM's instructions; write them precisely.

See [Schema Authoring](docs/schemas/index.md) for the full best-practices guide
and [Configuration Reference](docs/config-reference.md) for the annotated
config template.

## Code quality standards

These apply to every change:

- No legacy code, backward-compatibility layers, or dead paths — remove old
  code when you replace it.
- Single source of truth for every concept; no redundancy.
- Nothing hardcoded that belongs in a schema or config.
- Must scale from 10 targets to 100,000.
- Keep the product cohesive: consistent CLI style, config patterns, and naming.

## Changelog

Add an entry under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for any
user-facing change (Added / Changed / Fixed / Removed).
