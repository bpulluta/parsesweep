# Modernization Changelog

## 2026-03-26
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next user-facing reliability slice by turning context-budget failures into actionable CLI guidance:
	- Classified provider errors that surface as literal `context_window_exceeded` strings under the structured `context_budget` taxonomy so downstream CLI handling can recognize them reliably.
	- Added generic process-time recovery suggestions that point users toward bounded `--pages START-END` reruns, optionally surfacing a matching repo `config/<document-set>/page_ranges.csv` path when one exists for the failing document set.
	- Added focused regression coverage for both the real context-window error string and the generic suggestion helper paths, then confirmed the live whole-document tariff failure prints the actionable recovery steps instead of only the raw provider error.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_error_taxonomy.py tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
	- PASS: `pixi run streamline-extract process documents/tariffs/PSCo_Electric_Entire_Tariff.pdf --schema schemas/personal/electricity_tariff_schema.json --output /tmp/streamline_tariff_guardrail_check --max-context 1400000`
- Scope note:
	- This slice improves the default UX for large documents that exceed the current model context window.
	- It does not make whole-document extraction succeed automatically; large inputs still need bounded page ranges on the current model.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed a runtime-validation checkpoint and geothermal pack remediation after running live CLI smoke tests:
	- Ran a real extraction + consolidation smoke path on `documents/examples/sample_utility_rate.txt` with `schemas/example_utility_rate_schema.json`; the live process and consolidate workflow completed successfully against Azure OpenAI.
	- Discovered that the geothermal runtime seam failed readiness validation because the validator derived context-object column names differently from the real consolidator and the repo geothermal pack still carried stale/contradictory output config references.
	- Fixed runtime validation to reuse the consolidator naming logic for context-object columns, cleaned the geothermal pack output config to match the current schema row shape, and added focused CLI coverage proving the repo geothermal pack now validates successfully.
	- Extended the checkpoint across the other production domains: fixed stale AQ QA/QC field references in the repo pack, validated `aq_permits` and `tariffs` runtime seams successfully, and confirmed live AQ extraction/consolidation works on `documents/aq_permits/52432_DC_Permit.pdf`.
	- Confirmed tariff whole-document processing still exceeds the effective context budget on the current model for both repo tariff PDFs, but a sliced run using the repo page-range strategy (`PSCo_Electric_Entire_Tariff.pdf`, pages `49-143`) succeeded and consolidated correctly.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
	- PASS: `pixi run streamline-extract validate-runtime --schema schemas/personal/geothermal_ordinance_schema.json --profile default --report-format json`
	- PASS: `pixi run streamline-extract validate-runtime --schema schemas/personal/air_quality_permits_schema.json --profile default --report-format json`
	- PASS: `pixi run streamline-extract validate-runtime --schema schemas/personal/electricity_tariff_schema.json --profile default --report-format json`
	- PASS: `pixi run streamline-extract process documents/examples/ --schema schemas/example_utility_rate_schema.json --output /tmp/streamline_smoke_processed -n 1`
	- PASS: `pixi run streamline-extract consolidate /tmp/streamline_smoke_processed --schema schemas/example_utility_rate_schema.json --output /tmp/streamline_smoke_consolidated`
	- PASS: `pixi run streamline-extract process documents/aq_permits/52432_DC_Permit.pdf --schema schemas/personal/air_quality_permits_schema.json --output /tmp/streamline_aq_smoke_processed`
	- PASS: `pixi run streamline-extract consolidate /tmp/streamline_aq_smoke_processed --schema schemas/personal/air_quality_permits_schema.json --output /tmp/streamline_aq_smoke_consolidated`
	- PASS: `pixi run streamline-extract process documents/tariffs/PSCo_Electric_Entire_Tariff.pdf --schema schemas/personal/electricity_tariff_schema.json --output /tmp/streamline_tariff_smoke_sliced --pages 49-143 --max-context 600000`
	- PASS: `pixi run streamline-extract consolidate /tmp/streamline_tariff_smoke_sliced --schema schemas/personal/electricity_tariff_schema.json --output /tmp/streamline_tariff_smoke_sliced_consolidated`
- Scope note:
	- This checkpoint gave real evidence that the modernized runtime path is working across examples, AQ permits, and tariff slices, while also surfacing and removing concrete pack/validator drift in geothermal and AQ.
	- It is still a smoke check, not a full benchmark or accuracy certification across representative corpora, and tariff whole-document ergonomics still need stronger default guardrails.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-sample-assets slice by adding optional document-side placeholder scaffolding to `init-domain-pack`:
	- Added `--with-sample-assets` so onboarding can generate starter source-document assets under `documents/<domain>/`, including a README and `sample_manifest.csv` placeholder.
	- Wired scaffold reporting so machine-readable output now includes `sample_asset_paths`, and text-mode next steps call out reviewing the placeholder assets before the first run.
	- Added focused CLI coverage for successful sample-asset creation and deterministic failure when sample asset files already exist without `--force`.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice adds opt-in placeholder source-document assets to the onboarding surface.
	- It does not yet add interactive prompting for sample assets or deeper domain-template families.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-template-selection slice by adding lightweight template-mode selection to `init-domain-pack`:
	- Added `--template-mode {recommended,minimal}` so scaffolded onboarding docs and next-step output can be generated as either the current richer guidance set or a trimmed core-only workflow.
	- Interactive onboarding now prompts for template mode when it is not supplied explicitly.
	- Added focused CLI coverage for both explicit minimal mode and interactive minimal selection.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice lets onboarding choose between richer and trimmed guidance without changing the runtime scaffold itself.
	- It does not yet add sample asset scaffolding or deeper domain-template families.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-overwrite slice by adding interactive overwrite confirmation to `init-domain-pack`:
	- Interactive mode now detects pre-existing scaffold targets for pack, profile, and config files and prompts before overwriting them.
	- Preserved non-interactive behavior by keeping explicit `--force` semantics unchanged outside the guided prompt flow.
	- Added focused CLI coverage for both confirming and cancelling overwrite in interactive onboarding.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice removes another common failure mode from the interactive onboarding path.
	- It does not yet prompt for sample asset scaffolding or domain-template selection.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-root-prompt slice by extending interactive `init-domain-pack` prompting to cover scaffold roots:
	- Interactive mode now prompts for pack, profiles, and config roots when those flags are omitted, using repo-default paths as the prompt defaults.
	- The command result now reports the resolved scaffold roots so automated callers and tests can verify where assets were written.
	- Added focused CLI coverage for both custom-root prompting and accepting default root prompts under an isolated repo root.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice closes the remaining common custom-root gap in the interactive onboarding path.
	- It does not yet prompt for overwrite behavior, sample assets, or domain-template selection.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-interactive slice by adding guided prompt mode to `init-domain-pack`:
	- Added `--interactive` so users can scaffold a new domain pack by answering prompts for missing required values and core onboarding choices instead of composing the primary flags manually.
	- Preserved non-interactive behavior with explicit validation when `--name` or `--schema` are omitted outside interactive mode.
	- Added focused CLI coverage for interactive prompting, the new missing-argument validation path, and the intentional restriction that interactive prompting remains text-only instead of JSON-report mode.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice adds guided prompting for the core onboarding path on top of the existing scaffold command.
	- It does not yet provide a richer multi-step wizard for custom roots, sample assets, or domain-template selection.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-template-guidance slice by enriching scaffolded config and workflow guidance with schema-aware runtime recommendations:
	- Added document-type-aware process flag guidance so tariff onboarding now carries the proven `--max-context 1400000` recommendation in both scaffolded README content and generated next-step commands.
	- Added optional QA/QC workflow guidance to scaffolded README content and `next_steps` output whenever the generated pack includes a QA/QC lane.
	- Added focused CLI coverage for tariff and geothermal onboarding content so the richer guidance remains stable.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice enriches the generated onboarding template bundle using current schema and pack capabilities.
	- It does not yet add a fully interactive onboarding wizard or domain-specific sample asset generation.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-guidance slice by making `init-domain-pack` emit executable next-step workflow guidance:
	- Added machine-readable `next_steps` output covering runtime validation, process, and consolidate commands for the scaffolded domain.
	- Added text-mode next-step guidance so successful scaffolds end with a concrete repo-local workflow instead of only reporting created files.
	- Added focused CLI coverage proving guided output includes config-aware process commands when `--with-config` is used.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice guides users through the current end-to-end runtime flow after scaffolding.
	- It does not yet add an interactive onboarding wizard or richer domain-specific template bundles.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-config-content slice by making starter config files reflect the selected schema and document type:
	- Updated scaffolded `config/<domain>/README.md` content to include the resolved document type, domain name, schema path, and a suggested `pixi run streamline-extract process ... --pages-csv ...` workflow.
	- Updated scaffolded `page_ranges.csv` examples to use domain-appropriate sample filenames such as tariff, permit, or ordinance documents instead of a generic placeholder.
	- Added focused CLI coverage proving tariff and geothermal config scaffolds now emit domain-aware starter content.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice makes the starter config surface domain-aware based on schema metadata.
	- It does not yet generate richer domain-specific config files beyond the README and page-range template.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-config slice by letting `init-domain-pack` scaffold starter config files for a new domain:
	- Added `--with-config` so onboarding can create `config/<domain>/README.md` and `config/<domain>/page_ranges.csv` alongside the new pack.
	- Added `--config-root` plus machine-readable `config_paths` reporting so config scaffolding can be isolated in tests and automation.
	- Added focused CLI coverage for successful config-file creation and deterministic failure when a starter config file already exists without `--force`.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice scaffolds the minimal config surface needed for domain-specific page-range setup.
	- It does not yet create richer config templates, domain-specific defaults, or a single end-to-end interactive onboarding workflow.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-workspace slice by letting `init-domain-pack` scaffold the basic repo folder layout for a new domain:
	- Added `--with-workspace` so onboarding can create matching `documents/<domain>`, `processed/<domain>`, and `consolidated/<domain>` folders alongside the new pack.
	- Returned created workspace folder paths in the machine-readable command result and kept the scaffold idempotent on reruns.
	- Added focused CLI coverage for successful workspace-folder creation and repeated runs against pre-existing domain folders.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice scaffolds the minimal repo folder layout needed for a new domain workflow.
	- It does not yet create domain-specific config files, sample documents, or a fully guided end-to-end onboarding bundle.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-tiering slice by allowing `init-domain-pack` to scaffold the standard environment profile set:
	- Added `--profile-tiering` so onboarding can create `dev`, `staging`, and `prod` profiles alongside a new pack in one command.
	- Defaulted tiered validation to the generated `dev` profile and allowed `--profile {dev,staging,prod}` to choose which generated tier the runtime seam validates against.
	- Added focused CLI coverage for successful tiered profile creation and deterministic failure when `--with-profile` and `--profile-tiering` are requested together.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice scaffolds the standard profile tier set and validates against one generated tier.
	- It does not yet add richer per-domain runtime tuning or scaffold domain-specific environment defaults beyond the existing minimal templates.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-profile slice by allowing `init-domain-pack` to scaffold a profile alongside the pack:
	- Added optional `--with-profile` support so onboarding can write a minimal valid profile JSON and validate the created pack/profile pair immediately.
	- Added configurable `--profile-name` and `--profiles-root` options so the scaffold works for custom environments without modifying the compiler.
	- Added focused CLI coverage for successful paired pack/profile creation and deterministic failure when the target profile already exists without `--force`.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice scaffolds a minimal valid profile alongside a new pack.
	- It does not yet create profile tiers automatically per domain or add richer environment-specific runtime settings.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-template slice by making `init-domain-pack` scaffold schema-derived starter runtime sections:
	- Extended the pack scaffold command to copy starter consolidation settings from schema metadata, including deduplication keys/ignores and any explicitly configured output options.
	- Added nested-aware QA/QC scaffold generation so schemas like tariffs produce a starter projected quantitative lane instead of a flat invalid placeholder.
	- Added focused CLI coverage for both nested-array and flat-schema scaffold output so generated packs validate immediately across the current domain shapes.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice generates richer starter runtime sections from the selected schema.
	- It does not yet create domain-specific bespoke templates, schema files, or a full guided onboarding workflow.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-UX slice by adding a domain-pack scaffold command:
	- Added `streamline-extract init-domain-pack` to create a starter `pack.yaml` with the default module set for an existing schema.
	- Wired the scaffold command to immediately run runtime readiness validation on the created pack so onboarding produces a validated artifact seam in one step.
	- Added focused CLI coverage for successful scaffold creation and deterministic failure when the target pack already exists without `--force`.
