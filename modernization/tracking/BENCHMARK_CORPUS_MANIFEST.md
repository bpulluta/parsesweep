# Benchmark Corpus Manifest (Phase 0 Freeze)

## Status
Frozen (initial)

## Date
2026-03-24

## Branch
phase0-benchmark-manifest-freeze

## Scope
This manifest freezes the benchmark corpus document set and deterministic ordering for modernization validation.

## Deterministic Ordering Rule
All file lists are ordered using `LC_ALL=C sort` on repository-relative paths.

## Domains

### Geothermal Ordinances
Schema: `schemas/personal/geothermal_ordinance_schema.json`
Document count: 6

1. `documents/geothermal_ordinances/Chaffee County Colorado.pdf`
2. `documents/geothermal_ordinances/Chapter_16.16___OIL__GAS_AND_GEOTHERMAL_RESOURCES.docx`
3. `documents/geothermal_ordinances/Iron County, UT Code of Ordinances.pdf`
4. `documents/geothermal_ordinances/lyoncountynv-nv-1.pdf`
5. `documents/geothermal_ordinances/malheurcoor-or-1.pdf`
6. `documents/geothermal_ordinances/title-9-division-17-geothermal-ordinance-10-6-15.pdf`

### Tariffs
Schema: `schemas/personal/electricity_tariff_schema.json`
Document count: 2

1. `documents/tariffs/PSCo_Electric_Entire_Tariff.pdf`
2. `documents/tariffs/electric-tariff.pdf`

### Air Quality Permits
Schema: `schemas/personal/air_quality_permits_schema.json`
Document count: 1

1. `documents/aq_permits/52432_DC_Permit.pdf`

Cross-domain benchmark coverage blocker is now resolved for AQ permit presence.

## Reproducibility Check Commands
Run these commands twice and ensure outputs are identical:

```bash
find documents/geothermal_ordinances -type f ! -name '.DS_Store' -print0 | while IFS= read -r -d '' f; do printf '%s\n' "$f"; done | LC_ALL=C sort
find documents/tariffs -type f -print0 | while IFS= read -r -d '' f; do printf '%s\n' "$f"; done | LC_ALL=C sort
find documents/aq_permits -type f -print0 | while IFS= read -r -d '' f; do printf '%s\n' "$f"; done | LC_ALL=C sort
```

## Notes
- This freeze is an initial Phase 0 baseline. It is valid for currently available documents only.
- AQ permits benchmark coverage blocker is resolved as of 2026-03-24 with one representative document.

## Validation Evidence (2026-03-24)

### Reproducibility
- Geothermal deterministic list check: PASS (`GEOTHERMAL_REPRO_OK`)
- Tariff deterministic list check: PASS (`TARIFF_REPRO_OK`)
- AQ deterministic list check: PASS (`AQ_REPRO_OK`, 1 file)

### Smoke Extraction
- Geothermal smoke run: PASS (1/1 file processed, 54 items, output in `output/modernization_smoke/geothermal`)
- Tariff smoke run: PASS (1/1 file processed, 8 items, output in `output/modernization_smoke/tariffs`)
- AQ smoke run: PASS (1/1 file processed, 4 items, output in `output/modernization_smoke/aq_permits`)
