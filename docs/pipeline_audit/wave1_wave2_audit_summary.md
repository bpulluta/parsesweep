# Wave 1 + Wave 2 Audit Summary
**Branch:** `cleanup/wave1-wave2-audit`  
**Date:** 2026-07-29  
**Baseline:** 651 tests → **652 tests** (net +1 from new model_detector test)

---

## Scope

Targeted audit and cleanup of five problem categories across three Wave 1 files
and three Wave 2 cross-cutting issues.

---

## Wave 1 — Dead-Code & Redundancy

### `extraction/schema_utils.py`

| # | Problem | Fix |
|---|---------|-----|
| 1 | Missing `encoding="utf-8"` — schema loading was locale-dependent | Added `encoding="utf-8"` to `open()` call |
| 2 | `SchemaMetadata._load_schema` duplicated the same `json.load` body | Replaced the copy with a delegation to `load_schema`; removed the redundant `import json` from `schema_metadata.py` |

**Before** (two independent loaders):
```python
# schema_utils.py
with open(schema_path, "r") as f:      # no encoding
    return json.load(f)

# schema_metadata.py
with open(self.schema_path, "r", encoding="utf-8") as f:   # separate impl
    return json.load(f)
```
**After** (single source of truth):
```python
# schema_utils.py
with open(schema_path, "r", encoding="utf-8") as f:
    return json.load(f)

# schema_metadata.py
return load_schema(self.schema_path)   # delegates to shared utility
```

### `utils/value_normalizer.py` — **Confirmed clean**

Both `normalize_value` and `is_numeric_value` are live, called from
`qa_qc/comparison_engine.py`, and covered by 15 passing unit tests.
No dead code, no commented-out blocks, no TODOs.

### `utils/config.py` — **Confirmed clean (Wave 2 items deferred)**

All attributes (`project_root`, `schema_dir`, `default_schema`, `llm_config`)
are externally consumed. No dead code found. Hardcoded model-name defaults
(`"gpt-4o-mini"`, `"claude-3.5-sonnet"`, `"gemini-1.5-pro"`) were noted as
Wave 2 scope; they remain as-is because they are fallback labels when the user
has not configured a model, and removing them would require a broader
`llm_factory` refactor outside this wave's scope.

---

## Wave 2 — Hardcoding & Silent Exceptions

### 1. QA/QC model hardcoding (`qa_qc/model_detector.py`)

**Before** — silent provider-based fallbacks:
```python
AZURE_QA_MODELS = ["gpt-4o", "gpt-4.1"]
OPENAI_QA_MODELS = ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]  # gpt-3.5-turbo deprecated

def get_qa_models():
    if not QAQC_MODELS env:
        # silently picked provider-based defaults; Azure names are env-specific → runtime 500
        return AZURE_QA_MODELS / OPENAI_QA_MODELS
```

**After** — loud fail requiring explicit configuration:
```python
def get_qa_models():
    if not QAQC_MODELS env:
        raise ValueError(
            "QAQC_MODELS environment variable is not set. "
            "Set QAQC_MODELS with 2+ comma-separated deployment names, e.g.:\n"
            "  QAQC_MODELS=my-gpt-4o-deployment,my-gpt-4.1-deployment"
        )
```

`AZURE_QA_MODELS` and `OPENAI_QA_MODELS` class constants removed entirely.
`get_model_info()` updated to surface the error message rather than crashing.
Four fallback tests replaced with four error-assertion tests; one new
diagnostic test added. Net: **22 model_detector tests pass**.

### 2. Domain-specific hardcoding in `key_fields` preference lists (`cli/utils_commands.py`)

**Before:**
```python
quantitative_preferences = [
    "value", "rate", "amount",
    "ratedCapacityKW",   # ← electric utility-specific camelCase field
    "capacity", "unit", "units",
]
```
**After:** `"ratedCapacityKW"` removed. Generic names only.

**Schema auto-matching — two locations fixed:**

*Location A (document preview, ~line 1270):*  
Replaced hard-coded `if "geothermal" in path … elif "tariff" in path …`
block with `difflib.get_close_matches(doc_path.stem, schema_stems)`, falling
back to the first alphabetical schema.

*Location B (setup wizard, ~line 1169):*  
Replaced `if doc_type == "tariffs" … elif doc_type == "ordinances" …` block
with the same `difflib.get_close_matches(doc_type, schema_names)` pattern.

### 3. Silent exception logging (`extraction/`, `compilation/`)

Three bare `except` blocks annotated with `# noqa: BLE001` comments documenting
why silence is intentional:

| File | Context | Reason |
|------|---------|--------|
| `extraction/document_utils.py:561` | `_is_js_rendered_shell` HTML parse failure | Can't judge content; treat as non-shell |
| `compilation/excel_formatter.py:188` | column-width loop cell error | Cosmetic; any cell error is non-fatal |
| `compilation/synthesizer.py:295` | `_out_of_order` non-numeric check | Non-numeric values can't be compared |

Blocks in `comparison_engine.py` and `multi_model_extractor.py` already had
`logger.error(...)` on the next line — no change needed.

---

## Verification

```
pixi run python -m pytest tests/ -q --ignore=tests/test_browser_digger.py
652 passed in 30.63s
```

---

## Deferred Items

| Item | Reason |
|------|--------|
| `utils/config.py` hardcoded LLM model-name defaults (`"gpt-4o-mini"`, etc.) | Requires `llm_factory` refactor; tracked for a dedicated credentials/config cleanup wave |
| `qualitative_preferences` list in `utils_commands.py` (contains `"extracted_text"`) | Borderline generic; no active complaint; safe to leave |
| Discovery module silent exceptions (`digger.py`, `validators.py`) | Discovery is a separate subsystem with its own audit; out of scope here |
