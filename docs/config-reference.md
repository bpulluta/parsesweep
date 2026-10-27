# Configuration Reference

A ParseSweep run is driven by a single YAML config. Each command reads only its
own section, so a config can contain any subset of `extraction`, `compilation`,
`discovery`, and `validation`.

## Start small, add as you need

Only two keys are required to extract — `schema` and `input_dir`:

```yaml
extraction:
  schema: schemas/personal/my_schema.json
  input_dir: documents/my_domain
```

Everything else has a default (see the table below). Add a key only when you
have a reason to change its default — the [Tuning & Iteration guide](tuning.md)
tells you which key to reach for and when.

## Defaults at a glance

These are the values that apply when you omit a key. They come from single
source-of-truth constants in the code, so `--help` and runtime never drift.

| Setting | Section | Default | Change it when… |
|---------|---------|---------|-----------------|
| `model` | extraction | `.env` model → `gpt-4o-mini` | You want a stronger/cheaper tier |
| `max_context` | extraction | `600000` | Output is truncated on large docs |
| `output_dir` | extraction | `extracted/<domain>` | You want a custom location |
| `skip_existing` | extraction | `true` | Never (use `--fresh` to re-run) |
| `timeout_seconds` | extraction | `120` (`LLM_TIMEOUT` env) | Very large docs time out |
| `provider` | extraction | `auto` | Auto-detection picks the wrong one |
| `pages.auto_locate.trigger_chars` | extraction | `200000` | Target only docs above a size |
| `pages.auto_locate.max_selected_pages` | extraction | `30` | Sections span more/fewer pages |
| `output.default_format` | compilation | `both` when unset | You want only `csv` or `excel` |
| `synthesis.min_sources_for_llm` | compilation | `2` | Tune when the LLM reconciler runs |
| dedup `key_fields` | schema `$metadata.identity.deduplication` | none | Compiled sheet has duplicate rows |
| `partition_mode` | discovery | `auto` | You need a specific output layout |
| `policy.robots_mode` | discovery | `warn` | Enforce (`block`) or silence (`ignore`) |
| `policy.tos_mode` | discovery | `warn` | Enforce (`block`) or silence (`ignore`) |

The [Command Reference](commands/index.md) also renders every CLI option's
default directly from the live command definitions.

### Advanced: column-flattening overrides

When compiling nested records into flat spreadsheet columns, ParseSweep uses a
set of heuristic field-name defaults (which fields name a *type*, hold a
*value*, carry a *unit*, and so on). These ship tuned for tabular/rate-style
data; if your domain names fields differently, override them per-schema in the
schema's `$metadata.compilation.flattening` block (e.g. `type_fields`,
`value_fields`, `unit_fields`, `skip_fields`) — no code changes needed. Every
knob falls back to the built-in default when omitted.

## Schema-driven compilation

Output columns come from the domain schema, not a fixed opinion, project, or
geographic record layout. A manual can declare numeric measurements and units;
a regulation can declare requirements; a public-comment schema can declare
sentiment and evidence. Fields absent from that domain are not invented as
empty consumer columns.

Use the existing `compilation.output` settings to rename, prioritize, or exclude
columns. For example, a measurement schema declaring `manufacturer`,
`rated_power_kw`, and `measurement_quote` can use:

```yaml
compilation:
  output:
    default_format: csv
    column_renames:
      Manufacturer: manufacturer
      Rated Power Kw: rated_power_kw
      Measurement Quote: evidence
    column_order: [manufacturer, rated_power_kw, evidence]
```

`column_order` is a priority prefix, not an exact column selection; remaining
schema fields are retained. `exclude_fields` removes unwanted fields. Nested
flattening uses the schema's existing `compilation.flattening` settings.
Canonical extraction JSON remains the source for lossless arrays/objects,
quality warnings, coverage, document identity, and lineage. Spreadsheet output
is a presentation, not a replacement for that record.

Runtime compilation settings override schema `$metadata.compilation` settings.
With no format setting, CLI and Python API emit CSV and Excel. Compilation and
`run --validate-config` validate the merged contract before scanning records,
calling synthesis models, or clearing outputs with `--fresh`. Excel batches
above 1,048,575 data rows or with cells over 32,767 characters fail before
either export; select CSV to preserve full evidence. Requested files are written
to staging before publication; caught writer/publication failures restore prior
output where possible. If restoration fails, recoverable backups are retained
and their paths appear in the error. This is not a crash-safe multi-file
transaction: do not run concurrent writers into the same output directory.

Semantic summaries and classifications belong in the extraction schema's
instructions and constraints. Existing `compilation.synthesis` handles
cross-document reconciliation. Deterministic code validates structure,
provenance, identity, and export contracts; JSON Schema validation, including
declared calendar-date formats, does **not** certify quotation authenticity,
sentiment, or geographical support. US county-FIPS derivation remains an
explicit, optional wide-output normalization, not a requirement of any domain.

Page/section selection and context truncation are preserved as limitations in
canonical records and extraction manifests. Incomplete searches do not replace
previous good curated partitions. A seeded or capped sample is not a recall or
population estimate.

### Scale boundary

The compiler prepares one export frame for CSV/Excel, but global identity and
deduplication still require an in-memory frame. Complete exact/fuzzy keys use
indexed grouping rather than pairwise scans. Null-as-wildcard fuzzy matching
also uses indexed candidates while preserving ordered matching semantics;
highly ambiguous multidimensional identity buckets can still be expensive.
Prefer CSV for large batches; benchmark representative record sizes and
duplicate distributions before scaling. Schemas without
identity keys do not deduplicate; choose domain-appropriate keys explicitly
rather than inferring them in core code. Configured keys missing from both the
schema and compiled columns fail instead of silently deduplicating on fewer
fields. Atomic checkpoint snapshots avoid repeated whole-file writes, and
review caches avoid repeated parsing/model calls when grading inputs are
unchanged. Neither feature bounds acquisition latency or whole-pipeline
memory. Synthetic record, checkpoint, and mocked timeout tests do not establish
100,000-target, large-document, fuzzy-dedup, or live-API readiness.

## Annotated template

Copy the annotated template below to `config/<your_domain>/run.yaml` and edit
the sections you need. It documents every knob inline.

```{literalinclude} ../config/TEMPLATE.yaml
:language: yaml
:caption: config/TEMPLATE.yaml
```
