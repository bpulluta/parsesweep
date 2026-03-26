# Modernization Phase Gate Rubric and Objective Quality Thresholds

## Status
Active

## Date
2026-03-24

## Scope
This document defines the objective quality thresholds and phase gate policy for clean-slate modernization. It is the single scoring rubric used to determine if a phase can advance.

Benchmark corpus reference: `modernization/tracking/BENCHMARK_CORPUS_MANIFEST.md`

## Gate Policy
1. A phase may advance only when all required metrics for that phase meet or exceed thresholds.
2. Any threshold miss triggers remediation work before phase advancement.
3. Any contract-breaking regression is an automatic gate fail.
4. All gate outcomes must be recorded in `modernization/tracking/MODERNIZATION_CHANGELOG.md`.

## Metric Definitions

### 1) Extraction parity score
Definition: Percent of expected extraction items that are correctly reproduced on the benchmark corpus.

Formula:
`extraction_parity = (correct_items / expected_items) * 100`

### 2) QA/QC agreement quality
Definition: Percent of benchmark items where multi-model QA/QC reaches expected agreement/disagreement classification.

Formula:
`qaqc_signal_quality = (correct_qaqc_classifications / total_qaqc_items) * 100`

### 3) Consolidation correctness
Definition: Percent of consolidated rows that are both valid and correctly deduplicated.

Formula:
`consolidation_correctness = (correct_rows / total_rows_checked) * 100`

### 4) Runtime stability
Definition: Failure rate across benchmark processing runs.

Formula:
`failure_rate = (failed_runs / total_runs) * 100`

### 5) Throughput trend
Definition: Relative change in median processing time per document versus baseline.

Formula:
`throughput_delta_percent = ((current_median_sec_per_doc - baseline_median_sec_per_doc) / baseline_median_sec_per_doc) * 100`

Lower is better.

### 6) Cost trend
Definition: Relative change in median processing cost per document versus baseline.

Formula:
`cost_delta_percent = ((current_median_cost_per_doc - baseline_median_cost_per_doc) / baseline_median_cost_per_doc) * 100`

Lower is better.

## Objective Thresholds by Phase

### Phase 0 (Baseline Freeze and Guardrails)
Required metrics:
- Benchmark corpus manifest exists and is reproducible for all declared domains.
- Smoke extraction pass rate = 100% for all domains present in benchmark corpus.
- Contract-breaking errors = 0.

Gate result: PASS only when all three conditions are met.

### Phase 1 (Contract-First Foundation)
Required metrics:
- `extraction_parity >= 95.0`
- Artifact resolution consistency = 100%
- Contract validation pass rate = 100%
- `failure_rate <= 2.0`

Gate result: PASS only when all four conditions are met.

### Phase 2 (Pipeline Reconstruction)
Required metrics:
- `extraction_parity >= 97.0`
- `qaqc_signal_quality >= 95.0`
- `consolidation_correctness >= 99.0`
- `failure_rate <= 1.0`
- Contract-breaking errors = 0

Gate result: PASS only when all five conditions are met.

### Phase 3 (Enterprise Hardening)
Required metrics:
- `extraction_parity >= 97.0`
- `qaqc_signal_quality >= 96.0`
- `consolidation_correctness >= 99.5`
- `failure_rate <= 0.5`
- `throughput_delta_percent <= +15.0`
- `cost_delta_percent <= +10.0`

Gate result: PASS only when all six conditions are met.

### Phase 4 (Controlled Expansion)
Required metrics:
- All Phase 3 thresholds remain satisfied.
- New capability onboarding effort improves by at least 20% versus pre-modernization baseline.
- No increase in critical defect rate after feature release.

Gate result: PASS only when all three conditions are met.

## Measurement Procedure
1. Run benchmark extraction with frozen corpus and production schema per domain.
2. Run QA/QC comparison where applicable.
3. Run consolidation and validate dedup and row integrity.
4. Compute metrics using formulas above.
5. Record gate PASS/FAIL and evidence links in changelog.

## Gate Record Template
Use this section format for each gate run.

- Date:
- Branch:
- Phase:
- Extraction parity:
- QA/QC signal quality:
- Consolidation correctness:
- Failure rate:
- Throughput delta (%):
- Cost delta (%):
- Gate decision: PASS or FAIL
- Evidence paths:
- Follow-up actions:

## Gate Records

### 2026-03-25 Phase 3 Gate Record
- Date: 2026-03-25
- Branch: `phase1-artifact-compiler-slice`
- Phase: Phase 3, Enterprise Hardening
- Extraction parity: `100.0%`
	Evidence scope: AQ smoke extraction parity from `output/phase3_manifest_smoke/aq_permits` plus remediated tariff extraction parity from `output/phase3_manifest_range_remediated/tariffs`
- QA/QC signal quality: `100.0%`
	Evidence scope: four geothermal QA/QC fixture reports under `processed/qa_qc_test/qa_qc`
- Consolidation correctness: `100.0%`
	Evidence scope: AQ, geothermal, and tariff consolidated CSV outputs under `consolidated/aq_permits`, `consolidated/geothermal_ordinances`, and `consolidated/tariffs`
- Failure rate: `0.0%`
	Evidence scope: tracked benchmark runs against `output/phase3_manifest_smoke/aq_permits`, `output/phase3_manifest_smoke/tariffs`, and `output/phase3_manifest_range_remediated/tariffs`
- Throughput delta (%): `+0.0%`
- Cost delta (%): `+0.0%`
- Gate decision: PASS on the current tracked seeded evidence set
- Evidence paths:
	- `modernization/tracking/phase3_aq_permits_smoke_baseline_snapshot.json`
	- `modernization/tracking/phase3_tariffs_smoke_baseline_snapshot.json`
	- `modernization/tracking/phase3_tariffs_range_remediated_baseline_snapshot.json`
	- `modernization/tracking/expected/phase3_extraction/aq_permits/`
	- `modernization/tracking/expected/phase3_extraction/tariffs/`
	- `modernization/tracking/expected/phase3_qaqc/qa_qc_test/`
	- `modernization/tracking/expected/phase3_consolidated/aq_permits/`
	- `modernization/tracking/expected/phase3_consolidated/geothermal_ordinances/`
	- `modernization/tracking/expected/phase3_consolidated/tariffs/`
	- `output/phase3_manifest_smoke/aq_permits/run_manifests/c4e8a88522234ceb.manifest.json`
	- `output/phase3_manifest_smoke/tariffs/run_manifests/b3a75b5e4a786518.manifest.json`
	- `output/phase3_manifest_range_remediated/tariffs/run_manifests/cae816ea3fc8e003.manifest.json`
- Follow-up actions:
	- maintainer must accept the seeded expected artifacts as temporary release goldens or replace them with reviewed benchmark truth
	- maintainer must decide whether broader corpus coverage is required before release signoff
	- final release decision must still be recorded in `modernization/tracking/MODERNIZATION_CHANGELOG.md`
