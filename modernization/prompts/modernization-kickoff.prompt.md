---
mode: agent
description: "Kickoff modernization: summarize current phase, open checklist items, and next implementation slice."
---

Load and summarize modernization context for this repository.

Steps:
1. Read the required context files:
   - `AGENTS.md`
   - `modernization/plans/AUDIT_MASTER_PLAN_MODULAR_EXTRACTION_FOUNDATION.md`
   - `modernization/tracking/IMPLEMENTATION_CHECKLIST.md`
   - `modernization/tracking/MODERNIZATION_CHANGELOG.md`
   - latest ADR file(s) under `modernization/decisions/`
2. Identify the active modernization phase based on checklist/changelog evidence.
3. List open checklist items for the active phase.
4. Propose one smallest viable implementation slice that can be completed in one session.
5. Define validation gates for that slice (tests, expected result, pass criteria).

Output format:
- Phase: <name>
- Status Summary: <2-4 bullets>
- Open Checklist Items: <bulleted list>
- Next Implementation Slice: <one concrete slice>
- Validation Plan: <commands/tests + pass criteria>
- Risks/Dependencies: <if any>
