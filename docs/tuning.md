# Tuning & Iteration

Run a small slice (`psweep run --config … -n 3`), inspect the output, then
change **one setting at a time**. Each symptom below names the setting that
addresses it; defaults and exact rules are in the
[Configuration Reference](config-reference.md).

## Converge a new domain

1. **Start minimal.** Copy `config/TEMPLATE.yaml` (search and review) or
   `config/example/` (known URLs). Set the required keys and run
   `psweep run --config … --validate-config`.
2. **Run a slice:** `psweep run --config … -n 3`. Re-runs of the same slice are
   cheap: searches and grades are cached, and unchanged documents are reused.
3. **Measure:**
   - cost per stage in `compiled/run_accounting.json`,
   - discovery quality in `review.csv`,
   - accuracy against 3-5 hand-labelled targets in `samples/`, with
     `validation.references` and `min_accuracy` set.
4. **Change one setting** using the symptom sections below. Try the cheapest
   lever first (query, filter, description) before a bigger model, more pages
   or more documents.
5. **Converged** means:
   - the accuracy gate passes on targets you did not tune on (`validate --sample N`),
   - cost per target is within budget,
   - and the run ends without `partial_failure`.
6. **Scale:** run the full roster, with `runtime.max_search_calls` at roughly
   targets × queries and `runtime.max_review_calls` at roughly
   3 × `max_per_target` as attempt ceilings. A review ceiling below a target's
   need leaves it incomplete and keeps its older documents.
7. **Keep the config honest:** every remaining key traces to a measurement;
   delete keys that restate a built-in default. Exception: do not touch
   `extraction.timeout_seconds` or `save_discovered` once records exist (they
   are part of the reuse fingerprint).

## Discovery

### Too many irrelevant documents

- Tighten candidate selection: `selection.require_any`, `selection.exclude`,
  `exclude_url_patterns`, `exclude_text_patterns`, `draft_patterns` with
  `exclude_draft`.
- Require the target in the candidate: `selection.target_require_any:
  ["{jurisdiction}"]` (templates are rendered per target).
- Restrict sources with `crawl.allowed_domains` plus `include_url_patterns`.
  Ranking boosts (`link_prioritization.domain_scores`) prefer hosts but never
  exclude others.
- Check content after download with `document_classifier` (`action: filter`
  drops keyword failures before paid review), or add paid LLM curation with
  `document_review`.

### Too few documents

- Broaden or add `queries`; for Google add `search.params: {udm: web}`.
- Queries that rarely find qualifying documents belong in
  `search.fallback_queries`, which runs only for targets still below `keep_top`.
- Raise `search.max_results` (candidates per query) and
  `selection.max_per_target` (downloads per target). `document_review.keep_top`
  only limits what is kept afterwards.
- If the data lives on listings, exports or code hosts rather than search
  results, use a [source pattern](sources.md).
- A target with no results is not evidence that no document exists; report
  targets with reviewed documents separately from targets searched.

### Pages are empty or refused

- JavaScript shells or 401/403/429/451 responses: `crawl.browser_escalation:
  true` (browser requirements in [Security](security.md)).
- Municode: `crawl.code_host_adapters: true`. Search often returns a table of
  contents; put the chapter's `codes/...?nodeId=<section>` URL in the target's
  `urls` instead.

### Politeness and policy

Throttle with `runtime.min_request_interval_ms` and
`runtime.max_concurrent_downloads`; set `policy.robots_mode`/`tos_mode` to
`enforce` when required (HTTP acquisition only).

### A run ends with exit code 3

Some items failed transiently and stay incomplete. Re-run the same command:
completed work is reused and only the rest is fetched. The run notes and
`manifest.json` `errors` say what is outstanding.

## Extraction

### Output is truncated or misses later sections

1. **Page targeting** for big PDFs:

   ```yaml
   extraction:
     pages:
       auto_locate:
         section_description: "the section containing rate tables"
   ```

   Setting `trigger_chars` equal to `max_context` targets only documents that
   would otherwise be truncated.
2. **Raise `max_context`** (default `600000` characters) — more expensive.

Page targeting also adds the parts the section refers to, such as riders or
charge sheets on other pages. If a run warns that it left out relevant pages,
raise `max_selected_pages`.

For one known file, `extract --pages 27,68-73` or `pages.csv` rows.

### Fields come back empty or wrong

The schema's field descriptions are the model's instructions: sharpen them
first ([Schema Authoring](schemas/best-practices.md)), then try a stronger
model alias.

### Extraction times out

While extracting, every running document has a row under the progress bar
with its own timer, and each prints a line with its item count and time as
soon as it finishes. The bar reads `Preparing` while page targeting runs. A
call must return its whole answer within the timeout. Otherwise it is resent,
up to 4 attempts in total, and each retry logs a warning that names the
document, such as `tariffs.pdf: gpt-4o did not finish within the 600s timeout;
retrying in 2s (attempt 2/4)`. Every attempt resends the whole prompt and may
be billed, and a model that needs longer than the timeout usually needs it
again.

Response time grows mostly with the answer, because every item is written out
in full: a one-page sheet that lists many items can take minutes, and page
targeting only trims long inputs. First narrow the schema's scope to the
subset you need, such as one customer class. If calls still run long, raise
`extraction.timeout_seconds` (default `600`, or `LLM_TIMEOUT`). Add `--debug`
to log each request's size, response time and token counts.

## Cost and speed

- Page targeting sends fewer characters per document.
- Define model aliases once and pick a cheaper one per stage:

  ```yaml
  models:
    fast: gpt-4o-mini
    strong: gpt-4o
  extraction:
    model: strong
  discovery:
    document_review:
      model: fast
  ```

- Iterate on a slice: `-n` on `run`/`discover` (first N targets) or `extract`
  (first N documents). `psweep estimate <dir> --config <run.yaml>` prices a
  batch beforehand with the configured `extraction.model`; it counts the
  documents extraction would read, and ignores reuse, targeting, review and
  search spend.
- Re-runs reuse verified work and make no model calls for unchanged inputs.
  Per-stage costs are in `outputs/<domain>/compiled/run_accounting.json`.
- `psweep run --yes` skips the confirmation prompt for unattended runs.
- Settle `extraction.timeout_seconds` and `pages.auto_locate.save_discovered`
  before a large run: they are part of the extraction reuse fingerprint, so
  adding, changing or removing either re-extracts every document (paid).

## Compilation

### Duplicate rows

Deduplication identity is schema-owned: set
`$metadata.identity.deduplication.key_fields`. Preview merges without writing:
`psweep compile --config … --dry-run`.

### Many documents describe the same entity

Declare the entities as a roster (`discovery.targets` + `partition_by`):
compile then emits one row per target with `sources`, `confidence` and
`evidence`, reconciling values across that target's documents.

## Validation

1. **Converge on an exemplar.** Label a few targets by hand in
   `config/<domain>/samples/`, list the sheet under `validation.references`
   and set `min_accuracy`. Each miss is attributed to discovery (`no_docs`) or
   extraction (`extraction`); fix that stage and re-run until the gate passes.
2. **Compare models.** Add contender `validation.models` and a `judge`
   (optionally focused with `fields`). The judge sees only cells where
   contenders disagree. The leaderboard ranks consensus F1, quote support and
   cost per document.
3. **Scale.** `psweep validate --sample N` writes a blank labelling sheet for
   N random targets; fill it in blind, add it to `references`, and accuracy is
   reported with a 95% Wilson interval.

The console's `Next:` line points at the largest gap.
