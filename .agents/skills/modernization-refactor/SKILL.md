---
name: modernization-refactor
description: "Use when planning or executing StreamlineExtract modernization, clean-slate refactor phases, architecture hardening, migration gate reviews, checklist tracking, ADR updates, or when starting a new chat and needing full modernization context fast."
---

# Modernization Refactor Skill

## Goal
Execute and track the clean-slate modernization plan without losing context across chat sessions.

## Load Context First
Read these files before proposing work:
1. `AGENTS.md`
2. `modernization/plans/AUDIT_MASTER_PLAN_MODULAR_EXTRACTION_FOUNDATION.md`
3. `modernization/tracking/IMPLEMENTATION_CHECKLIST.md`
4. `modernization/tracking/MODERNIZATION_CHANGELOG.md`
5. Latest ADR(s) in `modernization/decisions/`

## Workflow
1. Identify current phase and gate status.
2. Choose one implementation slice that can be completed in one working session.
3. Define tests and validation criteria before code changes.
4. Implement smallest viable change.
5. Run validation using repo commands via `pixi`.
6. Update tracking docs:
   - checklist status
   - changelog entry (date, branch, scope)
   - ADR update if architecture changed
7. Report: completed work, test outcomes, next recommended slice.

## Guardrails
- Clean-slate by default: do not add long-term backward-compatibility layers.
- Remove legacy duplication when replacing functionality.
- No phase advancement without explicit gate pass evidence.
- Keep CLI changes minimal and high-value.

## Output Template
- Phase: <phase name>
- Slice: <what was implemented>
- Tests: <what ran + pass/fail>
- Risks/Blockers: <if any>
- Tracking Updates: <checklist/changelog/ADR>
- Next Slice: <single recommended next step>
