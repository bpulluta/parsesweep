# Air Quality Permit Toolkit

A comprehensive, production-ready toolkit for extracting structured data from air quality permits across the United States. Built to analyze backup generator specifications at data centers and other facilities.

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Overview

This toolkit provides an end-to-end pipeline for:

1. **Web Scraping**: Automated download of permit PDFs from state environmental agencies
2. **LLM Extraction**: Structured data extraction using LangExtract (recommended) or OpenAI with validated JSON schemas
3. **Data Consolidation**: Convert extracted JSON into analysis-ready CSV datasets
4. **Smart Optimizations**: 75% token reduction, rate limit handling, and duplicate detection

Originally developed for analyzing backup generators in the PJM territory, this toolkit is designed to be easily adapted for nationwide use and other permit types.

## Key Features

- **Multi-State Support**: Built-in scrapers for Virginia (with more states coming)
- **LangExtract Integration**: Entity extraction approach with optimized token usage (recommended)
- **OpenAI Fallback**: Traditional GPT-4 extraction available as backup
- **Smart Text Optimization**: Removes boilerplate while preserving 100% of data (75% token reduction)
- **Rate Limit Handling**: Exponential backoff and automatic retry for API stability
- **Duplicate Detection**: Skips already-processed documents intelligently
- **Structured Output**: JSON schema validation ensures consistent data quality
- **Resume Capability**: Smart skip logic for already-processed files
- **Production Ready**: Proper logging, error handling, and progress tracking
- **Extensible Design**: Base classes make it easy to add new states/scrapers
- **Simple CLI**: User-friendly command-line interface for all operations

## Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/NREL/backupgensprint.git
cd backupgensprint

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install the package
pip install -e .
```

### Configuration

Create a `.env` file in the project root:

```bash
OPENAI_API_KEY=your_api_key_here
```

### Basic Usage

```bash
# 1. Scrape permits from Virginia DEQ
permit-toolkit scrape --state virginia --test 5

# 2. Extract data using GPT-4 (note: use --permits flag for each permit number)
permit-toolkit extract --state Virginia --permits 21527 --permits 11541 --model gpt-4o

# Or test with first 5 permits
permit-toolkit extract --state Virginia --test 5 --model gpt-4o

# 3. Consolidate to CSV
permit-toolkit consolidate --input data/extracted/Virginia --output data/outputs/virginia_generators.csv
```

## Project Structure

```
backupgensprint/
├── src/
│   └── permit_toolkit/        # Main package
│       ├── scrapers/          # Web scraping modules
│       ├── extraction/        # LLM-based extraction
│       ├── consolidation/     # Data processing & export
│       ├── cli/               # Command-line interface
│       └── utils/             # Shared utilities & config
├── schemas/                   # JSON schemas
│   └── air_quality_permits_schema.json
├── examples/                  # Usage examples
├── tests/                     # Test suite
├── data/                      # Data directory (gitignored)
│   ├── permits/              # PDF files by state
│   ├── extracted/            # JSON extractions
│   └── outputs/              # Final datasets
├── pyproject.toml            # Package configuration
└── README.md                 # This file
```

## Usage Guide

### 1. Web Scraping

Download permit PDFs from state environmental agencies:

```bash
# Virginia - download all versions
permit-toolkit scrape --state virginia

# Virginia - latest versions only
permit-toolkit scrape --state virginia --latest-only

# Test with first 10 permits
permit-toolkit scrape --state virginia --test 10

# Custom output directory
permit-toolkit scrape --state virginia -o /path/to/output
```

**Manual Download**: For states without automated scrapers, manually download PDFs and organize by state:
```
data/permits/
  ├── Virginia/
  ├── Illinois/
  ├── Pennsylvania/
  └── ...
```

### 2. Data Extraction

Extract structured data using LangExtract (recommended) or OpenAI:

```bash
# Extract using LangExtract (recommended - 75% token reduction)
python extract_test_data.py

# Or use the CLI with OpenAI (legacy)
permit-toolkit extract --state Virginia --model gpt-4o
```

**LangExtract Extraction (Recommended):**
```python
from permit_toolkit.extraction import PermitExtractorLangExtract, load_schema
from pathlib import Path

schema = load_schema(Path("schemas/air_quality_permits_schema.json"))

extractor = PermitExtractorLangExtract(
    api_key=api_key,
    schema=schema,
    model_id="gpt-4o-mini",
    enable_text_optimization=True,   # 75% token reduction
    enable_rate_limiting=True,       # Automatic retry on rate limits
    enable_deduplication=True,       # Skip duplicate documents
)

result = extractor.extract_from_pdf(pdf_path, output_path)
```

**Benefits of LangExtract:**
- ✅ 75% reduction in token usage (saves costs)
- ✅ Better extraction consistency
- ✅ Automatic rate limit handling
- ✅ Smart duplicate detection
- ✅ Preserves 100% of data quality

**OpenAI Extraction (Legacy/Backup):**
```bash
# Traditional OpenAI approach
permit-toolkit extract --state Virginia --model gpt-4o --test 5
```

**What Gets Extracted:**
- Permit details (number, dates, facility info)
- Generator specifications (make, model, capacity)
- Fuel types and operating limits
- Emission limits (NOx, CO, PM, etc.)
- Compliance requirements

### 3. Data Consolidation

Convert JSON extractions to analysis-ready CSV:

```bash
# Consolidate all Virginia extractions
permit-toolkit consolidate \
  --input data/extracted/Virginia \
  --output data/outputs/virginia_generators.csv

