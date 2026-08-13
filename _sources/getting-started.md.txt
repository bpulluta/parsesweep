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

## Your first extraction

Every command takes `--config`, and each command reads its own section:

```bash
# Extract structured JSON from local documents
pixi run psweep extract --config config/my_domain/run.yaml

# Compile the extractions into an Excel/CSV spreadsheet
pixi run psweep compile --config config/my_domain/run.yaml
```

Or run the whole pipeline (discover → extract → compile) at once:

```bash
pixi run psweep run --config config/my_domain/run.yaml
```

See the [Command Reference](commands/index.md) for every command, option, and
its default.
