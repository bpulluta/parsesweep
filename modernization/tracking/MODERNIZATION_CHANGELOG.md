# Modernization Changelog

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed Phase 3 deterministic run manifest slice:
	- `src/streamline_extract/cli/commands.py` now builds and writes deterministic run manifests for single-model process runs
	- Added manifest helpers for stable manifest payload generation and persistence under `run_manifests/`
	- Process flow now records per-file output record paths and emits run-level status/timing metadata (`started_at`, `finished_at`, success/failure counts)
- Added/updated tests:
	- `tests/test_run_manifest.py` validates deterministic ordering/content and manifest file write path
- Validation gate run (PASS):
	- `pixi run pytest tests/test_run_manifest.py tests/test_process_artifact_lineage.py tests/test_artifact_compiler.py tests/test_core_contract_schema.py` (20 passed)

## 2026-03-24
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed Phase 2 translation/adapter cleanup slice:
	- `src/streamline_extract/utils/schema_metadata.py` no longer falls back from QA/QC record matching to consolidation dedup keys; canonical `qa_qc.record_matching.key_fields` is now required
	- `src/streamline_extract/qa_qc/comparison_engine.py` completeness calculation no longer carries legacy multi-field tuple translation branch and now uses canonical requirement-type matching key
- Added/updated tests:
	- `tests/test_schema_metadata.py` now asserts explicit QA/QC match-field configuration is required
- Validation gate run (PASS):
	- `pixi run pytest tests/test_schema_metadata.py tests/test_comparison_engine.py tests/test_multi_model_extractor.py tests/test_consolidation_lineage.py tests/test_process_artifact_lineage.py tests/test_artifact_compiler.py tests/test_core_contract_schema.py` (91 passed)

## 2026-03-24
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed Phase 2 legacy execution branch removal slice:
	- `src/streamline_extract/consolidation/consolidator.py` now processes canonical extraction-record files only (`payload` + `lineage`) and skips non-record files (for example, QA/QC `metadata.json`)
	- Removed legacy consolidation branches for wrapper-style `data` payloads and `_qaqc_metadata` lineage shims
	- Removed legacy lineage-key normalization path tied to non-canonical key variants
	- `src/streamline_extract/cli/commands.py` `validate` command now enforces canonical extraction-record payload format and no longer falls back to legacy wrapper fields
- Added/updated tests:
	- `tests/test_consolidation_lineage.py` migrated to canonical-only fixtures and added regression for skipping non-record metadata files
- Validation gate run (PASS):
	- `pixi run pytest tests/test_consolidation_lineage.py tests/test_process_artifact_lineage.py tests/test_multi_model_extractor.py tests/test_comparison_engine.py tests/test_artifact_compiler.py tests/test_core_contract_schema.py` (59 passed)

## 2026-03-24
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed Phase 2 QA/QC rebuild slice on artifact path:
	- `src/streamline_extract/qa_qc/multi_model_extractor.py` now writes per-model outputs in canonical extraction-record contract format (`record_id`, `contract_version`, `document`, `lineage`, `payload`, `quality`, `processing_metrics`)
	- Canonical QA/QC lineage now includes contract-required fields (`artifact_id`, `profile_id`, `run_id`, `model`, `provider`, `schema_id`, `extracted_at`)
	- `src/streamline_extract/qa_qc/comparison_engine.py` now unwraps `payload` automatically when reading extraction-record outputs, preserving compatibility with existing comparison logic
	- `src/streamline_extract/consolidation/consolidator.py` context extraction for extraction-record inputs now reads schema context from payload while lineage remains sourced from wrapper metadata
- Added/updated tests:
	- `tests/test_multi_model_extractor.py` updated to assert canonical QA/QC extraction-record outputs
	- `tests/test_consolidation_lineage.py` strengthened to assert payload context extraction (`Jurisdiction`) for extraction-record files
- Validation gate run (PASS):
	- `pixi run pytest tests/test_multi_model_extractor.py tests/test_comparison_engine.py tests/test_process_artifact_lineage.py tests/test_consolidation_lineage.py tests/test_artifact_compiler.py tests/test_core_contract_schema.py` (58 passed)

