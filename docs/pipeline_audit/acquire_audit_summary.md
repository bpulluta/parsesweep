# Acquire Module — Audit & Cleanup Summary (2026-07-20)

The worked example for `/audit-module`. This is what "universal, clean, modular,
configurable, optimized, scalable — no hardcoding, no redundancy, no legacy" looks
like when applied to one pipeline step. Reuse the same shape for `process`,
`consolidate`, and their sub-steps. (QA/QC deferred — work in progress.)

## What the acquire step is
Entry point `AcquisitionEngine.run()` (`src/streamline_extract/acquisition/engine.py`).
Stages: target load → SerpApi seek (`connectors/serpapi_seeker.py`) → candidate
selection (`candidate_selector.py`) + ranking (`link_prioritizer.py`) → routing/crawl
(`connectors/digger.py`) → download → keyword classify → LLM review
(`document_reviewer.py`) → curate → manifest. Config flows
`config/runtime_config_loader.py` → `cli/commands.py::acquire` → `AcquisitionRequest`.

## Headline change: per-stage model selection (cost lever, universal)
- New `extraction/llm_factory.py` — ONE resolver (`resolve_model_name` /
  `resolve_llm_kwargs` / `build_llm_client`). Every LLM stage routes through it.
- Each stage's `model:` is simply a model name; unset → inherits the env model.
  Optional top-level `models:` block defines **user-named** aliases (no reserved
  names). Works for every provider (threads Azure endpoint/version + key from
  `get_config().llm_config`). Honest limit: cross-provider-per-stage needs that
  provider's key in the ambient env.
- Replaced two duplicated `os.getenv`/`gpt-4o-mini`-hardcoded `_ensure_client`
  blocks (`document_reviewer.py`, `extraction/page_locator.py`).
- **Bug fixed:** `cli/commands.py` process path silently discarded
  `processing.model` for non-OpenAI providers; now honored for all providers
  (env-only runs unchanged, byte-for-byte legacy `else`). Verified live: extraction
  + synthesis ran on the configured Azure model, not the env default.

## Efficiency / cost
- **Review grade cache** (`document_reviewer.py`): `.review/<stem>.json` now stores a
  `cache_key` (size+mtime+description+model) and is read back to skip re-grading
  (note reports "graded N (M reused from cache)"); preserves human decisions.
  Proven live: 10 files re-reviewed → 0 LLM calls.
- **SerpApi result cache** (`connectors/serpapi_seeker.py`): `search.cache: true`
  (+ optional `cache_ttl_minutes`) caches raw results under
  `output/acquisition/<domain>/.serpapi_cache/`, keyed on params minus api_key.
- Kept LLM review as one call/document (per-doc accuracy beats bundling; mini
  model + cache remove the cost pressure).

## Redundancy removed (shared primitives)
- New `acquisition/retry.py` (`compute_backoff`, `is_transient_error`) replaces
  triplicated retry/backoff + transient-error logic in engine + seeker + digger.
- Digger `_is_document_url` now uses the shared `urls.url_extension` (one canonical
  extension parser instead of three notions of "is a document").

## Dead / legacy code removed
- Duplicate `@staticmethod` decorator in `engine.py`.
- Six unused `STATUS_*` constants in `constants.py` (dead "single source of truth"
  — the engine used literals).
- Test-scaffolding leaked into runtime manifests: `meets_fixture_gate` →
  `meets_coverage_threshold`, `gate_threshold` → `coverage_threshold`,
  `measurement_mode: fixture_index_links` → `seeded_index_links` (+ tests updated).
- Domain-flavored review prompt (news-biased / legal-flavored) made
  description-driven and domain-neutral; legal status literal
  `skipped_non_legal_document` → `skipped_selection_filter`.

## Config surface added (validated in the loader, shown in shipped configs)
- Top-level `models:` alias map (validated as `{str: non-empty str}`; passed to
  every command like `domain`).
- `acquisition.search.cache` / `cache_ttl_minutes`.
- Per-stage `model:` shown as commented guidance in shipped configs (portable —
  no hardcoded deployment names).

## Validated across 3 distinct domains (bounded live runs)
- `industrial_pump_datasheets` — NEW domain authored from scratch (config + schema
  only, no code): acquire → mini-model review picked the datasheet over an install
  manual → curate.
- `geothermal_ordinances` — PDF + review; review grade-cache reuse proven live.
- `datacenter_timelines` — web/HTML + synthesis; extraction + synthesis on the
  configured model (confirms the non-OpenAI model bug fix).
- 723 tests pass (new: `tests/test_llm_factory.py`, review-cache + serpapi-cache
  tests; updated metric + loader tests).

## Deferred (config-gated / general heuristics — candidates for a later pass)
- `engine._STATE_ALIASES` (US state table) + `_infer_jurisdiction_from_query` —
  only active when `partition_mode=jurisdiction`. Migrate to a config-supplied
  alias map for full universality.
- kW power-class scoring in `link_prioritizer.py` — only active when
  `power_range_kw` is set (generator domain). Generalize to config-driven numeric
  URL matching.
- Default authority (.gov/.edu) + shopping/social penalty lists in
  `link_prioritizer.py` — general heuristics, already overridable via
  `link_prioritization.domain_scores`; consider fully config-driving the defaults.
- Near-duplicate distributed/centralized routing methods in `engine.py` — consolidate.

## Verification commands
```
pixi run python -m pytest tests/ -q
# strict-load every shipped config
pixi run python - <<'PY'
from pathlib import Path, PurePath
from streamline_extract.config.runtime_config_loader import load_runtime_config_file, resolve_command_config
import glob
for p in sorted(glob.glob('config/*/run.yaml')):
    d = load_runtime_config_file(Path(p))
    for c in ('acquire','process','consolidate'):
        s={'acquire':'acquisition','process':'processing','consolidate':'consolidation'}[c]
        if d.get(s): resolve_command_config(command=c, cli_values={}, config_data=d, strict=True)
    print('OK', p)
PY
# bounded live pass:  set -a && . ./.env && set +a
pixi run streamline-extract acquire --config config/industrial_pump_datasheets/run.yaml
```