- Validation gate run:
	- PASS: `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice adds a minimal pack scaffold for existing schemas.
	- It does not yet generate schema files, add rich domain-specific templates, or fully complete the onboarding automation checklist item.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-validation consolidation slice by validating pack-owned consolidation references against schema-derived row shape:
	- Extended runtime readiness validation to verify consolidation deduplication key fields and output settings such as `exclude_fields`, `column_renames`, and `column_order` against the consolidated columns implied by the schema.
	- Added validation for supported consolidation format, strategy, comparison mode, and output option types.
	- Added focused unit and CLI coverage for successful consolidation-path validation and deterministic failure on broken deduplication or export field references.
- Validation gate run (planned):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice validates the pack-owned consolidation settings currently consumed by the runtime.
	- It does not yet scaffold domain assets or provide a one-command onboarding workflow.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-validation QA/QC slice by validating pack lane references against schema structure:
	- Extended runtime readiness validation to verify pack-owned QA/QC lane `record_matching.key_fields`, `comparison.primary_fields`, and nested-array projection references against the resolved schema.
	- Added a dedicated readiness check showing QA/QC lane field references were validated for the compiled pack.
	- Added focused unit and CLI coverage for successful QA/QC validation and deterministic failure on broken lane fields and projection parent references.
- Validation gate run (planned):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice validates pack QA/QC lane field references used by the current compare path.
	- It does not yet scaffold domain assets or validate every non-QA/QC pack setting.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-validation path-check slice by validating schema metadata references against structure:
	- Added a small schema-path walker so runtime readiness validation now verifies `extraction.context_objects` resolve to object nodes and `extraction.identifier_fields` resolve to valid schema paths.
	- Added readiness checks for validated context-object and identifier-field paths in the machine-readable report.
	- Added focused unit and CLI coverage for successful path validation and deterministic failure on broken metadata references.
- Validation gate run (planned):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice validates runtime metadata references against schema structure.
	- It does not yet validate pack QA/QC lane field paths or scaffold new domain assets.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-validation schema-contract slice by validating runtime-required schema metadata:
	- Extended runtime readiness validation to load the resolved schema file through `SchemaMetadata` and fail when the schema lacks the required `$metadata` extraction contract.
	- Added readiness checks and report fields for `main_data_array`, `identifier_fields`, and `context_objects` so onboarding diagnostics expose the schema contract the runtime will use.
	- Added focused unit and CLI coverage for successful schema-contract reporting and deterministic failure on schemas missing required runtime metadata.
- Validation gate run (planned):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice validates the minimum schema metadata contract required by the runtime seam.
	- It does not yet scaffold new packs or validate every deeper schema semantic beyond the runtime-required metadata.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-validation hardening slice by making pack schema references concrete:
	- Extended runtime readiness validation so pack `schema_path` must resolve to an existing schema file instead of only being present as metadata.
	- Exposed the resolved schema file path in the readiness report and CLI output for easier onboarding debugging.
	- Added focused unit and CLI coverage for successful schema-file reporting and deterministic failure when a pack points at a missing schema file.
- Validation gate run (planned):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice hardens onboarding validation only.
	- It does not yet scaffold new packs or validate deeper schema semantics beyond file existence.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 onboarding-automation slice by adding runtime readiness validation for schema and pack authors:
	- Added a read-only runtime readiness helper that resolves a schema or pack through the canonical artifact compiler path and verifies required runtime sections before reporting onboarding status.
	- Added `streamline-extract validate-runtime` with text and JSON output so schema authors can validate pack/profile resolution, artifact identity, enabled modules, and profile runtime settings without running extraction.
	- Added focused unit and CLI coverage for successful schema-backed validation and actionable JSON failure output on incomplete pack definitions.
- Validation gate run (planned):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice adds read-only onboarding diagnostics only.
	- It does not yet scaffold new domain packs or provide a one-command domain creation workflow.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Extended suspicious dedup-preview severity ranking with schema-aware field hints:
	- Added schema-derived severity hints so conflicts on schema-declared numeric fields are ranked `high` even when the flattened column names are ambiguous.
	- Kept the existing name-based heuristics as a fallback when schema hints are unavailable.
	- Added focused coverage proving an ambiguously named numeric field is still flagged as `high` severity by the preview.
- Validation gate run (planned):
	- PASS: `pixi run pytest tests/test_consolidation_lineage.py tests/test_process_artifact_lineage.py` (31 passed)
- Scope note:
	- This slice improves preview severity ranking using the user-authored schema.
	- It does not yet derive severity from richer schema semantics like formats, units enums, or descriptions.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Extended the dedup dry-run workflow with threshold-based failure for automation and onboarding:
	- Added `--fail-on-suspicious {none,high,medium,low}` to `streamline-extract consolidate --dry-run` so suspicious duplicate groups can fail the command at a chosen severity threshold.
	- Added `fail_on_suspicious` and `would_fail_on_suspicious` to the machine-readable dry-run report payload.
	- Added focused helper and CLI coverage proving a `high`-severity suspicious merge returns a non-zero exit code while still emitting the JSON report.
- Validation gate run (planned):
	- PASS: `pixi run pytest tests/test_process_artifact_lineage.py` (23 passed)
- Scope note:
	- This slice adds failure thresholds on top of the dry-run preview path.
	- It does not yet add a separate `validate-dedup` command or repo-wide CI policy defaults.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Extended suspicious dedup-preview reporting with severity ranking for schema iteration:
	- Added `high` / `medium` / `low` severity classification for suspicious duplicate groups based on the conflicting column names.
	- Added severity counts to the dry-run text and JSON reports so schema authors can prioritize likely data-loss risks before consolidation.
	- Added focused test coverage proving numeric-value conflicts are ranked `high` severity in preview output.
- Validation gate run (planned):
	- PASS: `pixi run pytest tests/test_consolidation_lineage.py tests/test_process_artifact_lineage.py` (28 passed)
- Scope note:
	- This slice ranks suspicious preview findings only.
	- It does not yet fail the command or introduce CI-facing thresholds.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Extended the deduplication dry-run workflow with suspicious-merge detection for schema authors:
	- Added heuristic preview analysis that flags duplicate groups as suspicious when rows match on `key_fields` but disagree on other populated non-metadata columns.
	- Added suspicious-group counts and conflict details to the dry-run text and JSON reports so overly broad `key_fields` are visible before consolidation writes outputs.
	- Added focused unit and CLI coverage for suspicious duplicate-group reporting.
- Validation gate run (planned):
	- PASS: `pixi run pytest tests/test_consolidation_lineage.py tests/test_process_artifact_lineage.py` (28 passed)
- Scope note:
	- This slice adds warning heuristics on top of the existing preview workflow.
	- It does not change the actual deduplication behavior used during real consolidation.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Extended the deduplication dry-run slice with a scriptable report format for schema iteration workflows:
	- Added `streamline-extract consolidate --dry-run --report-format json` to emit a stable machine-readable preview payload with duplicate groups, key fields, compare columns, and row counts.
	- Kept the existing text preview as the default dry-run experience for interactive use.
	- Added focused helper and CLI coverage for the JSON dry-run report path.
- Validation gate run (planned):
	- PASS: `pixi run pytest tests/test_process_artifact_lineage.py` (21 passed after JSON-output cleanup)
- Scope note:
	- This slice makes deduplication preview automation-friendly for schema authors and future onboarding tooling.
	- It does not yet add suspicious-merge scoring or standalone dedup validation commands.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 deduplication-safety slice by adding a consolidation dry-run preview for schema authors:
	- Added a deduplication preview path that uses the real schema-driven key-field mapping and fuzzy duplicate grouping logic without mutating the consolidated rows.
	- Added `streamline-extract consolidate --dry-run` to preview duplicate groups, row counts before/after deduplication, active key fields, and compare columns without writing CSV/Excel outputs.
	- Added focused CLI coverage proving dry-run previews deduplication and leaves the output directory untouched.
- Validation gate run (planned):
	- PASS: `pixi run pytest tests/test_process_artifact_lineage.py` (19 passed)
- Scope note:
	- This slice adds a safe preview workflow for schema authors testing `key_fields`.
	- It does not yet add a standalone `validate-dedup` command or suspicious-merge heuristics beyond the current duplicate grouping logic.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 schema-authoring UX slice by improving documentation and runtime visibility for user-authored schemas:
	- Added explicit `schema` vs `pack` vs `profile` guidance plus a quick schema-iteration workflow to `README.md`.
	- Added authoring-workflow guidance and a `Do You Need a pack.yaml?` decision rule to `schemas/SCHEMA_BEST_PRACTICES.md`.
	- Added CLI runtime-artifact summaries to consolidate and compare output so users can see whether a pack/profile runtime artifact was resolved.
	- Added focused tests for runtime-artifact summary formatting.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice improves user comprehension and visibility for schema authoring.
	- It does not yet add a dedicated deduplication validator or a one-command schema iteration workflow.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 geothermal canonical-path slice by making the production geothermal schema path authoritative in the runtime pack and docs:
	- Changed the geothermal domain pack primary `schema_path` to `schemas/personal/geothermal_ordinance_schema.json` and retained the root and `v3` geothermal schema files as aliases.
	- Added compiler coverage asserting the geothermal pack now resolves to the personal production schema path.
	- Updated the remaining user-facing geothermal schema references in `README.md` and `schemas/SCHEMA_BEST_PRACTICES.md` to point at the canonical production path.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_artifact_compiler.py`
