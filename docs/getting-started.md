# Getting Started

## Install

ParseSweep uses [pixi](https://pixi.sh) to manage its environment.

```bash
# Install pixi (if you don't have it)
curl -fsSL https://pixi.sh/install.sh | bash

# Clone and install
git clone https://github.com/bpulluta/parsesweep.git && cd parsesweep
pixi install
```

## Start with discovery only

Discovery needs a YAML config, not an extraction schema or LLM credentials.
Set your SerpApi credential in the environment or the project's `.env` file:

```bash
# .env — keep credentials out of YAML and version control
SERPAPI_API_KEY=your-serpapi-key
```

Copy the short template, then replace its domain and query with your own:

```bash
mkdir -p config/my_domain
cp config/TEMPLATE.yaml config/my_domain/run.yaml
```

The active config can be as small as:

```yaml
domain: my_domain
discovery:
  queries:
    - "industrial pump installation manual filetype:pdf"
```

`discovery.queries` auto-enables SerpApi when search is omitted. An explicit
`search.enabled: false` disables it. If you already
know the document URLs, replace `queries` with `seeds`; seed-only discovery
does not auto-enable search and needs no search credential:

```yaml
domain: my_domain
discovery:
  seeds:
    - https://example.org/documents/manual.pdf
```

Validate the local input plan, then acquire documents:

```bash
pixi run psweep discover --config config/my_domain/run.yaml --validate-config
pixi run psweep discover --config config/my_domain/run.yaml
```

Validation makes no provider/network calls or run artifacts. Passing it does
not confirm credentials, provider availability, or live network behavior.
Only the explicitly selected config is loaded. Existing adjacent section
YAML/JSON overrides beside a `run` config now fail: move their settings into
the corresponding sections of the run file and remove the adjacent files, or
load a section-wrapped config explicitly with `--config`.

### Inspect what was acquired

Open the run manifest and documents under
`discovered/my_domain/runs/<run_id>/`; that run's selected files are in
`curated/`. The consolidated downstream folder is
`discovered/my_domain/curated/`.

Discovery exit codes are the same in quiet, default, and verbose modes:
`success`/`dry_run`/validated `reused` = **0**; `failed`/`partial_failure` = **1**
(including failures with zero downloads); genuinely `empty` results = **2**.
Inspect the manifest status/errors, not only the file count.

The small setup uses these defaults:

- **10 unique candidates per query**, capped locally across live, cached, and
  enabled news results. Provider `num` does not override that cap. There is no
  hidden pagination or refill, and 10 candidates does not mean 10 downloads.
- **1 acquisition per target** (`selection.max_per_target`), selected by
  heuristics. This is not LLM relevance review or a completeness guarantee.
- Direct acquisition with **no crawl**, concurrency **2**, retry attempts **3**,
  and robots/ToS policy modes **warn**.
- Search cache **off**. When explicitly enabled, cache hits make zero live
  search calls.
- Browser mode, browser escalation, and code-host adapters **off**. Opt into
  recovery with `browser_escalation: {enabled: true}` and/or
  `code_host_adapters: {enabled: true}`; threshold-only blocks do not enable them.
- No automatic LLM calls. `document_review` is an explicit optional paid stage
  requiring a document description, model choice, and LLM credentials.

PDF candidates are preferred to web pages when present. HTML/HTM and
extensionless HTTP(S) URLs are provisionally eligible without disabling
`require_supported_document`; known non-document binaries are excluded, and
actual GET/MIME checks verify ambiguous URLs. Eligibility is not semantic
validation: inspect whether the acquired document is the one you need.

### Add only the choices you need

To change the candidate budget or acquisition count, add `search.max_results`
or `selection.max_per_target`. To restrict a source, combine a host allowlist
with a URL-path regex:

```yaml
discovery:
  queries:
    - "site:example.org installation manual filetype:pdf"
  allowed_domains: [example.org]
  discovery_rules:
    include_url_patterns: ['^https?://(?:www\.)?example\.org/documents/']
```

Scope checks apply before top-one selection and to final redirects. Search
`site:`/`filetype:` hints alone do not guarantee source or file type. Concrete
provider/vendor filters are domain-config choices, not settings automatically
inferred from a document parser.

For existing configs, migrate `seeker` to `search`, `max_results_per_query` to
`max_results`, and selection `primary_per_target`/`relevance_require_any_terms`/
`relevance_exclude_any_terms` to `max_per_target`/`require_any`/`exclude`.
Retired aliases fail even when canonical keys coexist. There are no built-in
legal draft, vendor authority, or shopping-path lists; configure those domain
preferences explicitly. Supply `jurisdiction` and `state` fields or use
`partition_by`: query text does not infer geography, and discovery has no
built-in US alias table.

HTTP robots/ToS enforcement is supported. Strict enforcement cannot certify
browser redirects or subresources and fails before unsupported browser
navigation. Use HTTP acquisition when enforcement is required.

SerpApi and download TLS verification defaults remain `false`. Environment
options `SERPAPI_SSL_VERIFY`, `DISCOVERY_SSL_VERIFY`, and `REQUESTS_CA_BUNDLE`
control verification and the Requests CA bundle; these are independent of
config validation.

For CSV targets, optional keyword classification, LLM review, crawling, or
other stage settings, use the [Configuration Reference](config-reference.md)
and the expanded `config/ADVANCED.yaml`. Neither a seeded sample nor a capped
search establishes recall or population coverage.

### Resume and document sidecars

Checkpoint v2 reuse requires matching effective config and artifact digest
evidence, including curated files. Changed settings, missing/altered files,
and old v1 entries cause reacquisition. Search-result caching is separate.
Publication locks reject concurrent corpus writers; a discovery journal lets
the next writer recover interrupted curated/provenance swaps. This is not a
general pipeline transaction.

OCR caches use `.text/<filename>.txt` and `.text/<filename>.meta.json`;
review sidecars use `.review/<filename>.json`. Full filenames avoid collisions
between formats with the same stem. OCR readers verify source size/mtime and
source/text digests; partial pairs are misses and old stem-only caches are not
reused. Metadata is replaced last. Curation carries these sidecars with selected
files; native extraction and OCR correction rules are unchanged.

## Add extraction when you have suitable documents

Extraction and compilation use two files per domain:

| File | Role |
|------|------|
| **Schema** (JSON) | Defines *what* to extract — field names, types, descriptions |
| **Config** (YAML) | Defines *how* to run — paths, models, page targeting, output |

The existing `init-domain-schema` command scaffolds a **schema**; it is not a
discovery setup wizard. Discovery does not require it. Keep project-specific
schemas locally under `schemas/personal/`; see
[Schema Authoring](schemas/index.md) for the contract and instructions.

### Configure LLM credentials

ParseSweep reads LLM credentials from a `.env` file in the project root or
from environment variables. Configure **exactly one** provider block when
using extraction, LLM document review, or another LLM stage.

Run the interactive credential setup:

```bash
pixi run psweep init
```

...or create `.env` by hand. An OpenAI-compatible proxy (LiteLLM, OpenRouter,
Azure AI Foundry, Together AI, vLLM, Ollama, …) can use the endpoint override:

```bash
# .env — endpoint override (highest priority)
LLM_BASE_URL=https://your-proxy.example.com/v1
LLM_API_KEY=sk-your-proxy-key
LLM_MODEL=gpt-4.1-mini     # optional default; overridden by run.yaml models:
```

Or use direct provider keys — one of:

```bash
AZURE_OPENAI_API_KEY=...      # + AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_VERSION
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
```

### Create a minimal schema

Only `$metadata.extraction.main_data_array` and `identifier_fields` are
required metadata keys. For example, `schemas/personal/my_schema.json`:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$metadata": {
    "extraction": {
      "main_data_array": "items",
      "identifier_fields": ["item_name"]
    }
  },
  "type": "object",
  "properties": {
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "item_name": {"type": "string", "description": "Name of the item"},
          "value": {"type": "number", "description": "The value to extract"}
        }
      }
    }
  }
}
```

The field `description`s are the LLM's extraction instructions — write them
precisely. This example illustrates the contract; it does not certify the
semantic quality of extracted values.

### Add extraction to the same config

Only `schema` and `input_dir` are required. `output_dir`, `max_context`, and
other settings have defaults; pin `model` when you want an explicit choice.
For discovered inputs:

```yaml
extraction:
  schema: schemas/personal/my_schema.json
  input_dir: discovered/my_domain/curated
  output_dir: extracted/my_domain
  model: your-model-name
