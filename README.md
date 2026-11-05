# ParseSweep

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/bpulluta/parsesweep/actions/workflows/ci.yml/badge.svg)](https://github.com/bpulluta/parsesweep/actions/workflows/ci.yml)
[![Docs](https://github.com/bpulluta/parsesweep/actions/workflows/docs.yml/badge.svg)](https://bpulluta.github.io/parsesweep/)

**Find documents, extract schema-defined data with an LLM, and compile it into one table.**

ParseSweep turns a list of targets (counties, utilities, products, …) into a
complete data structure: it discovers the documents that answer
your questions, extracts the fields your JSON schema defines, compiles them
with sources, confidence and quotes, and validates the result against other
models and human-labelled samples.

## Install

```bash
curl -fsSL https://pixi.sh/install.sh | bash      # pixi, if you don't have it
git clone https://github.com/bpulluta/parsesweep.git && cd parsesweep
pixi install
```

Put credentials in `.env` (never in YAML): one LLM provider for extraction, and
`SERPAPI_API_KEY` only if discovery searches the web. Start from
[`.env.example`](.env.example), or let `pixi run psweep init` write an OpenAI or
Azure key.

## Try the example

```bash
pixi run psweep run --config config/example/run.yaml
```

The [example](config/example/run.yaml) downloads the solar permit pages of
three counties directly, so it needs only the LLM key. It writes
`outputs/example/compiled/example.csv` (and `.xlsx`): one row per county with
the extracted fields plus `sources`, `confidence` and `evidence`.

## The pipeline

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

Each stage reuses verified earlier work, so re-running is cheap and resumes
where the last run stopped; `--fresh` starts over. Exit codes: 0 success,
1 failure, 2 nothing found, 3 partial (re-run to retry).

## A domain = schema + run.yaml + targets

```text
config/<domain>/
├── run.yaml       # how to run: models, discovery, extraction, compilation, validation
├── schema.json    # what to extract: fields and their descriptions
├── targets.csv    # who/what to cover: one row per target
└── samples/       # optional hand-labelled reference sheets
```

```bash
mkdir -p config/my_domain
cp config/TEMPLATE.yaml config/my_domain/run.yaml      # then add targets.csv
pixi run psweep discover --config config/my_domain/run.yaml --validate-config
pixi run psweep run --config config/my_domain/run.yaml --validate-config  # after schema.json
```

[`config/TEMPLATE.yaml`](config/TEMPLATE.yaml) is a complete, valid starting
point; domain folders you create under `config/` are git-ignored and stay local.
Discovery alone needs neither a schema nor LLM credentials; the later stages
need `schema.json`.

## Outputs

```text
outputs/<domain>/
├── discovered/   # runs/<run_id>/ (manifest, review.csv) and curated/ documents
├── extracted/    # one JSON record per document
├── compiled/     # <domain>.csv / .xlsx and run_accounting.json
└── validated/    # cells.csv, summary.csv, summary.json
```

## Documentation

- [Getting Started](docs/getting-started.md) — the example, then your own domain end to end
- [Source Patterns](docs/sources.md) — searches, exports, listings, code hosts, audio
- [Configuration Reference](docs/config-reference.md) — every `run.yaml` key, defaults, reuse and exit codes
- [Tuning & Iteration](docs/tuning.md) — symptom → setting
- [Schema Authoring](docs/schemas/index.md) — writing `schema.json`
- [Commands](docs/commands/index.md) — every CLI option
- [Security](docs/security.md) — trust model and protected operation

Published at <https://bpulluta.github.io/parsesweep/>.

## Development

```bash
pixi run test          # test suite
pixi run docs-build    # documentation (warnings are errors)
```

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT License — see [LICENSE](LICENSE).
