# ParseSweep

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/bpulluta/parsesweep/actions/workflows/ci.yml/badge.svg)](https://github.com/bpulluta/parsesweep/actions/workflows/ci.yml)
[![Docs](https://github.com/bpulluta/parsesweep/actions/workflows/docs.yml/badge.svg)](https://bpulluta.github.io/parsesweep/)

**Find documents, extract schema-defined data with an LLM, and compile it into one table.**

You give ParseSweep a list of targets (counties, utilities, products, …) and a
JSON schema of the fields you want. It finds the right documents and returns
one table where every value shows its source, a confidence level and a quote.

- Re-runs reuse earlier work, so they are cheap (`--fresh` starts over).
- It shows what a run will spend before it spends it.

## 1. Install

```bash
curl -fsSL https://pixi.sh/install.sh | bash      # pixi, if you don't have it
git clone https://github.com/bpulluta/parsesweep.git && cd parsesweep
pixi install
```

Add your keys to a `.env` file (never in YAML): one LLM provider key, plus
`SERPAPI_API_KEY` only if you search the web. Copy [`.env.example`](.env.example),
or run `pixi run psweep init`, which writes an OpenAI or Azure key.

## 2. Try the example

```bash
pixi run psweep run --config config/example/run.yaml
```

This downloads solar permit pages for three counties and extracts the fields in
[`config/example/`](config/example/run.yaml). It needs only the LLM key. The
result is `outputs/example/compiled/example.csv` (and `.xlsx`): one row per
county, plus `sources`, `confidence` and `evidence` columns.

## 3. Use it on your own data

Each domain is a folder with three files:

```text
config/my_domain/
├── run.yaml       # how to run it
├── schema.json    # what to extract
└── targets.csv    # who to cover, one row per target
```

```bash
mkdir -p config/my_domain
cp config/TEMPLATE.yaml config/my_domain/run.yaml   # starter for search + review
# add schema.json and targets.csv, then check the setup (no cost):
pixi run psweep run --config config/my_domain/run.yaml --validate-config
```

If you already have the URLs, start from [`config/example/`](config/example/run.yaml)
instead. Your `config/<domain>/` folders are git-ignored and stay local.

## 4. Iterate

Start small, look at the results, adjust, and widen:

```bash
pixi run psweep run --config config/my_domain/run.yaml -n 3   # first 3 targets only
```

Check `outputs/my_domain/compiled/` for the table. If something is missing or
wrong, [Tuning & Iteration](docs/tuning.md#converge-a-new-domain) maps each
symptom to the setting that fixes it. When 3 targets look right, drop `-n 3`.

## How it works

`run` goes through four stages. Each one can also be run alone
(`psweep discover`, `extract`, `compile`, `validate`).

1. **discover**: find and download the documents for each target.
2. **extract**: an LLM reads each document and fills in your schema.
3. **compile**: merge the results into one table.
4. **validate** (optional): compare against other models or hand-labelled samples.

Exit codes: 0 success, 1 failure, 2 nothing found, 3 partial (re-run to retry).

## Documentation

| Page | What it covers |
|---|---|
| [Getting Started](docs/getting-started.md) | The example, then your own domain end to end |
| [Configuration Reference](docs/config-reference.md) | Every `run.yaml` key, including [required keys, defaults and optional features](docs/config-reference.md#required-keys-defaults-and-optional-features) |
| [Tuning & Iteration](docs/tuning.md) | Symptom → setting |
| [Source Patterns](docs/sources.md) | Searches, exports, listings, code hosts, audio |
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
