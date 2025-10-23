# Usage Examples

This directory contains practical examples demonstrating how to use the Air Quality Permit Toolkit.

## Examples Overview

### 01_scrape_permits.py
Demonstrates web scraping permits from Virginia DEQ.

```bash
python examples/01_scrape_permits.py
```

**What it does:**
- Downloads 10 test permits from Virginia
- Saves PDFs to `data/permits/Virginia/`
- Skips already downloaded files

### 02_extract_data.py
Demonstrates LLM-based data extraction from PDFs.

```bash
python examples/02_extract_data.py
```

**What it does:**
- Loads PDFs from `data/permits/Virginia/`
- Extracts structured data using GPT-4
- Saves JSON to `data/extracted/Virginia/`

**Requirements:**
- OpenAI API key in `.env` file
- PDFs in the permits directory (run example 01 first)

### 03_consolidate_data.py
Demonstrates consolidating JSON files into CSV.

```bash
python examples/03_consolidate_data.py
```

**What it does:**
- Reads JSON files from `data/extracted/Virginia/`
- Flattens to tabular format
- Saves CSV to `data/outputs/virginia_generators.csv`

**Requirements:**
- Extracted JSON files (run example 02 first)

### 04_complete_workflow.py
Complete end-to-end workflow example.

```bash
python examples/04_complete_workflow.py
```

**What it does:**
- Runs all three steps sequentially
- Scrapes → Extracts → Consolidates
- Produces final CSV dataset

**Requirements:**
- OpenAI API key in `.env` file

## Running Examples

### Prerequisites

1. Install the package:
```bash
pip install -e .
```

2. Create `.env` file:
```bash
cp .env.example .env
# Add your OPENAI_API_KEY
```

### Run Individual Examples

```bash
# Activate virtual environment
source .venv/bin/activate

# Run examples in order
python examples/01_scrape_permits.py
python examples/02_extract_data.py
python examples/03_consolidate_data.py

# Or run complete workflow
python examples/04_complete_workflow.py
```

## Using as a Library

You can also import and use these functions in your own code:

```python
from pathlib import Path
from permit_toolkit.scrapers.virginia import VirginiaScraper
from permit_toolkit.extraction import PermitExtractor, load_schema
from permit_toolkit.consolidation import PermitConsolidator

# Your custom code here...
```

## Customization

### Processing Different States

Modify the examples to work with other states:

```python
# In any example, change:
state = "Virginia"
# To:
state = "Illinois"  # or any other state
```

### Adjusting Test Limits

Change the number of permits processed:

```python
# In scraper:
test_count=10  # Change to desired number

# In extractor:
pdf_files = pdf_files[:10]  # Change slice
```

### Using Different Models

Try different OpenAI models:

```python
extractor = PermitExtractor(
    api_key=api_key,
    schema=schema,
    model_id="gpt-4o-mini"  # Faster, cheaper
    # model_id="gpt-4o"      # More accurate
)
```

## Cost Considerations

The extraction step uses OpenAI's API:
- **gpt-4o**: ~$0.01-0.05 per permit (more accurate)
- **gpt-4o-mini**: ~$0.001-0.005 per permit (faster, cheaper)

Always test with `--test 5` first to estimate costs!

## Next Steps

After running examples:
1. Check `data/outputs/` for your CSV files
2. Analyze data with pandas, Excel, or BI tools
3. Customize for your specific use case
4. Scale up to process more permits

## Questions?

See the main README.md or open an issue on GitHub.
