# Process Module — Audit & Cleanup Summary (2026-07-20)

Second pipeline step through `/audit-module`, after `acquire`
(`docs/pipeline_audit/acquire_audit_summary.md`). Same bar: universal, clean,
modular, configurable, scalable — no hardcoding, no redundancy, no legacy. Scope
was **full migration**. QA/QC deferred (WIP).

## What the process step is
Entry point `cli/commands.py::process` → per-document text extraction
(`extraction/document_utils.py`) → `DocumentExtractor.extract`
(`extraction/document_extractor.py`) → `LLMClient.extract`
(`extraction/llm_client.py`, LiteLLM) → record saved via
`_extract_and_save_result`. Config flows
`config/runtime_config_loader.py` (`processing` section) → `process` command.
Optional LLM-assisted page targeting (`extraction/page_locator.py`) and
acquire→process provenance (`_build_source_context_map`).

## Headline changes

### 1. Domain-neutral provider detection + config-driven context window
- Removed the hardcoded customer deployment prefix `compassop-` from provider
  detection. One shared `detect_provider()` now lives in
  `extraction/llm_factory.py`; `LLMClient` imports it (killed the duplicated,
  double-`compassop` `_detect_provider`). Azure is honored via explicit
  `provider: azure` (always set by `config.py`), not a name sniff.
- Deleted the hardcoded `MODEL_CONTEXT_WINDOWS` map (3 customer deployment
  names → 300k). The fail-fast context-budget guard is now driven by a new
  top-level `model_context_windows:` config block (`{model-name -> prompt
  tokens}`), threaded `resolved_inputs → DocumentExtractor → LLMClient`. No
  model names are hardcoded anywhere; unlisted models are simply not guarded.
  **Documented behavior change:** those 3 names no longer auto-guard — opt in via
  config (shown as commented guidance in `TEMPLATE.yaml` +
  `utility_rate_tariffs/run.yaml`). In practice the old guard almost never fired
  (300k tokens ≫ default 400k-char / ~100k-token budget).

### 2. Redundancy — three extraction loops → one
- The live-dashboard / progress-bar / single-file loops were ~85-90% identical.
  Extracted `_process_one_document(...)` (the shared extract→save→result-dict
  core, never raises) + `_document_progress_desc(...)`. Each loop now calls the
  helper and renders only its own UI. ~200 duplicated lines → one 40-line helper
  + three thin loops.

### 3. Pricing — one source of truth
- `cli/cost_tracker.py` lost its private price table; it now delegates to
  `utils/model_pricing.get_model_pricing`.
- The batch cost-estimate gate was wired to gpt-4o-mini rates for **any** model;
  now it prices the actually-selected model via the shared DB (resolved through
  `llm_factory.resolve_model_name`). **Bug fixed:** the `>$1.00` confirmation gate
  is now accurate for non-default models.

### 4. Config deep validator for `processing`
- `processing` had **no** deep validator (unlike `acquisition`), so config-only
  values bypassed Click's type/choice guards. Added
  `_validate_processing_section_schema` (invoked at load): positive-int
  `max_context`/`limit`, bool `skip_existing`/`live_dashboard`, `provider` enum,
  string path fields, and a full `page_targeting` sub-block validator. Added
  `_validate_model_context_windows_block` (non-empty str → positive int).

### 5. Full-migration domain-neutralization
- **Identifier fields:** dropped the hardcoded `"jurisdiction"` from the
  "universal" identifier list. `_extract_and_save_result` now takes
  `identifier_fields`; the command derives them from the schema's
  `extraction.identifier_fields` (via `SchemaMetadata`), falling back to a
  neutral `["id","identifier","number","name"]`.
- **Index filters:** replaced domain-specific `--filter-state` /
  `--filter-jurisdiction` with a generic repeatable `--filter column=value`
  (matches any `download_index.csv` column, slug-normalized). The two old flags
  are kept as thin back-compat aliases onto the generic mechanism.
- **Token metrics:** `LLMClient.extract` now returns `input_tokens` /
  `output_tokens` (from provider usage), surfaced through `ExtractionResult` →
  the saved record's `processing_metrics` and the live dashboard (previously
  permanently stubbed to `None`/`0`).

### 6. Dead code & vocabulary
- Removed duplicate `load_dotenv()`; removed the always-false
  `if validation_report:` branch in `DocumentExtractor.extract`; renamed the
  misnamed provider-agnostic `_extract_with_openai` → `_extract_structured`;
  neutralized the loader's rejected "tier" vocabulary → "alias" (matching
  `llm_factory`); genericized the domain example paths in the `process` docstring.

## Before / after (bounded live pass, azure `compassop-gpt-4.1`)
| Domain | Type | Result | Tokens (in+out) | Cost |
|---|---|---|---|---|
| datacenter_timelines | HTML | 1 item | 6643+395 | $0.0164 |
| geothermal_ordinances | PDF | ok (id=California) | 16617+8517 | $0.1014 |
| industrial_pump_datasheets | PDF | ok (id=Grundfos) | 64686+9510 | $0.2055 |

Token metrics were `None` before this pass; now populated end-to-end. Extraction
ran on the configured Azure env model (provider detection unaffected by the
`compassop` removal). Config-driven guard proven: a `run.yaml`
`model_context_windows: {compassop-gpt-4.1: 500}` failed fast with
`context_window_exceeded` before any API call.

## Config surface added (validated in the loader, shown in shipped configs)
- Top-level `model_context_windows:` (validated; passed through like `models`).
- `processing` deep validation (types/ranges/enum + `page_targeting` sub-block).
- `TEMPLATE.yaml`: commented `models:` + `model_context_windows:` guidance;
  `utility_rate_tariffs/run.yaml`: commented `model_context_windows` hint.

## Deferred (documented, with reason)
- `_context_budget_suggestions_for_process` repo-layout coupling (`documents/`
  detection, `parents[3]`) — hint text / install-layout only, no domain literal.
- QA/QC path (`_run_qa_qc_extraction`, `qa_qc/*`, `model_detector.py`
  `compassop`/`QAQC_MODELS`) — WIP, explicitly out of scope.
- `utils/model_pricing.get_model_pricing` partial-match matcher can mis-map a bare
  family name (e.g. `gpt-4` → a nano variant). Pre-existing; not process-specific.

## Verification
- `pixi run python -m pytest tests/ -q` → **740 passed** (723 baseline; new
  `tests/test_process_audit.py` (10), loader validator tests (6), context-guard
  off test (1); updated 3 tests to the new contracts — none weakened).
- Strict-loaded every shipped `config/*/run.yaml`.
- Bounded live pass on 3 distinct domains + end-to-end context-guard test (above).