```

For local documents, use `input_dir: documents/my_domain` instead. You can also
test schema-only mode without a config:

```bash
pixi run psweep extract documents/my_domain/ \
  --schema schemas/personal/my_schema.json -n 2

# Config-driven extraction
pixi run psweep extract --config config/my_domain/run.yaml
```

## Inspect, then iterate

Extraction writes one JSON per document to `extracted/<domain>/`. Open a couple
and check the results, then tune **one setting at a time**:

- Output looks **truncated / missing later sections** → raise `max_context` or
  turn on page targeting.
- Documents are **large or slow/expensive** → consider `pages.auto_locate`.
- The compiled sheet has **duplicate rows** → set the schema's dedup key fields.
- Discovery acquired the **wrong document** → inspect source scope, ranking,
  and query terms before increasing result/acquisition limits or adding paid review.

The [Tuning & Iteration guide](tuning.md) maps symptoms to settings, and the
[Configuration Reference](config-reference.md) documents defaults and advanced
options.

## Compile and run the whole pipeline

Add compilation only when you are ready to use extracted records:

```yaml
compilation:
  schema: schemas/personal/my_schema.json
  input_dir: extracted/my_domain
  output_dir: compiled/my_domain
```

```bash
# Merge per-document JSON into Excel/CSV
pixi run psweep compile --config config/my_domain/run.yaml

# With the relevant sections configured: discover → extract → compile
pixi run psweep run --config config/my_domain/run.yaml
```

See the [Command Reference](commands/index.md) for every command, option, and
its default.
