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
| `output.default_format` | compilation | `excel` | You want `csv` or `both` |
| `synthesis.min_sources_for_llm` | compilation | `2` | Tune when the LLM reconciler runs |
| dedup `key_fields` | schema `$metadata.identity.deduplication` | none | Compiled sheet has duplicate rows |
| `partition_mode` | discovery | `auto` | You need a specific output layout |
| `policy.robots_mode` | discovery | `warn` | Enforce (`block`) or silence (`ignore`) |
| `policy.tos_mode` | discovery | `warn` | Enforce (`block`) or silence (`ignore`) |

The [Command Reference](commands/index.md) also renders every CLI option's
default directly from the live command definitions.

## Annotated template

Copy the annotated template below to `config/<your_domain>/run.yaml` and edit
the sections you need. It documents every knob inline.

```{literalinclude} ../config/TEMPLATE.yaml
:language: yaml
:caption: config/TEMPLATE.yaml
```