- Scope note:
	- This slice standardizes the canonical geothermal schema path without deleting the legacy root or `v3` schema files.
	- It does not yet remove those alias files from the repository or collapse the geothermal schema surface to a single file.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 geothermal production-schema cleanup slice by migrating the `schemas/personal` geothermal path onto the runtime pack seam:
	- Added `schemas/personal/geothermal_ordinance_schema.json` as a geothermal pack alias so runtime resolution works for the production schema path used in repo guidance.
	- Removed duplicated runtime-owned consolidation and `qa_qc` metadata from `schemas/personal/geothermal_ordinance_schema.json`.
	- Added regression coverage for runtime pack resolution and runtime QA/QC config resolution through the personal geothermal schema path.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice only cleans the geothermal production schema path and pack aliases.
	- It does not yet consolidate the remaining geothermal schema files into a single canonical tracked schema path.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 geothermal QA/QC cleanup slice by removing non-authoritative schema QA/QC blocks:
	- Removed duplicated `qa_qc` metadata from `schemas/geothermal_ordinance_schema.json` and `schemas/geothermal_ordinance_schema_v3.json`.
	- Added a regression test proving geothermal compare config still resolves from the runtime pack even when direct schema QA/QC access now fails.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice only removes geothermal schema QA/QC duplication where the pack-owned lane is already authoritative.
	- Generic schema QA/QC fallback remains in place for domains that have not yet moved to pack-owned config.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 consolidation-output slice by making runtime-pack `default_format` affect consolidate output selection:
	- Added a consolidate output-resolution helper that honors `default_format` only when it comes from runtime pack overrides, preserving existing schema-only behavior for unchanged domains.
	- Updated `streamline-extract consolidate` to emit CSV, Excel, or both based on the resolved runtime output format and to report the emitted paths accurately in normal and quiet output modes.
	- Added focused tests covering default dual-output behavior, runtime Excel-only behavior, and invalid-format fallback.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice intentionally does not make schema-owned `default_format` authoritative across all domains.
	- It establishes the clean runtime seam so pack-owned output selection can expand incrementally without broad regressions.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 geothermal schema-cleanup slice by simplifying the legacy geothermal alias schema:
	- Removed runtime-owned consolidation metadata (`strategy`, `comparison_mode`, and `output`) from `schemas/geothermal_ordinance_schema.json`.
	- Kept the schema-only extraction, validation, and deduplication key-field requirements intact while relying on the geothermal domain pack for runtime consolidation behavior.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_artifact_compiler.py`
	- `pixi run python -c "from streamline_extract.utils.schema_metadata import SchemaMetadata; SchemaMetadata('schemas/geothermal_ordinance_schema.json'); SchemaMetadata('schemas/geothermal_ordinance_schema_v3.json'); print('schema metadata ok')"`
- Scope note:
	- This slice removes duplicate runtime configuration from the legacy geothermal alias schema only.
	- It does not yet change consolidate output selection behavior for `default_format`.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 schema-simplification slice by moving geothermal consolidation export overrides into the domain pack:
	- Added pack-owned geothermal consolidation overrides for output exclusions plus nonessential export/dedup knobs in `schemas/domain_packs/geothermal_ordinances/pack.yaml`.
	- Added `SchemaMetadata` runtime override support and wired the consolidate CLI to apply pack-owned consolidation config when a runtime artifact resolves from the selected schema path.
	- Updated consolidation components to read exclude-field configuration through `SchemaMetadata` accessors instead of directly indexing raw schema metadata.
	- Removed the large geothermal v3 schema `consolidation.output` block plus unused `strategy` / `comparison_mode` fields from the schema file so authoring is less cluttered.
	- Added tests for metadata override merging and consolidation-time exclude-field overrides.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_schema_metadata.py tests/test_consolidation_lineage.py tests/test_artifact_compiler.py`
- Scope note:
	- This slice moves only the geothermal consolidation export override seam; it does not yet migrate all consolidation behavior or remove the same clutter from every legacy geothermal schema variant.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 geothermal export-runtime slice by wiring pack-owned output settings into saved exports:
	- Added schema metadata access for `column_renames` and applied output renaming and ordering before CSV/Excel saves.
	- Updated Excel export to honor runtime `freeze_columns` and `auto_width` settings so pack-owned output configuration affects the generated workbook.
	- Added focused tests covering runtime-owned CSV/Excel header shaping and Excel freeze-pane behavior.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_schema_metadata.py tests/test_consolidation_lineage.py`
- Scope note:
	- This slice wires runtime-owned export presentation settings through the current save path.
	- It does not yet change the CLI to emit only the schema default format or remove the same metadata clutter from every legacy schema variant.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 tariff QA/QC slice by adding a pack-owned nested-array projection for charge-level comparison:
	- Added explicit tariff pack-level QA/QC lanes with an active `quantitative` lane that projects `rate_schedules[].charges[]` into flat charge comparison rows while preserving parent schedule identifiers.
	- Updated `ComparisonEngine` and runtime QA/QC config resolution so pack-owned projection metadata is honored during compare execution.
	- Added focused tests covering tariff projection exposure from the compiled artifact, runtime config projection resolution, and end-to-end charge-level disagreement detection on nested tariff outputs.
- Validation gate run (planned):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_comparison_engine.py`
- Scope note:
	- This slice only changes QA/QC comparison behavior; it does not alter extraction output shape or consolidation behavior.
	- Tariff qualitative comparison remains intentionally disabled pending later text-review lane work.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 QA/QC migration slice by moving AQ permits onto pack-owned quantitative comparison config:
	- Added explicit AQ permits pack-level QA/QC lanes with an active `quantitative` lane and a disabled placeholder `qualitative` lane in `schemas/domain_packs/aq_permits/pack.yaml`.
	- Chose AQ record-matching fields that exclude the numeric comparison targets so missing or disagreeing capacity values do not prevent item matching.
	- Added repo-backed tests for AQ pack lane exposure and runtime QA/QC config resolution from a compiled artifact.
