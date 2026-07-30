---
agent: agent
description: "Bootstrap a brand-new document domain from raw source files: create schema, scaffold runtime, test extraction, and iterate."
---

Use this workflow when the user only has raw source documents and wants a complete current-state ParseSweep pipeline created.

Required steps:
1. Read `AGENTS.md`, `README.md`, and `schemas/SCHEMA_BEST_PRACTICES.md`.
2. Inspect 1-3 representative files under `documents/<domain>/`.
3. Identify the 4-8 highest-value fields the user needs in the first extraction pass.
4. Draft a lean schema under `schemas/personal/` with valid `$metadata.extraction`, top-level context objects, and a compact main data array. Use existing schemas only as references, not as something to copy wholesale.
5. Run `pixi run psweep check-schema <schema>` and fix schema issues.
6. Update or create `config/<domain>/run.yaml` so the workflow stays config-driven.
7. Validate the resolved command inputs with `pixi run psweep discover --config config/<domain>/run.yaml --validate-config`, then smoke-test `extract` and `compile`.
8. Extract 1-2 representative documents first, then compile the results.
9. Review the row shape and missing/error-prone fields, then expand the schema only where the first pass proved it is needed.
10. If needed, run QA/QC comparison and qualitative review.
11. Iterate on schema fields, page ranges, and wording until the extraction quality is strong.

Output format:
- Domain Summary: <document type, schema path, config path>
- Files Created or Updated: <list>
- Validation Commands: <commands + pass/fail>
- Extraction Findings: <what worked, what needs iteration>
- Next Iteration: <single concrete follow-up>