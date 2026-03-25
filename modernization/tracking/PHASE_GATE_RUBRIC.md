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