- Validation gate run (planned):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_comparison_engine.py`
- Scope note:
	- Tariffs were intentionally not migrated in this slice because the current QA/QC compare engine does not compare nested `charges` arrays, so pack-owned tariff config requires either a projected QA/QC shape or array-aware comparison logic in a separate slice.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 4 modular QA/QC slice by moving active comparison-lane selection into the runtime pack layer for geothermal:
	- Added explicit geothermal pack-level QA/QC lanes with an active `quantitative` lane and a disabled placeholder `qualitative` lane in `schemas/domain_packs/geothermal_ordinances/pack.yaml`.
	- Added schema-to-pack resolution in the runtime compiler so pack-backed config can be resolved from a schema path rather than only from a category name.
	- Updated `streamline-extract compare` and `ComparisonEngine` to use resolved runtime QA/QC config when a pack is available, while retaining schema-metadata fallback for domains that have not yet moved comparison config into packs.
	- Added tests for repo QA/QC lane resolution, schema-path-to-pack resolution, runtime-artifact compare config overrides, and compare summary lane metadata.
- Validation gate run (planned):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py tests/test_comparison_engine.py`
- Scope note:
	- This slice does not yet move QA/QC extraction prompts or all domain schemas into pack-owned QA/QC config.
	- The qualitative lane remains intentionally disabled; the goal here is to establish the modular seam for future qualitative comparison work.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the first concrete Phase 4 implementation slice by adding runtime profile tiering assets and wiring them into the process path:
	- Added tracked runtime profiles under `schemas/profiles/` for `default`, `dev`, `staging`, and `prod`.
	- Added tracked domain-pack definitions under `schemas/domain_packs/` for `aq_permits`, `tariffs`, and `geothermal_ordinances` so the existing artifact compiler resolves against real repository assets instead of falling back to unresolved runtime lineage.
	- Exposed `--profile` on `streamline-extract process`, using the selected profile when compiling runtime artifacts and surfacing the resolved profile in the process configuration summary.
	- Added repo-backed tests to confirm the real repository profiles and packs compile successfully and that runtime artifact resolution honors the selected profile.
	- Marked the Phase 4 environment profile tiering checklist item complete.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_artifact_compiler.py tests/test_process_artifact_lineage.py`
- Scope note:
	- This slice establishes real on-disk profile tiering and pack assets, but does not yet add profile-specific behavioral overrides beyond lineage and compiler resolution.
	- The next Phase 4 candidate slices remain deeper qualitative QA/QC or domain onboarding automation.

## 2026-03-25
- Pending maintainer Phase 3 release decision entry:
	- Phase: Phase 3, Enterprise Hardening
	- Branch: `phase1-artifact-compiler-slice`
	- Gate evidence reviewed by: `<maintainer>`
	- Decision: `PASS` | `CONDITIONAL PASS` | `HOLD`
	- Seed expected sets accepted for release evidence: `yes` | `no`
	- Broader corpus coverage required before release: `yes` | `no`
	- Non-blocking residual gaps accepted: `<list or none>`
	- Follow-up remediation items created: `<list or none>`
	- Notes: `<optional rationale>`
	- Completion instruction: replace this placeholder entry with the final decision values instead of adding a separate ad hoc summary so the release decision remains easy to audit.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 3 signoff-readiness slice by recording a formal gate result in the rubric:
	- Added a Phase 3 gate record to `modernization/tracking/PHASE_GATE_RUBRIC.md` using the current tracked seeded evidence set.
	- Recorded the current metric values for extraction parity, QA/QC signal quality, consolidation correctness, failure rate, throughput delta, and cost delta.
	- Marked the gate result as PASS on the seeded evidence set while keeping maintainer acceptance of seeded artifacts and any broader-coverage requirement as explicit follow-up actions.
	- Updated the Phase 3 release checklist wording so it reflects that the rubric-level scoring record now exists.
- Validation evidence reviewed:
	- `modernization/tracking/PHASE_GATE_RUBRIC.md`
	- `modernization/tracking/PHASE3_RELEASE_CHECKLIST.md`
	- tracked benchmark evidence already referenced in the rubric gate record
- Scope note:
	- This slice records the gate result; it does not itself constitute maintainer release approval.
	- Remaining Phase 3 work is now limited to maintainer evidence acceptance, any broader-coverage policy decision, explicit release decision recording, and follow-up remediation tracking if needed.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 3 benchmark-evidence slice by adding AQ consolidation coverage:
	- Fixed key-field mapping in the shared item matcher so schema fields like `referenceNumber` and `ratedCapacityKW` correctly map to consolidated column names such as `Reference Number` and `Rated Capacity K W`.
	- Regenerated the AQ consolidated artifact from `output/phase3_manifest_smoke/aq_permits` using the production AQ schema with the corrected deduplication-key mapping.
	- Added tracked AQ consolidated expected output under `modernization/tracking/expected/phase3_consolidated/aq_permits/aq-permits.csv`.
	- Ran the AQ consolidation correctness benchmark and confirmed `100.0%` correctness against the tracked seed output.
	- Updated the Phase 3 release checklist so AQ consolidation coverage is no longer a remaining gap.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_item_matcher.py tests/test_performance_benchmark.py`
	- `pixi run streamline-extract consolidate output/phase3_manifest_smoke/aq_permits --schema schemas/personal/air_quality_permits_schema.json --output consolidated/aq_permits`
	- `pixi run streamline-extract benchmark consolidated/aq_permits --consolidation-baseline-dir modernization/tracking/expected/phase3_consolidated/aq_permits --consolidation-schema schemas/personal/air_quality_permits_schema.json --min-consolidation-correctness 99.5`
- Scope note:
	- AQ consolidation coverage is now present in the tracked seeded evidence set.
	- Remaining Phase 3 release-signoff gaps are now maintainer acceptance of the seeded expected sets as goldens or replacement with reviewed truth, plus any decision to require broader corpus coverage.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 3 release-signoff handoff slice:
	- Converted the remaining release work in `PHASE3_RELEASE_CHECKLIST.md` into an explicit maintainer review packet instead of an open-ended blocker list.
	- Added a decision-oriented summary of what is already satisfied, what evidence remains signoff-owned, and which coverage gaps are still a policy choice rather than missing implementation.
	- Added a ready-to-record release decision template so the maintainer can record `PASS`, `CONDITIONAL PASS`, or `HOLD` directly in the changelog without reconstructing context from prior entries.
- Validation evidence reviewed:
	- `modernization/tracking/PHASE3_RELEASE_CHECKLIST.md`
	- `modernization/tracking/MODERNIZATION_CHANGELOG.md`
	- `modernization/tracking/PHASE_GATE_RUBRIC.md`
- Scope note:
	- This slice does not add new benchmark evidence or code-path changes.
	- It narrows the remaining Phase 3 work to an explicit release decision plus any maintainer-requested follow-up remediation items.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 3 governance/operator closure slice:
	- Verified branch freshness against `origin/main`; the branch is not behind the remote default branch at the time of review.
	- Verified the selected provider prerequisites in `.env` without exposing secrets: Azure OpenAI key, endpoint, and model are present.
	- Audited the current `src/` and `tests/` tree plus the branch diff footprint for reintroduced legacy execution paths; no surviving `decision_tree` / `dtree` references or new compatibility-layer execution branches were found.
	- Updated the Phase 3 release checklist to close the branch-freshness, environment, and legacy-branch governance items.
- Validation evidence reviewed:
	- `git fetch origin main --quiet && git rev-list --left-right --count origin/main...HEAD` → `0 3`
	- `git status --short --branch`
	- safe `.env` presence check for `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_MODEL`, and `OPENAI_API_KEY`
	- `git diff --name-only main...HEAD`
	- `git diff --stat main...HEAD -- src tests modernization schemas`
	- search over active `src/` and `tests/` content confirmed no surviving `decision_tree`, `decision_trees`, or `dtree` references
- Scope note:
	- This slice closes the remaining implementation-owned operator/governance checks.
	- Phase 3 release closure still depends on maintainer review of the seeded benchmark evidence, any required broader corpus or AQ consolidation coverage, explicit release decision recording, and follow-up remediation tracking for non-blocking residual gaps.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the next Phase 3 release-gating slice by re-running the targeted regression and quality-gate evidence with a small benchmark noise fix:
	- Added an optional missing-key warning suppressor to the shared key-field mapper and used it only for consolidation benchmarking so sparse-but-valid consolidated CSV comparisons do not emit false warning noise when a deduplication key field is absent from both compared outputs.
	- Re-ran the targeted contract/runtime regression suite and benchmark command tests.
	- Re-ran the tracked Phase 3 quality-gate benchmark commands and confirmed all current seeded evidence sets pass cleanly.
	- Updated the Phase 3 release checklist to mark the four objectively satisfied quality gates complete while leaving maintainer signoff, broader corpus coverage, AQ consolidation coverage, operator verification, and governance review open.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_core_contract_schema.py tests/test_artifact_compiler.py tests/test_run_manifest.py tests/test_error_taxonomy.py tests/test_extraction_failure_handling.py`
	- `pixi run pytest tests/test_item_matcher.py tests/test_performance_benchmark.py`
	- `pixi run streamline-extract benchmark output/phase3_manifest_smoke/aq_permits --extraction-baseline-dir modernization/tracking/expected/phase3_extraction/aq_permits --min-extraction-parity 97`
	- `pixi run streamline-extract benchmark output/phase3_manifest_range_remediated/tariffs --extraction-baseline-dir modernization/tracking/expected/phase3_extraction/tariffs --min-extraction-parity 97`
	- `pixi run streamline-extract benchmark processed/qa_qc_test/qa_qc --qaqc-baseline-dir modernization/tracking/expected/phase3_qaqc/qa_qc_test --min-qaqc-signal-quality 96`
	- `pixi run streamline-extract benchmark consolidated/geothermal_ordinances --consolidation-baseline-dir modernization/tracking/expected/phase3_consolidated/geothermal_ordinances --consolidation-schema schemas/personal/geothermal_ordinance_schema.json --min-consolidation-correctness 99.5`
	- `pixi run streamline-extract benchmark consolidated/tariffs --consolidation-baseline-dir modernization/tracking/expected/phase3_consolidated/tariffs --consolidation-schema schemas/personal/electricity_tariff_schema.json --min-consolidation-correctness 99.5`
- Scope note:
	- The quality gates now have explicit tracked pass evidence on the seeded benchmark sets, but Phase 3 release closure still depends on maintainer acceptance of those seeds as goldens or replacement with reviewed truth, broader corpus coverage, branch/env verification, and final governance signoff.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Populated tracked expected-artifact seeds and exercised the new quality metrics on real repository artifacts:
	- Added tracked seed expected sets under `modernization/tracking/expected/` for AQ extraction parity, remediated tariff extraction parity, four geothermal QA/QC comparison reports, and geothermal/tariff consolidated CSV outputs.
	- Patched the copied AQ and tariff extraction seed records with explicit `lineage.schema_id` paths required for parity scoring.
	- Fixed an extraction parity bug in benchmark scoring so canonical extraction-record `payload` data is scored correctly instead of the full wrapper object.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_performance_benchmark.py`
	- `pixi run streamline-extract benchmark output/phase3_manifest_smoke/aq_permits --extraction-baseline-dir modernization/tracking/expected/phase3_extraction/aq_permits --min-extraction-parity 97`
	- `pixi run streamline-extract benchmark output/phase3_manifest_range_remediated/tariffs --extraction-baseline-dir modernization/tracking/expected/phase3_extraction/tariffs --min-extraction-parity 97`
	- `pixi run streamline-extract benchmark processed/qa_qc_test/qa_qc --qaqc-baseline-dir modernization/tracking/expected/phase3_qaqc/qa_qc_test --min-qaqc-signal-quality 96`
	- `pixi run streamline-extract benchmark consolidated/geothermal_ordinances --consolidation-baseline-dir modernization/tracking/expected/phase3_consolidated/geothermal_ordinances --consolidation-schema schemas/personal/geothermal_ordinance_schema.json --min-consolidation-correctness 99.5`
	- `pixi run streamline-extract benchmark consolidated/tariffs --consolidation-baseline-dir modernization/tracking/expected/phase3_consolidated/tariffs --consolidation-schema schemas/personal/electricity_tariff_schema.json --min-consolidation-correctness 99.5`
