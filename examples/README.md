# Usage Examples# Usage Examples



Production-ready examples for the Air Quality Permit Toolkit.Practical examples demonstrating the Air Quality Permit Toolkit.



## Quick Start## Quick Start



```bashRun examples in order:

# 1. Download sample permits

python examples/01_scrape_permits.py```bash

# 1. Download sample permits

# 2. Extract structured data  python examples/01_scrape_permits.py

python examples/02_extract_data.py

# 2. Extract structured data

# 3. Convert to CSVpython examples/02_extract_data.py

python examples/03_consolidate_data.py

```# 3. Convert to CSV format

python examples/03_consolidate_data.py

## Examples```



### basic_usage.py## Example Scripts

Simple demonstration of the extraction workflow.

### 01_scrape_permits.py

### 01_scrape_permits.py

Download sample permits from Virginia DEQ.Downloads sample permits from Virginia DEQ.



### 02_extract_data.py**What it does:**

Extract structured data with HTML visualizations.- Downloads 10 test permits from Virginia

- Saves PDFs to `data/permits/Virginia/`

### 03_consolidate_data.py- Skips already downloaded files

Convert JSON to CSV format.

```bash

## Batch Processingpython examples/01_scrape_permits.py

```

For production use:

### 02_extract_data.py

```bash

python batch_extract_with_visualization.pyExtracts structured data using hybrid extraction (OpenAI + LangExtract).

```

**What it does:**

## Configuration- Processes PDFs from `data/permits/Virginia/`

- Extracts generator specs and emissions data

Set in `.env` file:- Saves JSON to `data/extracted/Virginia/`

```- Generates HTML visualizations for QA review

OPENAI_API_KEY=your_key_here

```**Requirements:**

- OpenAI API key in `.env` file
- PDFs in permits directory (run 01 first)

```bash
python examples/02_extract_data.py
```

### 03_consolidate_data.py

Consolidates JSON files into CSV format.

**What it does:**
- Reads JSON from `data/extracted/Virginia/`
- Flattens nested structures
- Saves CSV to `data/outputs/virginia_generators.csv`

**Requirements:**
- Extracted JSON files (run 02 first)

```bash
python examples/03_consolidate_data.py
```

### basic_usage.py

Complete end-to-end workflow in a single script.

```bash
python examples/basic_usage.py
```

## Batch Processing

For production workloads, use the batch script in the project root:

```bash
python batch_extract_with_visualization.py
```

This processes multiple permits with validation and generates comprehensive QA visualizations.

## Configuration

All examples use settings from `.env` file:

```bash
OPENAI_API_KEY=your_key_here
```

## Output Files

After running examples:

```
data/
├── permits/Virginia/              # Downloaded PDFs
├── extracted/Virginia/            # JSON extraction results
├── comparison/Virginia/           # HTML visualizations
│   └── visualizations/
└── outputs/
    └── virginia_generators.csv    # Consolidated CSV
```

## Tips

- Start with 01 → 02 → 03 for first-time setup
- Use `basic_usage.py` to understand the full workflow
- Review HTML visualizations to verify extraction quality
- Batch script in root directory for production processing