## 2026-03-24
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed Phase 2 extraction rebuild slice on artifact path:
	- `src/streamline_extract/cli/commands.py` now writes single-model extraction outputs in canonical extraction-record contract format (`record_id`, `contract_version`, `document`, `lineage`, `payload`, `quality`, `processing_metrics`)
	- Lineage now includes contract-required fields (`artifact_id`, `profile_id`, `run_id`, `model`, `provider`, `schema_id`, `extracted_at`)
	- `_extract_and_save_result` now accepts `provider` and `schema_id` to emit deterministic, contract-aligned lineage
	- `validate` command now validates canonical payload shape (`payload`) while still accepting legacy `data` wrappers during transition
- Added/updated tests:
	- `tests/test_process_artifact_lineage.py` updated to assert extraction-record structure and lineage fields
- Validation gate run (PASS):
	- `pixi run pytest tests/test_process_artifact_lineage.py tests/test_consolidation_lineage.py tests/test_multi_model_extractor.py tests/test_artifact_compiler.py tests/test_core_contract_schema.py` (35 passed)

## 2026-03-24
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed Phase 2 consolidation rebuild slice on artifact path:
	- `src/streamline_extract/consolidation/consolidator.py` now supports canonical extraction-record input (`payload`) in addition to wrapper-style `data`
	- Consolidation now extracts context from the correct source object per format (payload wrappers vs extraction-record contract)
	- Lineage extraction now falls back to `lineage.artifact_id` when top-level `artifact_id` is absent
	- Added lineage key normalization to enforce stable consolidated columns (`Run Id`, `Artifact Id`) and avoid duplicate variant columns
- Added/updated tests:
	- `tests/test_consolidation_lineage.py` adds extraction-record payload lineage coverage
- Validation gate run (PASS):
	- `pixi run pytest tests/test_consolidation_lineage.py tests/test_process_artifact_lineage.py tests/test_multi_model_extractor.py tests/test_artifact_compiler.py tests/test_core_contract_schema.py` (35 passed)

## 2026-03-24
- Continued on branch: `phase1-artifact-compiler-slice`
- Extended deterministic lineage into consolidation outputs:
	- `src/streamline_extract/consolidation/consolidator.py` now preserves lineage context from extraction wrappers
	- Added lineage extraction helper to carry `Run Id` and `Artifact Id` into consolidated rows
	- Supports both standard extraction wrapper format (`run_id`/`lineage`) and QA/QC model format (`_qaqc_metadata`)
- Added consolidation lineage tests:
	- `tests/test_consolidation_lineage.py`
- Validation gate run (PASS):
	- `pixi run pytest tests/test_consolidation_lineage.py tests/test_process_artifact_lineage.py tests/test_multi_model_extractor.py tests/test_artifact_compiler.py tests/test_core_contract_schema.py` (34 passed)

## 2026-03-24
- Continued on branch: `phase1-artifact-compiler-slice`
- Added deterministic run lineage id generation in `src/streamline_extract/cli/commands.py` (`_generate_run_id`)
- Wired `run_id` through both extraction paths:
	- Single-model outputs now include top-level `run_id` and `lineage.run_id`
	- QA/QC multi-model outputs now include `run_id` in `_qaqc_metadata` and `metadata.json`
	- CLI now passes `run_id` into `_extract_and_save_result` and `run_multi_model_extraction`
- Added/updated tests:
	- `tests/test_process_artifact_lineage.py` (deterministic run_id + persistence assertions)
	- `tests/test_multi_model_extractor.py` (QA/QC run_id assertions)
- Validation gate run (PASS):
	- `pixi run pytest tests/test_process_artifact_lineage.py tests/test_multi_model_extractor.py tests/test_artifact_compiler.py tests/test_core_contract_schema.py` (32 passed)

## 2026-03-24
- Continued on branch: `phase1-artifact-compiler-slice`
- Extended Phase 2 lineage integration to QA/QC multi-model path:
	- `src/streamline_extract/qa_qc/multi_model_extractor.py` now accepts `runtime_artifact`
	- Per-model `_qaqc_metadata` now includes `artifact_id`, `lineage`, and `contract_versions`
	- `metadata.json` now includes `artifact_id`, `lineage`, and `contract_versions`
	- `src/streamline_extract/cli/commands.py` passes `runtime_artifact` into `_run_qa_qc_extraction` and `run_multi_model_extraction`
- Added QA/QC lineage tests:
	- `tests/test_multi_model_extractor.py` (artifact metadata assertions)
- Validation gate run (PASS):
	- `pixi run pytest tests/test_multi_model_extractor.py tests/test_process_artifact_lineage.py tests/test_artifact_compiler.py tests/test_core_contract_schema.py` (31 passed)

