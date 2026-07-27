---
name: greenfield-domain-onboarding
description: "Use when a user only has raw documents for a new domain and needs the full current-state ParseSweep pipeline created, tested, and iterated."
---

# Greenfield Domain Onboarding

## Goal

Turn a raw `documents/<domain>/` folder into a working ParseSweep pipeline with a schema, config, validation evidence, and a first extract/compile pass.

## When To Use

- The user is starting from scratch with only documents.
- A new domain (regulatory filings, permits, tariffs, ordinances, etc.) needs onboarding.
- The user wants a repeatable domain-bootstrap workflow driven end-to-end.

## Required Workflow

1. Read `AGENTS.md`, `README.md`, and `schemas/SCHEMA_BEST_PRACTICES.md`.
2. Inspect a small representative sample of source documents.
3. Identify the 4–8 highest-value fields needed in the first pass.
4. Create a lean schema under `schemas/personal/` with valid `$metadata.extraction`, top-level context objects, and a compact main data array.
   - Use `init-domain-schema` with a closest reference schema as the starting point rather than copying a production schema wholesale.
   - If the user can already name the first-pass fields, pass `--include-field` to trim the starter immediately.
5. Validate the schema:
   ```bash
   pixi run psweep check-schema schemas/personal/<schema>.json
   ```
6. Run a schema-only smoke extraction on 1–2 documents (no config required at this stage):
   ```bash
   pixi run psweep extract documents/<domain>/ \
     --schema schemas/personal/<schema>.json \
     -n 2 --reprocess
   ```
7. Compile the smoke sample and preview deduplication:
   ```bash
   pixi run psweep compile extracted/<domain>/ \
     --schema schemas/personal/<schema>.json \
     --dry-run
   ```
8. Review the smoke output:
   - Is `main_data_array` producing the right rows?
   - Are `identifier_fields` pointing to the right document-level context?
   - Are `key_fields` keeping distinct records separate?
   - Does the dry-run dedup report flag any unintended merges?
9. Iterate on schema fields, descriptions, and page ranges before wiring up the full config.
10. Once the schema shape is stable, scaffold the config from the template:
    ```bash
    cp config/TEMPLATE.yaml config/<domain>/run.yaml
    ```
    Populate `domain`, `extraction.schema`, `extraction.input_dir`, `compilation.schema`, `compilation.input_dir`, and `compilation.deduplication.key_fields` at minimum.
    - `extraction.input_dir` should be `discovered/<domain>/curated` (consolidated across runs).
    - `compilation.output` section owns ALL presentation: column_renames, column_order, exclude_fields, freeze_columns, auto_width.
    - The schema owns ONLY data structure: field definitions, deduplication key_fields.
11. Run the full pipeline via a single command:
    ```bash
    pixi run psweep run --config config/<domain>/run.yaml
    ```
    Or step-by-step for debugging:
    ```bash
    pixi run psweep discover --config config/<domain>/run.yaml
    pixi run psweep extract --config config/<domain>/run.yaml
    pixi run psweep compile --config config/<domain>/run.yaml
    ```
12. Expand the schema only after the first config pass shows what is missing or too ambiguous.
13. If the user needs stronger validation, run QA/QC compare:
    ```bash
    pixi run psweep extract --config config/<domain>/run.yaml --enable-qa-qc
    pixi run psweep compare  --config config/<domain>/run.yaml
    ```

## Guardrails

- The system is two files per domain: a JSON schema (what to extract) and a YAML config (how to run). Do not introduce any other runtime layer unless the user explicitly needs shared QA/QC or multi-schema behavior.
- Always start with a lean schema. Do not clone a full production schema as a template.
- Use `init-domain-schema` with a reference schema as the novice entry point.
- Validate with `check-schema` before running any extraction.
- Run `extract` and `compile` on a 1–2 document smoke set before touching the full corpus.
- Do not scale to the full corpus until the smoke set produces acceptable structured output.
- Use `pixi` for all commands.
- Key schema fields to always set: `$metadata.extraction.main_data_array`, `$metadata.extraction.identifier_fields`, `$metadata.extraction.context_objects`, `$metadata.compilation.deduplication.key_fields`.

## Large Documents

For documents over 200 pages, configure page targeting in the config before scaling:

```yaml
extraction:
  pages:
    csv: config/<domain>/page_ranges.csv   # manual ranges win
    auto_locate:
      section_description: "the section containing <target data>"
      trigger_chars: 200000
      max_selected_pages: 30
```

## Expected Deliverables

- A validated schema at `schemas/personal/<domain>_schema.json`.
- A config at `config/<domain>/run.yaml` derived from `config/TEMPLATE.yaml`.
- Evidence from `check-schema`, smoke `extract`/`compile` (schema-only), and a full config run.
- A short iteration note describing what still needs refinement before scaling to the full corpus.
