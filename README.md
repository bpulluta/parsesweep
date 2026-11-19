# ParseSweep

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/bpulluta/parsesweep/actions/workflows/ci.yml/badge.svg)](https://github.com/bpulluta/parsesweep/actions/workflows/ci.yml)
[![Docs](https://github.com/bpulluta/parsesweep/actions/workflows/docs.yml/badge.svg)](https://bpulluta.github.io/parsesweep/)

**Find documents, extract schema-defined data with an LLM, and compile it into one table.**

Give ParseSweep a list of targets (counties, utilities, products, …) and a JSON
schema describing the fields you want. It finds the documents that answer your
questions and returns one table where every value carries its source.

## What you get

- Discovery by search, known URLs, listings or code hosts.
- The fields your schema defines, with sources, confidence and quoted evidence.
- Reuse of verified work, so re-runs and resumes are cheap (`--fresh` starts over).
- Cost shown before it is spent: the `run` plan lists paid stages, the search
  upper bound and unpriced models; `--show-effective-config` shows every
  default; `estimate` prices a batch.
- Validation against other models and hand-labelled samples.

## Install

```bash
curl -fsSL https://pixi.sh/install.sh | bash      # pixi, if you don't have it
git clone https://github.com/bpulluta/parsesweep.git && cd parsesweep
pixi install
```

Put credentials in `.env` (never in YAML): one LLM provider key, plus
`SERPAPI_API_KEY` only if discovery searches the web. Copy
[`.env.example`](.env.example), or run `pixi run psweep init`, which writes an
OpenAI or Azure key.

## Quickstart

```bash
pixi run psweep run --config config/example/run.yaml
```

The [example](config/example/run.yaml) downloads known solar permit pages for
three counties, so it needs only the LLM key. It writes
`outputs/example/compiled/example.csv` (and `.xlsx`): one row per county with
the extracted fields plus `sources`, `confidence` and `evidence`.

## How it runs

```text
discover  →  extract  →  compile  →  validate
web/sources  JSON per     one table     models and
→ documents  document     per domain    references
```

```bash
pixi run psweep run --config config/<domain>/run.yaml          # every configured stage
pixi run psweep run --config config/<domain>/run.yaml -n 3     # first 3 targets only
pixi run psweep discover --config config/<domain>/run.yaml     # or one stage at a time
```

Exit codes: 0 success, 1 failure, 2 nothing found, 3 partial (re-run to retry).
Outputs land in `outputs/<domain>/`; see [Getting Started](docs/getting-started.md).

## Your own domain

A domain is three files under `config/<domain>/`: `run.yaml` (how to run),
`schema.json` (what to extract) and `targets.csv` (who to cover). Domain folders
are git-ignored and stay local.

```bash
mkdir -p config/my_domain
cp config/TEMPLATE.yaml config/my_domain/run.yaml      # then add targets.csv
pixi run psweep discover --config config/my_domain/run.yaml --validate-config
pixi run psweep run --config config/my_domain/run.yaml --validate-config  # after schema.json
```

[`config/TEMPLATE.yaml`](config/TEMPLATE.yaml) is the search + review starter;
for known URLs, start from [`config/example/`](config/example/run.yaml). Then run
a slice with `-n 3` and follow the
[convergence loop](docs/tuning.md#converge-a-new-domain).

## Documentation

| Page | What it covers |
|---|---|
| [Getting Started](docs/getting-started.md) | The example, then your own domain end to end |
| [Source Patterns](docs/sources.md) | Searches, exports, listings, code hosts, audio |
| [Configuration Reference](docs/config-reference.md) | Every `run.yaml` key, [required keys, defaults and optional features](docs/config-reference.md#required-keys-defaults-and-optional-features), reuse and exit codes |
| [Tuning & Iteration](docs/tuning.md) | Symptom → setting |
| [Schema Authoring](docs/schemas/index.md) | Writing `schema.json` |
| [Commands](docs/commands/index.md) | Every CLI option |
| [Security](docs/security.md) | Trust model and protected operation |

Published at <https://bpulluta.github.io/parsesweep/>.

## Development

```bash
pixi run test          # test suite
pixi run docs-build    # documentation (warnings are errors)
```

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT License — see [LICENSE](LICENSE).