## 2026-03-24
- Continued on branch: `phase1-artifact-compiler-slice`
- Implemented first Phase 2 integration seam in extraction process flow:
	- Added runtime artifact resolution helper in `src/streamline_extract/cli/commands.py` (`_resolve_runtime_artifact`)
	- Wired `process` command to resolve artifact lineage once per run when domain packs/profiles are available
	- Extended extraction JSON output to include `artifact_id`, `lineage`, and `contract_versions`
- Added focused tests for Phase 2 seam:
	- `tests/test_process_artifact_lineage.py`
- Validation gate run (PASS):
	- `pixi run pytest tests/test_process_artifact_lineage.py tests/test_artifact_compiler.py tests/test_core_contract_schema.py` (17 passed)

## 2026-03-24
- Branch created: `phase1-artifact-compiler-slice`
- Implemented Phase 1 pack/profile resolver and runtime artifact compiler:
	- `src/streamline_extract/core/artifact_compiler.py`
	- `src/streamline_extract/core/__init__.py`
- Added targeted compiler tests:
	- `tests/test_artifact_compiler.py`
- Runtime artifact now emits deterministic `artifact_id` plus lineage fields (`artifact_id`, `profile_id`, `pack_name`, `pack_version`, `compiled_at`)
- Marked Phase 1 checklist items complete:
	- Implement profile/pack resolver + compiler
	- Implement artifact lineage emission
- Validation gate run (PASS):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_core_contract_schema.py` (14 passed)

## 2026-03-24
- Branch created: `modular-enterprise-clean-slate`
- Added modernization documentation hub at `modernization/`
- Moved master plan into `modernization/plans/`
- Added implementation checklist and changelog for ongoing tracking
- Added kickoff prompt command: `/modernization-kickoff`
- Updated `AGENTS.md` bootstrap to run kickoff prompt first when available

## 2026-03-24
- Branch created: `phase0-benchmark-manifest-freeze`
- Added benchmark corpus freeze manifest: `modernization/tracking/BENCHMARK_CORPUS_MANIFEST.md`
- Added deterministic reproducibility evidence for geothermal/tariff/AQ domain lists
- Ran smoke extraction gates with production schemas:
	- PASS: geothermal (`output/modernization_smoke/geothermal`)
	- PASS: tariffs (`output/modernization_smoke/tariffs`)
	- BLOCKED: AQ permits (no source documents in `documents/aq_permits`)
- Marked Phase 0 checklist item complete: Freeze benchmark corpus manifest

## 2026-03-24
- AQ permits corpus populated with representative file: `documents/aq_permits/52432_DC_Permit.pdf`
- Re-ran AQ reproducibility check: PASS (1 file)
- Re-ran AQ smoke extraction gate with production schema: PASS (1 file, 4 items)
- Updated benchmark corpus manifest to close AQ cross-domain coverage blocker

## 2026-03-24
- Added objective quality thresholds and phase gate policy document: `modernization/tracking/PHASE_GATE_RUBRIC.md`
- Defined explicit metric formulas for extraction parity, QA/QC signal quality, consolidation correctness, failure rate, throughput delta, and cost delta
- Added pass/fail threshold criteria for Phases 0 through 4
- Marked Phase 0 checklist items complete:
	- Define objective quality thresholds
	- Add phase gate rubric

## 2026-03-24
- Added migration scaffolding sunset policy: `modernization/tracking/MIGRATION_SCAFFOLDING_SUNSET_CRITERIA.md`
- Defined mandatory owner/trigger/deadline fields and gate-fail conditions for overdue scaffolding
- Added per-phase sunset triggers and hard deadlines (Phase 0->1, 1->2, 2->3, 3->4)
- Marked final Phase 0 checklist item complete: Add migration scaffolding sunset criteria

## 2026-03-24
- Finalized versioned extraction record contract: `schemas/core/extraction_record.schema.json`
- Added contract validation tests with schema and sample-instance checks: `tests/test_core_contract_schema.py`
- Marked Phase 1 checklist items complete:
	- Finalize extraction record contract (versioned)
	- Add CI contract validation

## 2026-03-24
- Finalized versioned modules catalog contract: `schemas/core/modules_catalog.schema.json`
- Expanded core contract validation tests to validate both extraction record and modules catalog schemas
- Marked Phase 1 checklist item complete:
	- Finalize modules catalog contract (versioned)