- Scope note:
	- These expected sets are tracked reproducibility seeds derived from current repository outputs, not yet maintainer-reviewed golden references.
	- Phase 3 quality gates should remain open until the maintainer accepts these seeds or replaces them with reviewed benchmark truth and expands any remaining domain coverage gaps.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the final quality-metric implementation slice for the remaining Phase 3 blockers:
	- Extended `streamline-extract benchmark` to optionally score consolidation correctness against expected consolidated CSV outputs using shared comparable columns and schema deduplication keys.
	- Added a new consolidation correctness gate via `--consolidation-baseline-dir`, `--consolidation-schema`, and `--min-consolidation-correctness`, keeping all three rubric quality metrics on the same benchmark command surface.
	- Updated the Phase 3 operational runbook and blocker summary to document the new path while keeping release signoff open until real expected consolidated outputs are tracked.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_performance_benchmark.py`
- Scope note:
	- This slice implements consolidation correctness measurement infrastructure only; it does not yet provide tracked benchmark-corpus consolidation evidence for Phase 3 release signoff.
	- All remaining Phase 3 blockers are now evidence-population or maintainer-verification work rather than missing scoring infrastructure.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the second quality-metric implementation slice for the remaining Phase 3 blockers:
	- Extended `streamline-extract benchmark` to optionally score QA/QC signal quality against expected `comparison_report.csv` classifications using `Requirement` and `Status` columns.
	- Added a new QA/QC signal gate via `--qaqc-baseline-dir` and `--min-qaqc-signal-quality`, keeping rubric-aligned quality scoring on the same benchmark command surface as extraction parity and runtime gates.
	- Updated the Phase 3 operational runbook and blocker summary to document the new path while keeping the release gate open until real expected QA/QC reports are tracked.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_performance_benchmark.py`
- Scope note:
	- This slice implements QA/QC signal measurement infrastructure only; it does not yet provide tracked benchmark-corpus QA/QC signal evidence for Phase 3 release signoff.
	- The remaining implementation-grade slice is consolidation correctness scoring.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the first quality-metric implementation slice for the remaining Phase 3 blockers:
	- Extended `streamline-extract benchmark` to optionally score extraction parity against expected extraction JSON records using schema metadata deduplication keys.
	- Added a new extraction parity gate via `--extraction-baseline-dir` and `--min-extraction-parity`, keeping quality scoring on the same benchmark command surface as the existing runtime and cost gates.
	- Updated the Phase 3 operational runbook and blocker summary to document the new path while keeping the release gate open until real benchmark-corpus expected outputs are tracked.
- Validation gate run (PASS):
	- `pixi run pytest tests/test_performance_benchmark.py`
- Scope note:
	- This slice implements extraction parity measurement infrastructure only; it does not yet provide tracked benchmark-corpus parity evidence for Phase 3 release signoff.
	- The next implementation-grade slices are QA/QC signal scoring and consolidation correctness scoring.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed a Phase 3 blocker-review slice for the remaining release checklist items:
	- Confirmed the unchecked Phase 3 gates now split into operator-verification items, governance review items, and three rubric metrics that still lack tracked scoring artifacts.
	- Cross-checked the release checklist against `PHASE_GATE_RUBRIC.md`, `PHASE3_OPERATIONAL_RUNBOOK.md`, benchmark manifests/snapshots, and the closest regression/quality tests.
	- Added an explicit blocker summary plus recommended follow-up commands to `PHASE3_RELEASE_CHECKLIST.md` so the remaining release work is actionable without reopening already-closed implementation slices.
- Validation evidence reviewed:
	- `modernization/tracking/PHASE_GATE_RUBRIC.md`
	- `modernization/tracking/PHASE3_OPERATIONAL_RUNBOOK.md`
	- `tests/test_extraction_failure_handling.py`
	- Existing manifest and snapshot evidence under `output/phase3_manifest_smoke/` and `output/phase3_manifest_range_remediated/`
- Scope note:
	- No additional release gates were marked complete in this slice because the remaining items require either maintainer verification or new benchmark-corpus scoring evidence.
	- The next implementation-grade slice is to materialize tracked measurements for extraction parity, QA/QC signal quality, and consolidation correctness.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed a Phase 3 release-readiness checklist review slice:
	- Marked the release checklist items now directly supported by tracked evidence: production schema selection in run manifests, benchmark corpus input presence, runtime contract/compiler/manifest/error-taxonomy tests, benchmark execution on real manifests, zero-error / zero-failure benchmark reliability gates, and the completed tracked-surface cleanup review.
	- Left maintainer-owned or not-yet-evidenced items open, including branch freshness, `.env` verification, qualitative quality gates, no-new-legacy-branch verification, and final release decision recording.
- Validation evidence reviewed:
	- `output/phase3_manifest_smoke/aq_permits/run_manifests/c4e8a88522234ceb.manifest.json`
	- `output/phase3_manifest_smoke/tariffs/run_manifests/b3a75b5e4a786518.manifest.json`
	- `output/phase3_manifest_range_remediated/tariffs/run_manifests/cae816ea3fc8e003.manifest.json`
	- `modernization/tracking/phase3_aq_permits_smoke_baseline_snapshot.json`
	- `modernization/tracking/phase3_tariffs_smoke_baseline_snapshot.json`
	- `modernization/tracking/phase3_tariffs_range_remediated_baseline_snapshot.json`
	- `pixi run pytest tests/test_core_contract_schema.py tests/test_artifact_compiler.py tests/test_run_manifest.py tests/test_error_taxonomy.py`
- Scope note:
	- This is a release-checklist evidence pass, not a final release approval.
	- Remaining blockers are intentionally explicit in `modernization/tracking/PHASE3_RELEASE_CHECKLIST.md`.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed the Phase 3 benchmark/release-review evidence slice for tracked manifest outputs:
	- Re-ran `streamline-extract benchmark` against `output/phase3_manifest_smoke/aq_permits`, `output/phase3_manifest_smoke/tariffs`, and `output/phase3_manifest_range_remediated/tariffs` using their tracked baseline snapshots.
	- Verified all three benchmark runs completed with `0` failed documents, `0` structured errors, and `+0.00%` throughput / cost deltas versus the saved baselines.
	- Ran the targeted Phase 3 runtime gate tests: `tests/test_core_contract_schema.py`, `tests/test_artifact_compiler.py`, `tests/test_run_manifest.py`, and `tests/test_error_taxonomy.py`.
- Validation gate run (PASS):
	- `pixi run streamline-extract benchmark output/phase3_manifest_smoke/aq_permits --baseline-snapshot modernization/tracking/phase3_aq_permits_smoke_baseline_snapshot.json`
	- `pixi run streamline-extract benchmark output/phase3_manifest_smoke/tariffs --baseline-snapshot modernization/tracking/phase3_tariffs_smoke_baseline_snapshot.json`
	- `pixi run streamline-extract benchmark output/phase3_manifest_range_remediated/tariffs --baseline-snapshot modernization/tracking/phase3_tariffs_range_remediated_baseline_snapshot.json`
	- `pixi run pytest tests/test_core_contract_schema.py tests/test_artifact_compiler.py tests/test_run_manifest.py tests/test_error_taxonomy.py`
- Scope note:
	- This closes the implementation-side benchmark item in the modernization checklist.
	- Maintainer release review, broader qualitative quality gates, and final release decision recording remain open under the Phase 3 release checklist.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked docs/help surface:
	- Updated `streamline-extract compare --help` examples to use the current geothermal production schema path and a quoted document-folder example.
	- Remediated active QA/QC implementation docs to remove references to deleted script-driven workflows and the missing `schemas/qaqc/geothermal_qaqc.json` path.
- Validation gate run (PASS):
	- `pixi run streamline-extract compare --help`
	- Search over active `src/`, `docs/`, `README.md`, and `modernization/` content confirmed no remaining references to `schemas/qaqc/geothermal_qaqc.json`, `scripts/test_phase4_integration.py`, or `scripts/test_qaqc_comparison.py`.
