# Data Directory Structure

This directory contains all permit data at various stages of the pipeline.

## Structure

```
data/
├── permits/          # PDF permit files (gitignored - large files)
│   ├── Virginia/    # 177 PDFs
│   ├── Illinois/    # 12 PDFs
│   └── ...
├── extracted/       # JSON extractions from LLM (gitignored - large files)
│   ├── Virginia/    # 177 JSON files
│   ├── Illinois/    # To be extracted
│   └── ...
└── outputs/         # Final CSV datasets (tracked in git)
    ├── virginia_generators.csv  # 592 generator records
    └── ...
```

## Usage

### View Existing Data

The Virginia dataset is complete and available in `outputs/`:
- `virginia_generators.csv` - 592 generator records from 177 permits

### Extract New Data

Illinois permits are ready for extraction:

```bash
# Extract Illinois permits
permit-toolkit extract --state Illinois --test 2

# Or extract all
permit-toolkit extract --state Illinois
```

### Consolidate New State

After extraction:

```bash
permit-toolkit consolidate \
  --input data/extracted/Illinois \
  --output data/outputs/illinois_generators.csv
```

## Git Tracking

- ❌ **All Ignored**: `permits/`, `extracted/`, and `outputs/` (can be reproduced)
- ✅ **Reproducible**: Follow the pipeline to regenerate all data
- 💡 **Why**: Keep repo lightweight; all data can be regenerated from PDFs using the toolkit

## Reproducing Virginia Data

```bash
# Virginia is already extracted, consolidate it
permit-toolkit consolidate \
  --input data/extracted/Virginia \
  --output data/outputs/virginia_generators.csv
```

## Reproducing Illinois Data (Full Pipeline Test)

```bash
# 1. Extract from PDFs (12 permits)
permit-toolkit extract --state Illinois --model gpt-4o

# 2. Consolidate to CSV
permit-toolkit consolidate \
  --input data/extracted/Illinois \
  --output data/outputs/illinois_generators.csv
```
