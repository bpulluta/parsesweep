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

## Repository layout

Tracked: `src/`, `tests/`, `docs/`, `config/README.md`, `config/TEMPLATE.yaml`,
`config/example/`, and CI/packaging files (`.github/`, `pyproject.toml`,
`pixi.toml`, `pixi.lock`). Your domains under `config/`, `outputs/` and
`documents/` are git-ignored; personal scratch folders belong in
`.git/info/exclude`, not `.gitignore`.

## Branching

- Always work on a feature branch — never commit directly to `main`.
- Name branches by intent, e.g. `feat/…`, `fix/…`, `docs/…`, `refactor/…`.
- Keep changes phase-gated and test-first. Open a PR against `main`.

## Testing

Use the fast loop while iterating; run the full suite before opening a PR:

```bash
pixi run --locked -e dev test-fast
pixi run --locked -e dev test
# A focused module is usually the fastest feedback loop.
pixi run --locked -e dev test tests/test_<area>.py
```

`test` remains the complete CI gate. `test-fast` excludes only explicitly
marked `integration`, `slow`, and `live` cases; it includes unmarked tests.
Real subprocess, local TLS, and ffmpeg checks are `integration`; the largest
scheduling and checkpoint cases are `slow`. Smaller scale cases remain in
the fast loop. Markers are strict: a misspelled marker fails collection.
Use `-v` when individual test names are useful, and `--durations=20` to
measure bottlenecks before consolidating anything.

Ordinary tests block external sockets and disable dotenv loading. Local
HTTP/TLS servers use synthetic data; external endpoint tests require an
explicit `--live` opt-in:

```bash
pixi run --locked -e dev test -m 'integration or slow'
pixi run --locked -e dev test -m live --live
```

Use the `unit`, `contract`, `integration`, and `live` validation layers for
new tests; `slow` marks expensive scale checks independently of their layer.
Mock the narrow remote transport boundary, not entire installed modules.
Keep regression invariants when consolidating repeated fixtures. Share only
immutable fixture inputs; mutable workspaces, servers and call histories
must stay test-local. Allocation profiling belongs in a dedicated benchmark,
not a correctness test that merely prints timings. Release validation must
also run a synthetic subprocess CLI pipeline and inspect its actual outputs;
passing mocked requests is not an end-to-end smoke test.
Never use real credentials or sensitive documents in tests.

Lint and format with ruff (docstrings are enforced NumPy-style):

```bash
pixi run lint
```

`pixi run lint` runs `ruff check` and `ruff format --check` on `src/` and
`tests/` under the config in `pyproject.toml`; it must pass, and CI runs the
same task. Apply formatting with `pixi run -e dev ruff format src tests`. When a
rule is a false positive, prefer a line-level `# noqa: CODE - reason` over a
new ignore. Report skipped tests or blocked checks explicitly in the PR.

## Building the docs

Docs live in `docs/` and build with Sphinx in the `docs` environment. The API
reference is generated from NumPy docstrings (autoapi) and the CLI reference
from the live Click objects (sphinx-click), so **the code is the source of
truth** — write clear docstrings rather than separate prose.

```bash
pixi run docs-build     # sphinx-build -W (warnings are errors)
pixi run docs-serve     # serve docs/_build/html at :8000
pixi run docs-clean     # remove docs/_build
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
and [Configuration Reference](docs/config-reference.md) for every config key.

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
