# Modernization Implementation Checklist

## Phase 0: Baseline Freeze and Guardrails
- [x] Add modernization kickoff prompt command for new chat bootstrap
- [x] Freeze benchmark corpus manifest
- [x] Define objective quality thresholds
- [x] Add phase gate rubric
- [x] Add migration scaffolding sunset criteria

## Phase 1: Contract-First Foundation
- [x] Finalize extraction record contract (versioned)
- [x] Finalize modules catalog contract (versioned)
- [x] Implement profile/pack resolver + compiler
- [x] Implement artifact lineage emission
- [x] Add CI contract validation

## Phase 2: Pipeline Reconstruction
- [x] Rebuild extraction on new artifact path
- [x] Rebuild QA/QC on new artifact path
- [x] Rebuild consolidation on new artifact path
- [x] Remove legacy execution branches
- [x] Remove old translation/adapter code

## Phase 3: Enterprise Hardening
- [x] Implement deterministic run manifests
- [x] Implement error taxonomy and handling policy (structured process errors now include generic context-budget recovery guidance in the CLI, with repo page-range config hints when available)
- [x] Execute performance and scale benchmarks (manifest-backed reruns completed for AQ smoke, tariff smoke, and remediated large-tariff evidence on 2026-03-25; broader release decision remains pending under the Phase 3 release checklist)
- [x] Clean deprecated modules/docs/scripts (tracked `scripts/` surface is reduced to three validated retained utilities: `create_validation_comparison.py`, `migrate_field_names.py`, and `update_validation_spreadsheet.py`; active CLI/docs references were remediated to remove deleted QA/QC script and missing schema-path examples)
- [x] Finalize release checklist and runbook

## Phase 4: Controlled Expansion
- [ ] Evaluate deeper qualitative QA/QC (quantitative lane modularization landed; qualitative lane still disabled)
- [x] Add environment profile tiering if needed
- [ ] Improve domain onboarding automation (runtime validation, `init-domain-pack`, schema-derived starter `qaqc`/`consolidation` blocks, optional profile scaffolding, one-command profile tiering, workspace folder scaffolding, schema-aware config starter scaffolding, machine-readable guided next-step commands, schema-aware QA/QC/process guidance, guided interactive prompting for core onboarding choices, interactive custom-root prompting, interactive overwrite confirmation, lightweight template-mode selection, and optional sample document asset scaffolding landed; a fully interactive end-to-end onboarding flow remains open)

## Exit Criteria Tracking
- [ ] All mandatory quality gates pass
- [ ] No unresolved critical regressions
- [ ] Reliability and performance targets met