- Scope note:
	- This closes the implementation-side cleanup item for deprecated modules/docs/scripts; the remaining open Phase 3 item is benchmark/release gate review.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed final outlier helper scripts `scripts/compare_schemas.py`, `scripts/consolidate_extractions.py`, and `scripts/inspect_hydropower.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
	- `pixi run python -m py_compile scripts/create_validation_comparison.py scripts/migrate_field_names.py scripts/update_validation_spreadsheet.py`
- Scope note:
	- `scripts/compare_schemas.py` targeted retired `air_quality_permits_schema_v2_*` variants that are no longer present.
	- `scripts/consolidate_extractions.py` targeted the retired `data/extracted` / `data/consolidated` permit layout.
	- `scripts/inspect_hydropower.py` depended on a missing one-off workbook under `documents/airobotics/Hydropower.xlsx`.
	- The retained `scripts/` surface is now limited to the validation spreadsheet utilities and `scripts/migrate_field_names.py`.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed remaining spot-check analysis scripts `scripts/analyze_geothermal_keys.py`, `scripts/analyze_section_collisions.py`, `scripts/check_applies_to.py`, `scripts/check_date_formats.py`, `scripts/check_empty_rows.py`, `scripts/check_operating_hours.py`, `scripts/check_permit_keys.py`, and `scripts/check_schedule_r.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- These files were print-only checks over specific consolidated outputs or validation spreadsheets, plus one Chaffee-specific section-collision analysis helper tied to the fixed QA/QC fixture.
	- Their removal leaves only a very small retained utility surface in `scripts/`, centered on schema comparison, spreadsheet maintenance, migration, selective consolidation, and domain-specific inspection.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed one-off deduplication audit/check scripts `scripts/analyze_tariff_duplicates.py`, `scripts/audit_duplicates.py`, `scripts/check_tariff_dedup.py`, and `scripts/check_utah_dedup.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- `scripts/audit_duplicates.py` targeted the retired `data/cleaned` permit layout, while the other three were print-only spot checks over specific consolidated tariff or geothermal outputs rather than reusable tracked utilities.
	- This leaves the remaining `scripts/` surface concentrated on the final retained validation, schema, and spreadsheet-maintenance utilities.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed one-off tariff/validation helper scripts `scripts/extract_service_classifications.py`, `scripts/flatten_tariffs.py`, `scripts/restructure_tariff_data.py`, and `scripts/debug_matching.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- The tariff scripts targeted retired layouts such as `extracted/tariffs_v3_hierarchical`, `extracted/tariffs`, ad hoc `consolidated/tariffs_structured`, or a one-off split-PDF artifact rather than the tracked modern runtime.
	- `scripts/debug_matching.py` was an ad hoc diagnostic around `validation_ground_truth_CS.xlsx`, while the retained validation spreadsheet utilities remain in place.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed script-level experimental test files `scripts/test_cleanup.py`, `scripts/test_deduplication.py`, `scripts/test_document_dedup.py`, `scripts/test_ground_truth_matching.py`, `scripts/test_operations_schema.py`, `scripts/test_page_range.py`, `scripts/test_universal_schema.py`, and `scripts/test_v3_generatorsets.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- These files were not part of the tracked `tests/` suite, had no live references, and several targeted obsolete paths or modules such as missing QA/QC schemas, missing page-range fixtures, retired schema variants, or pre-modernization data/visualization layouts.
	- Their removal leaves `scripts/` focused on retained utility-style helpers rather than experimental script-level tests.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed hard-coded verification scripts `scripts/check_consolidated_output.py`, `scripts/check_extraction.py`, `scripts/verify_consolidated.py`, `scripts/verify_extraction.py`, and `scripts/check_ground_truth.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- These files were print-only spot checks tied to specific geothermal, tariff, or validation artifacts rather than reusable tracked utilities.
	- Their removal further narrows the remaining `scripts/` surface to higher-signal validation, migration, and domain-inspection helpers.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed fixed-fixture QA/QC comparison/debug scripts `scripts/test_comparison.py`, `scripts/test_comparison_engine.py`, `scripts/test_comparison_fix.py`, `scripts/test_focused_comparison.py`, `scripts/test_simplified_comparison.py`, and `scripts/check_working_hours.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- These files were Chaffee-specific comparison/debug helpers against `processed/qa_qc_test/qa_qc/Chaffee County Colorado` and report generation side effects, but were not part of the tracked `tests/` suite or the live CLI/runtime surface.
	- Removing them leaves the remaining `scripts/` surface more focused on retained validation, migration, and domain-inspection utilities.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed exploratory QA/QC analysis scripts `scripts/examine_structure.py`, `scripts/analyze_common_items.py`, and `scripts/section_based_comparison.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- These files were Chaffee-specific exploration helpers tied to the fixed `processed/qa_qc_test/qa_qc/Chaffee County Colorado` fixture rather than reusable tracked utilities.
	- More formal comparison test/debug scripts against the same fixture remain for separate survivor-review decisions.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed one-off inspection scripts `scripts/check_spreadsheet.py`, `scripts/final_verification.py`, and `scripts/compare_raw_data.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- These files were scenario-specific print/debug helpers around a single validation spreadsheet, a hard-coded tariff consolidation outcome, and a fixed QA/QC test directory rather than reusable tracked utilities.
	- Their removal reduces final survivor-review noise without changing CLI/runtime behavior.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete PJM spreadsheet inspection helpers `scripts/check_aggregation.py`, `scripts/check_excel_columns.py`, `scripts/check_de_addresses.py`, `scripts/debug_state_totals.py`, and `scripts/verify_final_totals.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- These files were standalone inspection/debug scripts tied to the missing local spreadsheet `data/compiled/PermitData_PJM_v4.xlsx` rather than the tracked modern extraction runtime.
	- They were not referenced by the live CLI surface, so removal reduces survivor-review noise without affecting runtime behavior.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete visualization helpers `scripts/visualize_from_excel.py` and `scripts/visualize_excel_map.sh`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- These files implemented a standalone PJM permit-mapping workflow around a missing local `data/compiled/PermitData_PJM_v4.xlsx` spreadsheet, ad hoc `data/geocoding_cache.json`, and live Nominatim geocoding rather than the tracked modern extraction runtime.
	- Remaining references to that spreadsheet now indicate other survivor-review candidates rather than a retained visualization entry point.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete scripts `scripts/process_stages_simple.py` and `scripts/process_stages_with_grouping.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- Both scripts were standalone datacenter-stage processing helpers tied to the missing local `data/` tree (`data/reference/data-baxtel.csv`, `data/cleaned/`, and optional `data/compiled/uptime_awards_us_achievements.csv`) rather than the tracked modern extraction runtime.
	- With these removals, the datacenter-stage one-off script cluster is no longer part of the tracked live surface.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete script `scripts/process_datacenter_stages.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- The removed script was an older datacenter-stage helper that depended on live Nominatim reverse-geocoding calls, a non-tracked `data/reference/data-baxtel.csv` input, and ad hoc cache/output files under `data/cleaned/` rather than the modernized runtime surface.
	- Newer datacenter-stage helpers remain for separate survivor-review decisions.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete scripts `scripts/consolidate_tariffs.py` and `scripts/compare_extractions.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- `scripts/consolidate_tariffs.py` depended on the old `extracted/tariffs` layout that has been replaced by the tracked `processed/` and CLI consolidation flow.
	- `scripts/compare_extractions.py` was a one-off hard-coded comparison helper for specific processed output directories rather than a reusable tracked utility.
	- The broader `Clean deprecated modules/docs/scripts` checklist item remains open for final survivor review.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Tightened Phase 3 tracking language after the recent cleanup/remediation sequence:
	- Updated `modernization/tracking/IMPLEMENTATION_CHECKLIST.md` so the open cleanup item reflects the narrower remaining work: final survivor review plus any additional docs cleanup
	- Updated `modernization/tracking/PHASE3_RELEASE_CHECKLIST.md` so the repository-hygiene gate is phrased as a tracked live-surface review rather than an undefined generic cleanup slice
- Validation note:
	- Re-scanned `scripts/` and confirmed no remaining tracked `backupgensprint` or active `decisiontree` references
	- No code-path tests were required for this tracking-only slice

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete scripts `scripts/quick_check.py` and `scripts/diagnose_image_pdfs.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- Both scripts were one-off Virginia PDF diagnostics tied to hard-coded legacy files under `data/permits/Virginia/`, which are no longer part of the tracked repository structure.
	- The broader `Clean deprecated modules/docs/scripts` checklist item remains open for additional tracked-surface cleanup.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete scripts `scripts/check_pdf_pages.py`, `scripts/debug_failed_permits.py`, and `scripts/reextract_with_ocr.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- These scripts were tied to a hard-coded set of legacy Virginia permit PDFs under `data/permits/Virginia/` and `data/extracted/Virginia/`, which are no longer part of the tracked modern repository structure.
	- The broader `Clean deprecated modules/docs/scripts` checklist item remains open for additional tracked-surface cleanup.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 remediation slice in tracked validation utilities:
	- Updated `scripts/migrate_field_names.py` to resolve the workspace root from the repository instead of a dead `/Users/bpulluta/backupgensprint` path
	- Removed stale `decisiontree` validation-directory handling from the active migration path so the script matches the current tracked validation strategies
- Validation gate run (PASS):
	- `pixi run python -m py_compile scripts/migrate_field_names.py`
- Scope note:
	- This script remains a tracked utility for migrating validation JSON field names, so remediation was preferable to deletion.
	- The broader `Clean deprecated modules/docs/scripts` checklist item remains open for additional tracked-surface cleanup.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed a small Phase 3 remediation slice in tracked validation utilities:
	- Updated `scripts/create_validation_comparison.py` to resolve the workspace root from the repository instead of a dead `/Users/bpulluta/backupgensprint` path
	- Updated `scripts/update_validation_spreadsheet.py` to resolve the workspace root from the repository and removed stale `decisiontree` strategy handling from the active path
- Validation gate run (PASS):
	- `pixi run python -m py_compile scripts/create_validation_comparison.py scripts/update_validation_spreadsheet.py`
- Scope note:
	- These scripts remain tracked utility tools, so remediation was preferable to deletion.
	- The broader `Clean deprecated modules/docs/scripts` checklist item remains open for additional tracked-surface cleanup.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete scripts `scripts/verify_styled.py` and `scripts/verify_comparison.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- Both scripts were one-off spreadsheet inspection helpers that only targeted dead absolute paths under `/Users/bpulluta/backupgensprint/validation/` and were not wired into the modernized CLI/runtime path.
	- Larger validation utility scripts that still reference `backupgensprint` remain for a separate remediation or cleanup decision.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete scripts `scripts/extract_uptime_awards.py` and `scripts/debug_uptime_page.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- These scripts were standalone Uptime exploration/debug helpers, relied on direct web-fetch or Selenium inspection flows, and wrote outputs to dead external paths under `/Users/bpulluta/backupgensprint/data/compiled/` rather than the tracked repository structure.
	- The broader `Clean deprecated modules/docs/scripts` checklist item remains open for additional tracked-surface cleanup.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete scripts `scripts/fetch_uptime_page.py` and `scripts/fetch_achievements_js.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- Both scripts were standalone Uptime exploration helpers that wrote to dead external paths under `/Users/bpulluta/backupgensprint/data/compiled/` and were not part of the modernized extraction runtime.
	- The broader `Clean deprecated modules/docs/scripts` checklist item remains open for additional tracked-surface cleanup.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete script `scripts/rerun_failed_extractions.py`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- The removed script imported a nonexistent `PermitExtractor` from `streamline_extract.extraction`, hard-coded pre-modernization Virginia extraction paths under `data/extracted/Virginia/`, and used an old one-off OCR reprocessing workflow outside the modernized runtime.
	- The broader `Clean deprecated modules/docs/scripts` checklist item remains open for additional tracked-surface cleanup.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in tracked peripheral tooling:
	- Removed obsolete script `scripts/scrape_uptime_awards.py`
	- Removed stale `pixi` task entries `scrape-uptime` and `scrape-uptime-test` from `pixi.toml`
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- The removed script imported a nonexistent `streamline_extract.scrapers.uptime_institute` module and wrote to an external dead path outside the repository, so it was no longer a valid part of the modernized tracked surface.
	- The broader `Clean deprecated modules/docs/scripts` checklist item remains open for additional tracked-surface cleanup.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in the live CLI surface:
	- Updated active `process` command examples in `src/streamline_extract/cli/commands.py` from stale `schemas/proprietary/...` paths to the tracked `schemas/personal/...` production schema paths
	- Updated active `consolidate` command examples in `src/streamline_extract/cli/commands.py` to the same current schema layout
