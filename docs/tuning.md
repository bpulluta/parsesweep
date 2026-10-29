# Tuning & Iteration

ParseSweep is built to be tuned incrementally: start from the
[minimal config](getting-started.md#start-with-discovery-only), run it, inspect the output,
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

Validated work is reused automatically: matching source/schema/settings and
record-byte evidence skip model calls. Source, model, scope, or schema changes
invalidate reuse; malformed or old unverifiable JSON is re-extracted. `--fresh`
forces a whole-output reset after preflight, rebuilding only the selected subset.

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

### One entity, several repeated sub-entities

If records repeat sub-entities (phases, expansions, line items), keep them as
separate rows instead of collapsing them into the entity row:

```yaml
compilation:
  synthesis:
    group_by: ["entity.key_field"]
    item_group_by: ["item_level_field"]   # one row per sub-entity per entity
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

Broaden queries or raise `search.max_results` (default `10` unique candidates
per query) and `selection.max_per_target` (default `1` acquisition per target).
Check scope and exclusion evidence before increasing budgets. Search caps do
not guarantee downloads or recall. Optional paid `document_review.keep_top`
controls retention after acquisition; review does not expand the search.

### Domain filters or partitions changed unexpectedly

Use canonical `search`, `max_results`, and selection
`max_per_target`/`require_any`/`exclude`. Retired aliases fail with migration
guidance, rather than merging into canonical settings. Adjacent section
YAML/JSON overrides beside a `run` config are also retired: move settings into
its explicit sections and remove the adjacent files.

There are no built-in legal draft, vendor authority, or shopping-path lists.
Use `selection.draft_patterns` with `exclude_draft`,
`link_prioritization.domain_scores`, and `shopping_path_keywords` for observed
domain mismatches. `link_prioritization.keywords` replaces the generic fallback.
Concrete provider/vendor source filters belong in the domain config; parsers
do not add them automatically. `allowed_domains` plus URL rules enforce source
scope; soft ranking boosts are not an allowlist.

Supply `state`/`jurisdiction` fields explicitly, or use `partition_by` for
arbitrary metadata layouts. Query text never infers geography and discovery
has no built-in US alias table; supply any `jurisdiction_aliases` yourself.

### Crawl politeness / policy

`policy.robots_mode` and `policy.tos_mode` default to `warn` (log and continue).
Set `enforce` for HTTP enforcement or `ignore` to silence. Strict enforcement
cannot certify browser redirects or subresources: it fails before unsupported
browser navigation, including escalation. Use HTTP acquisition when enforcement
is required. Throttle with `runtime.min_request_interval_ms` and
`runtime.max_concurrent_downloads`.

SerpApi/download TLS verification remains the user's environment choice;
`SERPAPI_SSL_VERIFY` and `DISCOVERY_SSL_VERIFY` retain default `false`.
Configure verification explicitly (and `REQUESTS_CA_BUNDLE` if needed);
validation does not test live certificate chains.

### Automation reports no downloads

Inspect manifest `status` and `errors`. Quiet/default/verbose modes share exit
codes: error-free `success`/`dry_run`/`reused` = `0`, `failed`/`partial_failure`
(including zero downloads caused by failure) = `1`, genuinely `empty` = `2`.
A partial result is not successful acquisition.

## Re-running after changes

Discovery checkpoint v2 reuse requires matching effective config and artifact
digest evidence, including curated files. Changed settings, missing/altered
artifacts, and old v1 entries cause reacquisition; opt-in search caching is
separate. Publication locks reject concurrent corpus writers; journal recovery
restores interrupted discovery curated/provenance swaps on the next writer.
Do not treat that small protocol as a general pipeline transaction.

Extraction validates reuse evidence instead of trusting JSON existence.
`extract --fresh` resets its entire resolved output after successful preflight;
with a limit/filter, only that subset is rebuilt. Invalid input/schema/scope,
missing required credentials, or overlapping paths preserve existing output.
`run --fresh` also bypasses discovery checkpoints and enabled search cache:

```bash
pixi run psweep extract --config config/my_domain/run.yaml --fresh
pixi run psweep run     --config config/my_domain/run.yaml --fresh
```

OCR cache identity is separate from existing output JSON: it uses
`.text/<filename>.txt` and `.text/<filename>.meta.json`. Source size/mtime and
source/text digests must match; partial or stale pairs are misses, metadata is
replaced last, and old stem-only caches are not reused. LLM review sidecars use
`.review/<filename>.json`; curation carries these full-name pairs/sidecars.
Raw OCR is cached before current correction rules are applied, so changing
rules does not repeat OCR. Old corrected OCR caches are misses. `.pages/` and
`.sections/` products verify source content and resolved model/settings; full-name
section pairs also validate their text digest. Delete these directories for a
cold OCR/targeting run; they are separate from record reuse.

See the [Configuration Reference](config-reference.md) for the full defaults
table and the [Command Reference](commands/index.md) for every option.
