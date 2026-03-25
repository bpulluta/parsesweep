# Migration Scaffolding Sunset Criteria

## Status
Active

## Date
2026-03-24

## Purpose
Define strict criteria, owners, and deadlines for temporary migration scaffolding so no legacy bridge remains past required modernization gates.

## Scope
This policy applies to all temporary compatibility helpers introduced during modernization, including:
- feature flags for old/new path switching
- adapters or translators between legacy and canonical contracts
- temporary dual-write or dual-read utilities
- temporary fallback logic for legacy execution branches

## Non-Negotiable Rules
1. Every scaffolding item must have an explicit owner, removal trigger, and hard deadline.
2. No scaffolding item may survive past the gate specified in this policy.
3. Any missed deadline blocks phase advancement until either:
   - the scaffolding is removed, or
   - an ADR is approved with a revised hard deadline and risk mitigation.
4. New scaffolding is allowed only when no lower-risk alternative exists and removal plan is defined at creation time.

## Sunset Triggers and Deadlines

### Phase 0 to Phase 1
Scaffolding category: baseline-only instrumentation or migration flags used for baseline freeze.
Removal trigger: Phase 1 contract validation is green and artifact resolution consistency reaches 100%.
Hard deadline: before closing Phase 1 gate.

### Phase 1 to Phase 2
Scaffolding category: contract translation/adapters and temporary schema bridges.
Removal trigger: extraction, QA/QC, and consolidation consume canonical artifact without translation.
Hard deadline: before closing Phase 2 gate.

### Phase 2 to Phase 3
Scaffolding category: temporary legacy execution path toggles and old/new branch selectors.
Removal trigger: all benchmark runs execute only via modular pipeline path.
Hard deadline: before closing Phase 3 gate.

### Phase 3 to Phase 4
Scaffolding category: hardening-time temporary observability shims and transitional failover hooks.
Removal trigger: enterprise SLO and reliability gates are met with permanent mechanisms.
Hard deadline: before opening Phase 4 feature work.

## Required Scaffolding Record Fields
Each temporary scaffold must be logged with the following fields:
- id
- description
- owner
- introduced_in_branch
- introduced_date
- phase_introduced
- phase_sunset_required
- removal_trigger
- hard_deadline
- validation_for_removal
- status (`active`, `scheduled`, `removed`)
- removed_date

## Tracking and Evidence
1. Record all active scaffolding items in this file under the registry section.
2. Update status changes in `modernization/tracking/MODERNIZATION_CHANGELOG.md`.
3. Removal verification must include evidence paths (tests, benchmark runs, or code references).

## Scaffolding Registry

### Current items
- None.

## Gate Check Procedure
1. Before phase gate review, list all scaffolding records with status `active`.
2. Verify each active item has not exceeded its hard deadline.
3. Verify removal triggers are satisfied for items due this phase.
4. If any due item remains active, mark gate as FAIL.

## Exception Path
Any exception requires an ADR in `modernization/decisions/` that includes:
- reason removal is unsafe now
- blast radius/risk assessment
- revised hard deadline
- rollback and mitigation plan