- Validation gate run (PASS):
	- `pixi run streamline-extract process --help`
	- `pixi run streamline-extract consolidate --help`
- Scope note:
	- This is a user-facing help cleanup only; the broader `Clean deprecated modules/docs/scripts` checklist item remains open for additional tracked-surface cleanup.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Captured tracked Phase 3 benchmark baseline snapshots from existing run-manifest outputs:
	- `modernization/tracking/phase3_aq_permits_smoke_baseline_snapshot.json` from `output/phase3_manifest_smoke/aq_permits`
	- `modernization/tracking/phase3_tariffs_smoke_baseline_snapshot.json` from `output/phase3_manifest_smoke/tariffs`
	- `modernization/tracking/phase3_tariffs_range_remediated_baseline_snapshot.json` from `output/phase3_manifest_range_remediated/tariffs`
- Validated the new delta-gate benchmark workflow against tracked snapshot evidence:
	- `pixi run streamline-extract benchmark output/phase3_manifest_smoke/aq_permits --baseline-snapshot modernization/tracking/phase3_aq_permits_smoke_baseline_snapshot.json --max-failure-rate 0.005 --max-total-errors 0 --max-throughput-delta-percent 15 --max-cost-delta-percent 10`
	- AQ permits result: PASS, failure rate `0.0%`, total errors `0`, throughput delta `+0.00%`, cost delta `+0.00%`
	- `pixi run streamline-extract benchmark output/phase3_manifest_smoke/tariffs --baseline-snapshot modernization/tracking/phase3_tariffs_smoke_baseline_snapshot.json --max-failure-rate 0.005 --max-total-errors 0 --max-throughput-delta-percent 15 --max-cost-delta-percent 10`
	- Tariff smoke result: PASS, failure rate `0.0%`, total errors `0`, throughput delta `+0.00%`, cost delta `+0.00%`
	- `pixi run streamline-extract benchmark output/phase3_manifest_range_remediated/tariffs --baseline-snapshot modernization/tracking/phase3_tariffs_range_remediated_baseline_snapshot.json --max-failure-rate 0.005 --max-total-errors 0 --max-throughput-delta-percent 15 --max-cost-delta-percent 10`
	- Remediated large-tariff result: PASS, failure rate `0.0%`, total errors `0`, throughput delta `+0.00%`, cost delta `+0.00%`
- Scope note:
	- This records reusable tracked snapshot evidence and proves the rubric-aligned delta-gate workflow end to end.
	- The broader Phase 3 performance benchmark checklist item remains open until maintainer review confirms this evidence set is sufficient or additional benchmark corpus coverage is generated.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed Phase 3 benchmark-baseline tooling slice:
	- `src/streamline_extract/benchmarking/performance.py` now records median per-document duration and cost in addition to averages
	- Added benchmark snapshot read/write support so benchmark evidence can be persisted and reused for later comparisons
	- Added baseline comparison support for rubric-aligned throughput and cost deltas using median document metrics
	- `streamline-extract benchmark` now supports `--baseline-snapshot`, `--write-snapshot`, `--snapshot-label`, `--max-throughput-delta-percent`, and `--max-cost-delta-percent`
	- Updated Phase 3 runbook and release checklist to require a tracked baseline snapshot before evaluating delta gates
- Validation gate run:
	- `pixi run pytest tests/test_performance_benchmark.py`
- Scope note:
	- This slice removes the current tooling blocker for Phase 3 throughput/cost gate evaluation, but the checklist item remains open until fresh benchmark outputs and a tracked baseline snapshot are recorded.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Removed defunct legacy helper scripts that referenced nonexistent commands or modules:
	- Removed `scripts/complete_illinois_pipeline.sh` (`permit-toolkit` wrapper)
	- Removed `scripts/start_api.sh` (nonexistent `streamline_extract.api.main`)
	- Removed `scripts/extract_tariffs.py` (nonexistent `engines.langextract_engine` path)
- Validation gate run (PASS):
	- `pixi run streamline-extract --help`
- Scope note:
	- These files were obsolete utility wrappers outside the modern CLI/runtime path; the cleanup reduces stale script surface without changing the active extraction pipeline.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Removed broken legacy integration scripts under `tests/integration/` that referenced nonexistent geothermal data paths and pre-modernization extraction flow:
	- Removed `tests/integration/test_geothermal_extraction.py`
	- Removed `tests/integration/test_schema_integration.py`
- Validation gate run (PASS):
	- `pixi run pytest tests` (248 passed)
- Scope note:
	- These files were standalone legacy integration scripts rather than active pytest-based modernization coverage; removing them keeps the tracked test tree aligned with the current artifact-driven pipeline.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Cleaned tracked validation scripts that still carried the removed decision-tree strategy:
	- Updated `scripts/update_validation_spreadsheet.py` to compare only active strategies (`AQ Toolkit`, `LlamaExtract`)
	- Updated `scripts/create_validation_comparison.py` to remove `Decision Tree` columns, data loading, and status output
- Validation note:
	- Verified both scripts have no diagnostics after the cleanup and no longer contain tracked `Decision Tree` strategy references

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Cleaned stale extraction packaging metadata after recent Phase 3 removals:
	- Removed nonexistent extraction entries `openai_client.py`, `qa_qc.py`, and `validation_utils.py` from `src/streamline_extract.egg-info/SOURCES.txt`
	- Verified remaining extraction entries in `SOURCES.txt` match the current live extraction package files
- Validation note:
	- No runtime code-path changes in this follow-up slice; verified `src/streamline_extract.egg-info/SOURCES.txt` with diagnostics and remaining extraction-entry checks

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Removed the unused decision-tree extraction surface from the live CLI/runtime path:
	- Removed legacy CLI module `src/streamline_extract/cli/dtree.py`
	- Removed `dtree-extract` command registration from `src/streamline_extract/cli/main.py`
	- Removed legacy extraction package `src/streamline_extract/extraction/decision_trees/`
	- Removed stale packaging entries for the CLI module and decision-tree package from `src/streamline_extract.egg-info/SOURCES.txt`
- Validation gate run (PASS):
	- `pixi run pytest tests/test_cli_features.py tests/test_process_artifact_lineage.py tests/test_multi_model_extractor.py tests/test_consolidation_lineage.py` (45 passed)
- Scope note:
	- This slice removes a user-visible legacy command path that is no longer used; broader `Clean deprecated modules/docs/scripts` work remains open for additional live-path and documentation cleanup.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in live code:
	- Removed unused extraction module `src/streamline_extract/extraction/validator.py`
	- Removed stale packaging entry for the module from `src/streamline_extract.egg-info/SOURCES.txt`
- Validation gate run (PASS):
	- `pixi run pytest tests/test_process_artifact_lineage.py tests/test_multi_model_extractor.py tests/test_consolidation_lineage.py` (23 passed)
- Scope note:
	- This slice removes another legacy hybrid-validation helper not referenced by the modern CLI/runtime path; the broader `Clean deprecated modules/docs/scripts` checklist item remains open.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in live code:
	- Removed unused extraction module `src/streamline_extract/extraction/text_optimizer.py`
	- Removed stale packaging entry for the module from `src/streamline_extract.egg-info/SOURCES.txt`
- Validation gate run (PASS):
	- `pixi run pytest tests/test_process_artifact_lineage.py tests/test_multi_model_extractor.py tests/test_consolidation_lineage.py` (23 passed)
- Scope note:
	- This slice removes another dead extraction helper not referenced by the modern CLI/runtime path; the broader `Clean deprecated modules/docs/scripts` checklist item remains open.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed another small Phase 3 cleanup slice in live code:
	- Removed unused extraction module `src/streamline_extract/extraction/deduplicator.py`
	- Removed unused extraction module `src/streamline_extract/extraction/rate_limiter.py`
	- Removed stale packaging entries for both modules from `src/streamline_extract.egg-info/SOURCES.txt`
- Validation gate run (PASS):
	- `pixi run pytest tests/test_process_artifact_lineage.py tests/test_multi_model_extractor.py tests/test_consolidation_lineage.py` (23 passed)