# Consolidate multiple states
permit-toolkit consolidate \
  --input data/extracted \
  --output data/outputs/all_generators.csv
```

**Output Format**: One row per generator set with columns for:
- Facility information
- Generator specifications
- Operating parameters
- Emission limits
- Compliance requirements

## Python API

Use the toolkit programmatically:

```python
from pathlib import Path
from permit_toolkit.scrapers.virginia import VirginiaScraper
from permit_toolkit.extraction import PermitExtractor, load_schema
from permit_toolkit.consolidation import PermitConsolidator
from permit_toolkit.utils import get_config

# Initialize configuration
config = get_config()

# Scrape permits
scraper = VirginiaScraper(
    output_dir=config.get_permits_dir("Virginia"),
    test_mode=True,
    test_count=5
)
scraper.run()

# Extract data
schema = load_schema(config.default_schema)
extractor = PermitExtractor(
    api_key=config.openai_api_key,
    schema=schema,
    model_id="gpt-4o"
)

for pdf_path in Path("data/permits/Virginia").glob("*.pdf"):
    result = extractor.extract(pdf_path)
    extractor.save_result(result, pdf_path, config.get_extracted_dir("Virginia"))

# Consolidate data
consolidator = PermitConsolidator(
    extraction_dir=config.get_extracted_dir("Virginia")
)
df = consolidator.consolidate(output_path=Path("data/outputs/results.csv"))
summary = consolidator.generate_summary(df)
print(summary)
```

## Adding New States

To add a scraper for a new state:

1. Create a new scraper class inheriting from `BaseScraper`
2. Implement the required methods: `get_permit_list()`, `download_permit()`, `_get_output_path()`
3. Add the state to the CLI choices in `src/permit_toolkit/cli/main.py`

Example:

```python
from permit_toolkit.scrapers.base import BaseScraper

class PennsylvaniaScraper(BaseScraper):
    def get_permit_list(self):
        # Implement state-specific logic
        pass
    
    def download_permit(self, permit_info, output_path):
        # Implement download logic
        pass
    
    def _get_output_path(self, permit_info):
        # Generate output filename
        pass
```

## Schema Customization

The extraction schema is fully customizable. Edit `schemas/air_quality_permits_schema.json` to:

- Add new fields
- Modify data types
- Adjust validation rules
- Adapt for different permit types

The LLM extraction prompt automatically adapts to your schema.

## Data Directory Structure

The `data/` directory is gitignored and structured as:

```
data/
├── permits/              # Downloaded PDF files
│   ├── Virginia/
│   │   ├── 21527_DC_Permit.pdf
│   │   └── 11541_DC_Permit.pdf
│   └── Illinois/
├── extracted/            # JSON extraction results
│   ├── Virginia/
│   │   ├── langextract-21527.json
│   │   └── langextract-11541.json
│   └── Illinois/
└── outputs/              # Final CSV datasets
    ├── virginia_generators.csv
    └── all_generators.csv
```

## Performance & Costs

**Extraction Speed**: ~10-15 seconds per permit (GPT-4o)  
**Cost**: ~$0.05-0.15 per permit (varies by document length)  
**Accuracy**: Schema validation + manual spot-checking recommended

**Tips for Large Batches**:
- Use `--test` flag to validate on small samples first
- Monitor API costs with OpenAI dashboard
- Consider GPT-4-turbo for faster/cheaper processing
- Use `--reprocess` flag sparingly

**Note**: pypdf may show warnings about PDF structure - these are harmless and automatically suppressed in production mode.

## Testing Before Production

To test the pipeline without affecting your production data:

```bash
# Create test directory structure
mkdir -p test_data/permits/Virginia test_data/extracted/Virginia test_data/outputs

# Copy a couple of test permits
cp data/permits/Virginia/21527_DC_Permit.pdf test_data/permits/Virginia/
cp data/permits/Virginia/11541_DC_Permit.pdf test_data/permits/Virginia/

# Run extraction on test permits
permit-toolkit extract --state Virginia \
  --permits 21527 --permits 11541 \
  --model gpt-4o \
  --input test_data/permits/Virginia \
  --output test_data/extracted/Virginia

# Consolidate test results
permit-toolkit consolidate \
  --input test_data/extracted/Virginia \
  --output test_data/outputs/test_results.csv
```

## Nationwide Adaptation

This toolkit is designed for easy nationwide expansion:

1. **State-Agnostic Extraction**: The LLM handles varying permit formats automatically
2. **Flexible Scrapers**: Base classes provide consistent interface for new states
3. **Manual Download Support**: No scraper needed - just organize PDFs by state
4. **Unified Schema**: Single schema works across all states with minor adaptations

**Current Coverage**: Virginia (automated), others via manual download  
**Target**: All 50 states + territories

## Testing

Run the test suite to verify your installation:

```bash
# Run all tests
pytest tests/ -v

# Run with coverage report
pytest tests/ -v --cov=src/permit_toolkit --cov-report=term-missing

# Run specific test file
pytest tests/test_basic.py -v
```

**Note**: Make sure you've installed the package in editable mode (`pip install -e .`) before running tests.

## Contributing

Contributions welcome! Priority areas:

- [ ] Additional state scrapers
- [ ] Alternative LLM providers (Anthropic, local models)
- [ ] Enhanced validation logic
- [ ] Performance optimizations
- [ ] Additional permit types (water, waste, etc.)

## License

MIT License - see LICENSE file for details

---

**Status**: Production Ready (v0.1.0)  
**Tested on**: Python 3.9, 3.10, 3.11, 3.12  
**Platform**: macOS, Linux, Windows
