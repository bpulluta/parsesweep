# Getting Started

This tutorial builds one domain end to end: find documents, extract fields,
compile a table and check its quality. Every key used here is described in the
[Configuration Reference](config-reference.md).

## 1. Install

```bash
curl -fsSL https://pixi.sh/install.sh | bash      # pixi, if you don't have it
git clone https://github.com/bpulluta/parsesweep.git && cd parsesweep
pixi install --locked
```

## 2. Credentials

Credentials live in `.env` in the project root (or the environment), never in
YAML. `pixi run psweep init` writes them interactively.

```bash
# .env
SERPAPI_API_KEY=your-serpapi-key       # web search (discovery queries)

# exactly one LLM provider, for extraction, review and validation:
OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=... | GEMINI_API_KEY=... |
# AZURE_OPENAI_API_KEY=... + AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_VERSION |
# LLM_BASE_URL=https://proxy.example.com/v1 + LLM_API_KEY=...  (OpenAI-compatible proxy)
```

## 3. Discover documents

```bash
mkdir -p config/my_domain
cp config/TEMPLATE.yaml config/my_domain/run.yaml
```

Describe your targets in `config/my_domain/targets.csv` (one row each; the
columns are available to query templates) and write queries for them:

```yaml
domain: my_domain
discovery:
  targets: targets.csv
  partition_by: [state, name]
  queries:
    - '"{name}" {state} installation manual filetype:pdf'
```

If you already know the URLs, use `sources` instead of `queries`; that needs
no search key. Other ways to reach data (exports, listings, code hosts,
recordings) are in [Source Patterns](sources.md).

```bash
pixi run psweep discover --config config/my_domain/run.yaml --validate-config
pixi run psweep discover --config config/my_domain/run.yaml -n 3
```

`--validate-config` checks the config offline (no credentials or network);
`-n 3` runs the first three targets. Inspect the result:

- `outputs/my_domain/discovered/runs/<run_id>/manifest.json` — status, errors
  and every candidate's fate
- `outputs/my_domain/discovered/curated/` — the documents later stages read

Check that the acquired documents are the ones you need before going further;
[Tuning](tuning.md) maps common problems to settings.

## 4. Write the schema

`config/my_domain/schema.json` defines what to extract. The only required
metadata is the main array:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$metadata": {
    "extraction": {"main_data_array": "items"},
    "identity": {"deduplication": {"key_fields": ["state", "name"]}}
  },
  "type": "object",
  "properties": {
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "state": {"type": "string", "description": "Two-letter state code of the target"},
          "name": {"type": "string", "description": "Target name exactly as in targets.csv"},
          "max_flow_gpm": {
            "type": ["number", "null"],
            "description": "Maximum rated flow in gallons per minute; null if not stated"
          }
        }
      }
    }
  }
}
```

Because the deduplication `key_fields` equal `partition_by`, extraction and
compilation are anchored to the target roster
([roster mode](config-reference.md#roster-anchored-extraction)). Field
descriptions are the model's instructions. `psweep init-domain-schema`
scaffolds a schema, and `psweep check-schema` reviews one; see
[Schema Authoring](schemas/index.md).

## 5. Extract and compile

```bash
pixi run psweep extract --config config/my_domain/run.yaml -n 2
pixi run psweep compile --config config/my_domain/run.yaml
```

Extraction writes one JSON per document to `outputs/my_domain/extracted/`;
open a couple before scaling up. Compilation writes
`outputs/my_domain/compiled/my_domain.csv` (and/or `.xlsx`). With a target
roster it emits one row per target with `sources`, `confidence` and
`evidence`.

For local documents, skip discovery and pass the folder:
`psweep extract documents/my_domain/ --config …`.

## 6. Validate

Label a few targets by hand in `config/my_domain/samples/exemplar.csv`
(headers matching the compiled columns), then:

```yaml
validation:
  references: [samples/exemplar.csv]
  min_accuracy: 0.9
```

```bash
pixi run psweep validate --config config/my_domain/run.yaml
```

The console prints a leaderboard and a `Next:` line;
`outputs/my_domain/validated/` holds the per-cell results. The three-step
workflow (exemplar → model comparison → blind sample) is in
[Tuning](tuning.md#validation).

## 7. Run everything

```bash
pixi run psweep run --config config/my_domain/run.yaml
```

`run` executes every configured stage in order. Re-running reuses verified
work and resumes where the last run stopped; exit code `3` means some items
are still incomplete and a re-run will retry them. See
[Runs, reuse and exit codes](config-reference.md#runs-reuse-and-exit-codes).
