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

## Configure credentials

ParseSweep reads LLM credentials from a `.env` file in the project root (or
from environment variables). Configure **exactly one** provider block.

Run the interactive setup:

```bash
pixi run psweep init
```

...or create `.env` by hand. Any OpenAI-compatible proxy (LiteLLM, OpenRouter,
Azure AI Foundry, Together AI, vLLM, Ollama, …) works via the endpoint
override:

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

## Two files per domain

| File | Role |
|------|------|
| **Schema** (JSON) | Defines *what* to extract — field names, types, descriptions |
| **Config** (YAML) | Defines *how* to run — paths, page targeting, dedup, output |

See the [Configuration Reference](config-reference.md) for the annotated config
template and [Schema Authoring](schemas/index.md) for schema best practices.

## Start minimal

You do not need a full config to begin. The smallest useful setup is a schema
with two required metadata keys plus a two-line config.

**1. A minimal schema** (`schemas/personal/my_schema.json`). Only
`$metadata.extraction.main_data_array` and `identifier_fields` are required —
everything else is optional:

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
precisely. See [Schema Authoring](schemas/index.md) for the full guide.

**2. A minimal config** (`config/my_domain/run.yaml`). Only `schema` and
`input_dir` are required; `model`, `output_dir`, `max_context`, and everything
else fall back to sensible defaults:

```yaml
extraction:
  schema: schemas/personal/my_schema.json
  input_dir: documents/my_domain
```

**3. Run it** (schema-only mode skips the config entirely, ideal for a first test):

```bash
# Quick test: point extract straight at a folder + schema, limit to 2 docs
pixi run psweep extract documents/my_domain/ \
  --schema schemas/personal/my_schema.json -n 2

# Or the config-driven run (picks up every setting)
pixi run psweep extract --config config/my_domain/run.yaml
```

## Inspect, then iterate

Extraction writes one JSON per document to `extracted/<domain>/`. Open a couple
and check the results, then tune **one knob at a time**:

- Output looks **truncated / missing later sections** → raise `max_context` or
  turn on page targeting.
- Documents are **large or slow/expensive** → add `pages.auto_locate`.
- The compiled sheet has **duplicate rows** → set the schema's dedup key fields.
- Discovery finds **too many or too few** documents → tune keywords.

Each of these has a default you can see and a knob you can change — the
[Tuning & Iteration guide](tuning.md) maps every symptom to the exact setting,
and the [Configuration Reference](config-reference.md) lists every default.

## Compile and run the whole pipeline

```bash
# Merge the per-document JSON into one Excel/CSV
pixi run psweep compile --config config/my_domain/run.yaml

# Or run discover → extract → compile in one shot
pixi run psweep run --config config/my_domain/run.yaml
```

See the [Command Reference](commands/index.md) for every command, option, and
its default.
