# Master Plan: Modular Extraction Foundation (Clean-Slate Enterprise Build)

## Executive summary
The current extraction system performs well, but the repository should evolve to an enterprise-grade architecture without carrying legacy constraints. This plan intentionally prioritizes clean design, strict contracts, and production hardening over backward compatibility.

Primary objective:
- redesign the system around a single modular architecture
- remove legacy pathways and duplicate logic
- enforce reliability, testability, and scalability as first-class requirements

## Strategic stance
This is a clean-slate modernization plan.

Explicit decisions:
- no backward-compatibility adapters
- no dual old/new execution paths beyond short-lived migration scaffolding
- no preservation of legacy schema shapes if they conflict with target architecture

What stays non-negotiable:
- extraction quality must remain at least baseline-equivalent during migration
- each phase must pass objective quality gates before advancing

## System-wide audit summary
1. What is working now
- extraction quality is strong enough to define a baseline benchmark
- schema metadata discipline already points toward contract-driven design
- QA/QC flow provides a practical foundation for quality measurement

2. What must change for enterprise scale
- coupled responsibilities across extraction, QA/QC, and consolidation
- implicit contracts and loose boundaries
- legacy/duplicate code paths that increase maintenance risk

3. Main risk if unchanged
- scaling to more domains increases complexity faster than delivery capacity
- reliability and auditability become harder to guarantee

## Core architecture principles
- One contract model: all runtime stages consume one canonical compiled artifact.
- One orchestration path: no long-term parallel legacy pipeline.
- Strict module boundaries: classifier, policy, projection, and consolidation units are isolated and testable.
- Deterministic lineage: every output is traceable to artifact id + profile id + model context.
- Production-first repo hygiene: remove dead code, old docs, and stale execution branches.

## Target architecture (V1 clean core)
Required artifacts:
- schemas/core/extraction_record.schema.json
- schemas/modules/modules_catalog.schema.json
- schemas/domain_packs/<pack_name>/pack.yaml
- schemas/profiles/default.profile.json
- schemas/registry/registry.yaml

Required runtime behavior:
1. Domain pack + profile compile into a versioned runtime artifact.
2. Extraction, QA/QC, and consolidation consume the same artifact id.
3. Artifact lineage is emitted in every run output and report.
4. CLI remains minimal, but commands map only to the new architecture.

## Non-goals
- preserving old schema formats
- maintaining compatibility wrappers
- keeping deprecated CLI semantics that conflict with the clean model
- expanding feature surface before hardening foundation quality

## Phased development plan (quality-gated)

### Phase 0: Baseline freeze and migration guardrails
Goal:
- establish objective measurement before major structural change

Deliverables:
- frozen benchmark corpus manifest (documents + expected outputs)
- baseline quality report (extraction, QA/QC, consolidation)
- phase gate rubric with explicit pass/fail thresholds
- migration feature flag strategy with hard sunset date

Gate to proceed:
- benchmark reruns are reproducible
- quality thresholds are approved and documented

### Phase 1: Contract-first core foundation
Goal:
- implement the canonical contract and artifact compiler as the new source of truth

Deliverables:
- extraction record contract finalized (versioned)
- module catalog contract finalized (versioned)
- domain-pack/profile resolver and compiler
- artifact registry and lineage emission
- contract validation tooling in CI

Gate to proceed:
- contract and compiler are stable across benchmark corpus
- artifact resolution consistency is 100%

### Phase 2: Pipeline reconstruction on new modules
Goal:
- rebuild extraction, QA/QC, and consolidation to run natively on the new artifact

Deliverables:
- modular components:
  - value semantics classifier
  - eligibility policy engine
  - lane projector
  - consolidation mapper
- legacy execution branches removed
- old metadata translation code removed

Gate to proceed:
- benchmark quality is at or above baseline thresholds
- no critical regressions in core output contracts
- dead/duplicate code inventory reduced per cleanup target

### Phase 3: Reliability hardening and enterprise readiness
Goal:
- productionize for scale, observability, and operational confidence

Deliverables:
- deterministic run manifests with full lineage
- robust error taxonomy and failure handling
- performance profiles and scalability benchmarks
- repository cleanup (archive stale docs/scripts, remove deprecated modules)
- release checklist and operational runbook

Gate to proceed:
- reliability SLOs met on benchmark and stress corpora
- performance targets met for representative large documents
- release checklist fully green

### Phase 4: Controlled capability expansion
Goal:
- add advanced features only on top of hardened core

Deliverables (demand-driven):
- deeper qualitative QA/QC stages
- profile tiering for environments (dev/staging/prod)
- domain onboarding automation improvements

Gate to proceed:
- each addition reduces onboarding effort measurably
- no breach of complexity budget

## Testing and validation framework

### Test layers
- Unit tests:
  - contract validators
  - module behavior (classifier/policy/projector/consolidation mapper)
- Integration tests:
  - end-to-end process and consolidate workflows through new pipeline
- Golden tests:
  - benchmark corpus output comparison against approved baselines
- Stress tests:
  - large-document and high-volume processing performance checks

### Required phase metrics
- extraction parity score vs baseline corpus
- QA/QC agreement signal quality
- consolidation correctness (dedup and row integrity)
- runtime stability (failure rate)
- throughput and processing cost trends

### Gate policy
- no phase advancement without all mandatory gates passing
- any failed gate triggers remediation sprint before next phase

## Complexity budget and cleanup rules
- every new abstraction must remove or simplify at least one existing complexity hotspot
- any temporary migration scaffolding must have explicit removal criteria and deadline
- track repository cleanliness metrics:
  - number of deprecated modules
  - number of duplicate logic paths
  - time to onboard a new domain pack
  - files touched per new domain implementation

## Dependencies across existing plans
Execution order:
1. Plan 3 modular foundation
2. Plan 1 lane semantics and QA/QC logic
3. Plan 2 prompt layering and governance

Constraint:
- Plans 1 and 2 are implemented only through the new contract-driven architecture.

## Success criteria
Technical:
- benchmark extraction quality meets or exceeds baseline thresholds
- single contract-driven pipeline for extraction, QA/QC, and consolidation
- full lineage traceability for every output

Operational:
- repository is cleaned of deprecated execution pathways
- onboarding a new domain requires fewer steps and fewer files
- system is release-ready with documented runbook and SLO-aligned reliability

## First 2-week execution plan (clean-slate kickoff)
Week 1:
- freeze benchmark corpus and define phase gate rubric
- finalize canonical contract drafts and artifact compiler spec
- add CI validation for contracts and benchmark execution

Week 2:
- implement compiler/resolver and lineage emission skeleton
- stand up modular pipeline skeleton (without legacy adapters)
- run first benchmark comparison and record gap report

Exit criteria after 2 weeks:
- clean-slate architecture direction is locked
- objective quality gates are active in CI
- clear gap list and implementation plan for Phase 2 reconstruction
