---
name: greenfield-domain-onboarding
description: "Use when a user only has raw documents for a new domain and needs the full current-state ParseSweep pipeline created, tested, and iterated."
---

# Greenfield Domain Onboarding

## Goal
Turn a raw `documents/<domain>/` folder into a working ParseSweep pipeline with a schema, runtime pack, config, validation evidence, and a first extraction/consolidation pass.

## When To Use
- The user says they are starting from scratch.
- The user only has documents and needs the rest of the pipeline created.
- A new domain such as solar, wind, mining, or another regulatory/document set needs onboarding.
- The user wants Copilot to drive a repeatable domain-bootstrap workflow.

## Required Workflow
1. Read `AGENTS.md`, `README.md`, and `schemas/SCHEMA_BEST_PRACTICES.md`.
2. Inspect a small representative sample of source documents.
3. Identify the 4-8 highest-value fields the user needs in the first pass.
4. Create a lean schema under `schemas/personal/` with valid `$metadata.extraction`, top-level context objects, and a compact main data array. Prefer `init-domain-schema` with a closest reference schema over copying a full production schema.
5. If the user already knows the highest-value first-pass fields, use `--include-field` on `init-domain-schema` to trim the starter immediately.
6. Use the closest existing schema as reference material, not as a full template to copy wholesale.
7. Keep ownership boundaries explicit: schema handles extraction contract and minimal dedup semantics; pack YAML handles runtime modules, QA/QC behavior, and deployment/runtime tuning.
8. Validate the schema with `pixi run psweep validate-schema <schema>`.
9. Scaffold the runtime surface with `pixi run psweep init-domain-pack --name <domain> --schema <schema> --with-workspace --with-config`.
10. Validate the runtime seam with `pixi run psweep validate-runtime --pack schemas/domain_packs/<domain>/pack.yaml --profile default`.
11. Run `process` on 1-2 documents first, not the full corpus.
12. Run `consolidate` on the extracted records and inspect the row shape.
13. Expand the schema only after the first pass shows what is missing or too ambiguous.
14. If the user needs stronger validation, run QA/QC compare and optional qualitative review.
15. Iterate on schema fields, page ranges, and extraction wording before scaling up.

## Guardrails
- Keep the contract-first runtime as the only active path.
- Prefer a lean first-pass schema over cloning a full production schema.
- Reuse existing production schemas as references for field names, descriptions, and domain patterns only when they actually help.
- Use `init-domain-schema` as the default novice entry point when the user only has documents plus a closest reference domain.
- Use `--include-field` when the user can already name the first-pass fields, rather than generating a larger starter and asking them to prune JSON manually.
- Do not scale to the full corpus until a small smoke set produces acceptable structured output.
- Use `pixi` for all commands.
- Update tracking docs if the work changes active modernization/release status.

## Expected Deliverables
- A validated schema under `schemas/personal/`.
- A validated pack under `schemas/domain_packs/<domain>/pack.yaml`.
- Starter config under `config/<domain>/`.
- Evidence from `validate-runtime`, `process`, and `consolidate`.
- A short iteration note describing what still needs refinement.