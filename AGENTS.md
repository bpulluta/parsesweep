# StreamlineExtract Agent Operating Instructions

## Purpose
These instructions keep modernization work consistent across new chat sessions.

## Default Operating Mode
- Treat this repository as a clean-slate modernization effort for enterprise-scale extraction.
- Do not prioritize backward compatibility unless explicitly requested by the user.
- Prefer code and architecture cleanup over preserving legacy pathways.

## Session Bootstrap (run at start of modernization work)
1. Read these files first:
   - `modernization/plans/AUDIT_MASTER_PLAN_MODULAR_EXTRACTION_FOUNDATION.md`
   - `modernization/tracking/IMPLEMENTATION_CHECKLIST.md`
   - `modernization/tracking/MODERNIZATION_CHANGELOG.md`
   - `modernization/decisions/ADR-0001-clean-slate-modernization.md`
2. Summarize current phase, open checklist items, and latest changelog entries.
3. Propose the next smallest implementation slice with test gates.

## Working Rules
- Always work in a feature branch for modernization tasks.
- Keep changes phase-gated and test-first.
- Record major decisions as ADRs in `modernization/decisions/`.
- Update checklist and changelog in the same change set as implementation work.
- Use `pixi` for repo commands.

## Completion Criteria for Any Modernization Task
- Code change is implemented or docs/task state is updated.
- Relevant tests are run (or blocker is clearly documented).
- `modernization/tracking/IMPLEMENTATION_CHECKLIST.md` status is updated.
- `modernization/tracking/MODERNIZATION_CHANGELOG.md` has a dated entry.
