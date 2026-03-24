# Modernization Documentation Hub

This folder is the source of truth for the clean-slate modernization of StreamlineExtract.

## Purpose
- Track architecture decisions, implementation phases, and quality gates.
- Keep modernization docs separate from archive content.
- Maintain a current view of what is planned, in progress, and complete.

## Structure
- `plans/`: Master plans and phase roadmaps.
- `tracking/`: Execution logs, checklists, and status updates.
- `decisions/`: Architecture decision records (ADRs).

## Primary Documents
- Master Plan: `plans/AUDIT_MASTER_PLAN_MODULAR_EXTRACTION_FOUNDATION.md`
- Checklist: `tracking/IMPLEMENTATION_CHECKLIST.md`
- Changelog: `tracking/MODERNIZATION_CHANGELOG.md`

## Workflow
1. Update checklist status before and after implementation work.
2. Record major design choices in `decisions/` using ADR files.
3. Append milestone updates to changelog with date and branch.
4. Keep docs focused and current; archive stale notes outside this folder.

## Branching Rule
All modernization implementation should be done in feature branches, then merged only after quality gates pass.
