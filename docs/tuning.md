# Tuning & Iteration

ParseSweep is built to be tuned incrementally: start from the
[minimal config](getting-started.md#start-minimal), run it, inspect the output,
then change **one knob at a time**. This page maps each symptom you might see to
the exact setting that fixes it, its default, and how to change it.

## Extraction quality

### Output is truncated or misses later sections

Large documents overflow the context window and get cut off before the section
you want. Two levers, cheapest first:

1. **Page targeting** (recommended for big PDFs) — let the LLM find and extract
   only the relevant pages:

   ```yaml
   extraction:
     pages:
       auto_locate:
         section_description: "the section containing rate tables"
         trigger_chars: 200000        # default: 200000 — only docs above this size
         max_selected_pages: 30       # default: 30
         keywords: [rate, schedule]   # optional heuristic pre-filter
   ```

2. **Raise the context budget** (brute force, more expensive):

   ```yaml
   extraction:
     max_context: 1000000             # default: 600000
   ```

   Set `trigger_chars` equal to `max_context` for zero-regression targeting —
   only documents that *would* be truncated get page-targeted.

For a one-off file you already know, skip config and pass `--pages 615-759` on
the `extract` command, or provide a `pages.csv` with per-file ranges (CSV
ranges always win over `auto_locate`).

### Fields come back empty or wrong

The field `description`s in your schema are the model's instructions. Sharpen
them before touching anything else — see
[Schema Authoring](schemas/index.md). If a whole document type is weak,
try a stronger model tier (below).

### Extraction times out on huge documents

Raise the per-request timeout (default `120` seconds):

```yaml
extraction:
  timeout_seconds: 300
```

## Cost and speed

### Runs are too slow or too expensive

- **Use page targeting** so you send fewer characters per document (above).
- **Pick a cheaper model tier** per stage. Define aliases once, reference them
  per stage:

  ```yaml
  models:
    fast: gpt-4o-mini
    strong: gpt-4o
  extraction:
    model: fast          # cheap for bulk extraction
  validation:
    models: [fast, strong]
  ```

- **Limit a test run** with `-n 5` on `extract` to iterate quickly before a
  full pass.

Cached work is reused automatically: `extract` skips documents that already
have output JSON. Pass `--fresh` to force a re-run after you change the schema,
model tier, or page settings.

## Compilation

### The compiled sheet has duplicate rows

Deduplication identity is **schema-owned** so every stage agrees on what makes a
row unique. Set it in the schema, not the config:

```json
"$metadata": {
  "identity": {
    "deduplication": {
      "key_fields": ["item_name", "customer_class"],
      "ignore_fields": ["notes"]
    }
  }
}
```

Preview what would be merged without writing output: `psweep compile --dry-run`.

### Many documents describe the same entity

Turn on **synthesis** to reconcile many per-document records into one
authoritative row per entity:

```yaml
compilation:
  synthesis:
    enabled: true
    min_sources_for_llm: 2           # default: 2 — skip the LLM when only 1 source
    group_by: ["entity.key_field"]
```

## Discovery (web acquisition)

### Too many irrelevant documents

Tighten selection and classification keywords:

```yaml
discovery:
  selection:
    exclude: [news, blog, press release]
  document_classifier:
    required_keywords: [ordinance, permit]
```

### Too few documents

Broaden your search queries and loosen `selection.max_per_target`, or enable
`document_review` to keep the most authoritative results rather than filtering
hard up front.

### Crawl politeness / policy

`robots_mode` and `tos_mode` both default to `warn` (log and continue). Set
`block` to enforce, or `ignore` to silence. Throttle with
`runtime.min_request_interval_ms` and `runtime.max_concurrent_downloads`.

## Re-running after changes

Every stage honors previous work by default. Pass `--fresh` to ignore caches
and start over:

```bash
pixi run psweep extract --config config/my_domain/run.yaml --fresh
pixi run psweep run     --config config/my_domain/run.yaml --fresh
```

See the [Configuration Reference](config-reference.md) for the full defaults
table and the [Command Reference](commands/index.md) for every option.
