# Consolidate Module — Audit & Cleanup Summary (2026-07-20)

Third worked example of `/audit-module`, after `acquire` and `process`. Same bar:
universal, clean, modular, configurable, optimized, scalable — no hardcoding, no
redundancy, no legacy. (QA/QC deferred — work in progress.)

## What the consolidate step is
Entry point `cli/commands.py::consolidate`. Two paths, both schema-driven:
- **Tabular** (default): `Consolidator.consolidate_from_directory()`
  (`consolidation/consolidator.py`) → `SchemaDetector` → `DataFlattener` (nested JSON →
  columns) → `Deduplicator` (schema key-field dedup) → `ExcelFormatter`/`CsvExporter`.
- **Synthesis** (config-gated LLM): `Synthesizer` (`consolidation/synthesizer.py`)
  reconciles many per-document records into ONE row per entity (conflict resolution +
  confidence + citations + optional ordering constraint). Model routes through
  `extraction/llm_factory.py`. Config flows `runtime_config_loader.py` →
  `consolidate` → `synthesis` block.

## Dead / legacy code removed
- **`consolidation/archive/` (4 files, ~82 KB)** — incl. domain-specific
  `geothermal_consolidator.py` and `consolidate_generic.py` (imported a nonexistent
  `PermitConsolidator`). Was git-ignored local dead code; deleted.
- **`consolidation/cleaner.py` (`ExtractionCleaner`, 390 lines)** — wired to no command;
  used the pre-v2 contract (`data.get("data")`, `completeness_score`, `cost_usd`,
  `source_file`) that no longer exists (canonical is `payload`/`lineage`). Deleted, with
  its only caller — the dead `test_cleaner_rejects_none_metadata`.

## Hardcoding removed → config-gated (defaults preserve behavior)
- **`data_flattener.py` domain-neutralized.** The energy/tariff magic lists
  (`type_fields`, `skip_fields`, `distinguishing_fields`, value/unit/season field names,
  the feet/inches/dBA `normalize_units` map, and the expand-vs-summarize thresholds) are
  now driven by an optional `consolidation.flattening` schema block. Every knob falls back
  to a documented module default, so schemas that declare nothing behave **byte-for-byte
  identically** (proven live — see Verification). New getter
  `SchemaMetadata.get_flattening_config()`. `core/artifact_compiler.py` only uses
  `make_column_name` → unaffected.
- **US-state normalization gated.** `consolidator.py` no longer unconditionally
  normalizes a `State` column; it consults
  `consolidation.normalization.state_column` (default `"State"` = legacy behavior; set
  `null` to opt a non-US domain out). New getter
  `SchemaMetadata.get_state_normalization_column()`.

## Config / model surface (validated in the loader, shown in shipped configs)
- **`consolidation.synthesis` is now validated** at load time
  (`_validate_consolidation_section_schema` / `_validate_synthesis_block` in
  `runtime_config_loader.py`), mirroring the acquisition/processing validators. Previously
  it was an unchecked passthrough — a mistyped key (e.g. `group_bye`) silently produced
  empty output. Enforced: types/shapes of every subkey, `group_by` required when
  `enabled`, `reconcile_fields` entries need a non-empty `field`, and
  `ordering_constraint` may only reference reconciled fields. `min_sources_for_llm` must
  be a non-negative int.
- **`config/TEMPLATE.yaml`** now documents the optional `flattening`, `normalization`,
  and `synthesis` blocks as commented, portable guidance (no deployment-specific values).
- Synthesis `model` already routed through `extraction/llm_factory.py` (per-stage,
  provider-agnostic) — unchanged, confirmed correct.

## Efficiency / cost (already good — confirmed, not changed)
- Synthesizer's deterministic path: the LLM is called **only** when
  `>= min_sources_for_llm` sources carry a reconcilable value; 0/1 sources resolve with no
  API call. Proven live (off-topic + single-source cases skip the LLM).

## Tests (audited for necessity / non-redundancy per request)
- 754 pass (was 740; −1 dead cleaner test, +15 new).
- **Removed:** `test_cleaner_rejects_none_metadata` (tested deleted dead code).
- **Added:**
  - `tests/test_runtime_config_loader.py` — 6 synthesis-config validation tests
    (valid block accepted; unknown key, missing `group_by`, bad `reconcile_fields`,
    ordering referencing a non-reconciled field, negative `min_sources_for_llm` rejected).
  - `tests/test_data_flattener.py` (new) — 5 tests: defaults preserve legacy energy
    behavior; a NON-energy schema redirects grouping/distinguisher; unit-normalization map
    is replaceable and can be disabled; expand thresholds configurable.
  - `tests/test_consolidation_lineage.py` — 2 state-normalization gate tests
    (default abbreviates; config `null` opts out).
  - `tests/test_synthesizer.py` — 2 tests: LLM-merge row assembly (joined citations,
    narrative field, source count) and end-to-end `synthesize_from_directory`
    (recursive load, `run_manifests` skipped, one row per group).
- **Kept** (verified each covers distinct behavior, no redundancy):
  `test_consolidation_lineage.py` lineage/exclude/renames/dedup-preview suite; the 3
  remaining metadata guard tests (distinct public classes).

## Validated across distinct domains (bounded live runs)
- **Tabular, byte-for-byte no-regression:** `geothermal_small` (25 rows) and
  `residential_electricity_rates` (71 rows) consolidated; the residential CSV produced
  before vs. after the flattener change is **identical** (`diff` empty) — defaults
  preserved.
- **Synthesis / LLM path:** a tiny synthetic data-center input (2 on-topic sources with a
  conflicting announced date + 1 off-topic) → off-topic filtered by `relevance_flag` →
  1 LLM reconciliation call → conflicting `2023-03` vs `2023-02-15` resolved to the more
  specific `2023-02-15`, `ordering_consistent=True`, `citation_urls` = both on-topic URLs
  only, `sources_checked=2`, narrative `current_status` populated. Full domain-neutral
  reconciliation end-to-end via config.
- All 6 shipped `config/*/run.yaml` strict-load (incl. `datacenter_timelines` synthesis
  block).

## Deferred (candidates for a later pass)
- `deduplicator.py` `HIGH/MEDIUM_SEVERITY_TOKENS` name heuristics + jurisdiction column
  list (`State/County/City/Municipality/Jurisdiction`) and fuzzy fields
  (`Condition/Applies To`) — active only in the dedup *preview*; schema severity hints
  already take priority, so this is a general fallback. Config-drive later.
- `excel_formatter.py` center-align column-name list — cosmetic only.

## Verification commands
```
pixi run python -m pytest tests/ -q                      # 754 pass
# strict-load every shipped config (incl. datacenter synthesis)
pixi run python - <<'PY'
from pathlib import Path; import glob
from psweep.config.runtime_config_loader import load_runtime_config_file, resolve_command_config
for p in sorted(glob.glob('config/*/run.yaml')):
    d = load_runtime_config_file(Path(p))
    for c in ('acquire','process','consolidate'):
        s={'acquire':'acquisition','process':'processing','consolidate':'consolidation'}[c]
        if d.get(s): resolve_command_config(command=c, cli_values={}, config_data=d, strict=True)
    print('OK', p)
PY
# bounded live:  set -a && . ./.env && set +a
pixi run psweep consolidate processed/geothermal_small --schema schemas/personal/geothermal_ordinance_schema.json --output /tmp/g
```