- Scope note:
	- This slice removes dead extraction helpers not referenced by the modern CLI/runtime path; the broader `Clean deprecated modules/docs/scripts` checklist item remains open for additional active-path and documentation cleanup.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Executed Phase 3 benchmark CLI matrix against existing canonical run-manifest outputs:
	- `pixi run streamline-extract benchmark output/phase3_manifest_smoke/aq_permits --max-failure-rate 0.005 --max-total-errors 0`
	- AQ permits smoke result: PASS, `53.52s` average document time, `$0.0106` average document cost, `1.12` docs/min, failure rate `0.0%`, total errors `0`
	- `pixi run streamline-extract benchmark output/phase3_manifest_smoke/tariffs --max-failure-rate 0.005 --max-total-errors 0`
	- Tariff smoke result: PASS, `7.68s` average document time, `$0.0364` average document cost, `6.16` docs/min, failure rate `0.0%`, total errors `0`
	- `pixi run streamline-extract benchmark output/phase3_manifest_range_remediated/tariffs --max-failure-rate 0.005 --max-total-errors 0`
	- Representative large-tariff remediated result: PASS, `324.97s` average document time, `$0.0574` average document cost, `0.18` docs/min, failure rate `0.0%`, total errors `0`
	- `pixi run streamline-extract benchmark output/phase3_manifest_scale_preflight/tariffs --max-failure-rate 0.005 --max-total-errors 0`
	- Representative large-tariff preflight result: FAIL as expected for the unremediated oversized request, failure rate `100.0%`, total errors `1`
- Gate assessment note:
	- Current Phase 3 benchmark artifacts now provide explicit smoke-scale and representative-large-document evidence for stability and structured-error gates.
	- Phase 3 throughput and cost delta formulas remain blocked because the tracked Phase 0 baseline corpus manifest preserved corpus membership and smoke pass status, but not baseline timing/cost manifests or median values needed for rubric comparison.
	- `Execute performance and scale benchmarks` remains open until a baseline performance snapshot is reconstructed or re-run and compared against the current benchmark matrix.

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Validated page-range remediation on the representative large tariff using tracked config:
	- Used existing tracked config `config/tariffs/page_ranges.csv` with `PSCo_Electric_Entire_Tariff.pdf,49,143`
	- `pixi run streamline-extract process documents/tariffs/PSCo_Electric_Entire_Tariff.pdf --schema schemas/personal/electricity_tariff_schema.json --pages-csv config/tariffs/page_ranges.csv --output output/phase3_manifest_range_remediated/tariffs`
	- Remediated result: PASS, 22 items extracted, 0 structured errors, `324.97s` average document time, `$0.0574` average document cost
	- `pixi run streamline-extract benchmark output/phase3_manifest_range_remediated/tariffs --max-failure-rate 0.005 --max-total-errors 0`
	- Benchmark result: PASS, failure rate `0.0%`, total errors `0`
- Review note:
	- Page-range remediation resolves the context-budget failure mode for the representative PSCo tariff, but throughput is still slow enough that Phase 3 performance-target comparison against baseline remains open

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Added Phase 3 context-budget preflight guard for known large-window model configuration:
	- `src/streamline_extract/extraction/llm_client.py` now estimates prompt token usage before provider execution and raises `ExtractionError` when the request would exceed the known model context window
	- Oversized requests now fail fast with operator guidance to reduce `max_context_chars` or use page ranges/chunking
- Validation gate run (PASS):
	- `pixi run pytest tests/test_extraction_failure_handling.py tests/test_error_taxonomy.py tests/test_process_artifact_lineage.py` (13 passed)
- Recorded improved representative large-document evidence after preflight guard:
	- `pixi run streamline-extract process documents/tariffs/PSCo_Electric_Entire_Tariff.pdf --schema schemas/personal/electricity_tariff_schema.json --output output/phase3_manifest_scale_preflight/tariffs --max-context 1400000`
	- Result after preflight guard: fast explicit failure, `Successful=0`, `Failed=1`, no provider traceback in user-facing output
	- `pixi run streamline-extract benchmark output/phase3_manifest_scale_preflight/tariffs --max-failure-rate 0.005 --max-total-errors 0`
	- Benchmark result: FAIL, failure rate `100.0%`, total errors `1`, total run time reduced to `1.2s`
- Follow-up action:
	- Remaining Phase 3 scale blocker is chunking or page-range remediation for oversized tariff documents

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Fixed Phase 3 reliability gap where hard LLM failures were downgraded into empty successful outputs:
	- `src/streamline_extract/extraction/llm_client.py` now raises `ExtractionError` for empty responses, JSON parse failures, and provider exceptions instead of returning empty payloads
	- `src/streamline_extract/utils/error_taxonomy.py` now classifies context-limit failures as `context_budget/context_window_exceeded`
	- Added regression coverage in `tests/test_extraction_failure_handling.py`
- Validation gate run (PASS):
	- `pixi run pytest tests/test_extraction_failure_handling.py tests/test_process_artifact_lineage.py tests/test_multi_model_extractor.py` (22 passed)
	- `pixi run pytest tests/test_error_taxonomy.py tests/test_extraction_failure_handling.py` (7 passed)
- Recorded representative large-document scale evidence:
	- `pixi run streamline-extract process documents/tariffs/PSCo_Electric_Entire_Tariff.pdf --schema schemas/personal/electricity_tariff_schema.json --output output/phase3_manifest_scale_fixed/tariffs --max-context 1400000`
	- Result after fix: explicit failure with `context_length_exceeded`, `Successful=0`, `Failed=1`
	- `pixi run streamline-extract benchmark output/phase3_manifest_scale_fixed/tariffs --max-failure-rate 0.005 --max-total-errors 0`
	- Benchmark result: FAIL, failure rate `100.0%`, total errors `1`
- Follow-up action:
	- Large-tariff scale runs require context-budget remediation before Phase 3 scale benchmark gates can pass

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Recorded real Phase 3 smoke benchmark evidence using canonical run manifests:
	- AQ permits smoke extraction: `pixi run streamline-extract process documents/aq_permits --schema schemas/personal/air_quality_permits_schema.json --output output/phase3_manifest_smoke/aq_permits -n 1`
	- AQ permits benchmark: `pixi run streamline-extract benchmark output/phase3_manifest_smoke/aq_permits --max-failure-rate 0.005 --max-total-errors 0`
	- AQ permits result: PASS, 1 successful document, 0 errors, 53.52s average document time, $0.0106 average document cost
	- Tariff smoke extraction: `pixi run streamline-extract process documents/tariffs/electric-tariff.pdf --schema schemas/personal/electricity_tariff_schema.json --output output/phase3_manifest_smoke/tariffs`
	- Tariff benchmark: `pixi run streamline-extract benchmark output/phase3_manifest_smoke/tariffs --max-failure-rate 0.005 --max-total-errors 0`
	- Tariff result: PASS, 1 successful document, 0 errors, 7.68s average document time, $0.0364 average document cost
- Follow-up note:
	- Full Phase 3 `Execute performance and scale benchmarks` remains open pending larger-corpus and representative scale runs

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed small Phase 3 cleanup slice in live code:
	- Removed unused `ExtractionCleaner` import from `src/streamline_extract/cli/commands.py`
	- Removed dead unused `save_metadata` helper from `src/streamline_extract/qa_qc/multi_model_extractor.py`
- Validation gate run (PASS):
	- `pixi run pytest tests/test_multi_model_extractor.py tests/test_metadata_validation.py` (18 passed)
- Scope note:
	- Broader deprecated modules/docs/scripts cleanup remains open; this slice only removed dead code in active paths

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed Phase 3 release checklist and operational runbook slice:
	- Added release gate checklist at `modernization/tracking/PHASE3_RELEASE_CHECKLIST.md`
	- Added operational execution runbook at `modernization/tracking/PHASE3_OPERATIONAL_RUNBOOK.md`
	- Marked Phase 3 checklist item complete: finalize release checklist and runbook
- Validation note:
	- No code-path tests required for documentation-only slice; files were added to modernization tracking structure directly

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Added initial Phase 3 performance profile benchmark slice:
	- Added run-manifest performance aggregation helper in `src/streamline_extract/benchmarking/performance.py`
	- Added `streamline-extract benchmark` command to report performance profiles from `run_manifests/` and evaluate threshold gates
	- Benchmark profiles aggregate run duration, per-document duration, throughput, cost, and structured error totals from canonical extraction records
- Added/updated tests:
	- `tests/test_performance_benchmark.py`
- Validation gate run (PASS):
	- `pixi run pytest tests/test_performance_benchmark.py tests/test_run_manifest.py tests/test_process_artifact_lineage.py tests/test_core_contract_schema.py` (24 passed)
- Remaining blocker:
	- No repository `run_manifests/*.manifest.json` files are present yet, so benchmark execution against real corpus outputs is still pending

## 2026-03-25
- Continued on branch: `phase1-artifact-compiler-slice`
- Completed Phase 3 error taxonomy and handling policy slice:
	- Added canonical structured error taxonomy helper in `src/streamline_extract/utils/error_taxonomy.py`
	- Single-model extraction records now emit deterministic `quality.errors` arrays in canonical extraction-record outputs
	- Process run manifests now aggregate structured failures with deterministic `errors.total_errors`, `errors.by_category`, and `errors.by_code`
	- QA/QC metadata now records structured `model_errors` plus aggregated error summaries
	- Consolidation now carries extraction-record error context into row-level columns (`Error Count`, `Error Categories`, `Error Messages`)
	- Core extraction-record contract now validates structured `quality.errors`
- Added/updated tests:
	- `tests/test_error_taxonomy.py`
	- Extended manifest, process-lineage, QA/QC, contract, and consolidation coverage for structured errors
- Validation gate run (PASS):
	- `pixi run pytest tests/test_error_taxonomy.py tests/test_run_manifest.py tests/test_process_artifact_lineage.py tests/test_multi_model_extractor.py tests/test_core_contract_schema.py tests/test_consolidation_lineage.py` (40 passed)

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
