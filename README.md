# Air Quality Permit Toolkit

Production-ready extraction pipeline for structured data from air quality permits. Processes backup generator specifications at data centers and industrial facilities.

## Pipeline

1. **Scraping**: Download permit PDFs from state agencies
2. **Extraction**: LangExtract entity extraction with gpt-4o-mini (6.7x rate limit improvement)
3. **Consolidation**: JSON to analysis-ready CSV

## Architecture

- **LangExtract (Primary)**: Entity-based extraction, 200k TPM rate limit (gpt-4o-mini)
- **Rate Limiting**: Exponential backoff, automatic retry
- **Deduplication**: Hash-based document detection
- **QC Layer**: Conservative filtering (evidence-based, no inference)
- **Schema Validation**: Enforced JSON output structure

## Quality Control

**Evidence-Based Extraction**:
- Prompt specifies document structure (HEADER/BODY/TOP locations)
- Example uses fictitious data (prevents contamination)
- No inference for make/model/fuel (parse only what's stated)
- Conservative generator filtering (removes only empty entries)

**gpt-4o-mini Performance**:
- 200k TPM vs 30k TPM (gpt-4o) = 6.7x throughput
- Comparable quality with explicit location guidance in prompts
- Validated on Virginia permits (correct facility names, dates, specs)

## Installation

```bash
git clone https://github.com/NREL/backupgensprint.git
cd backupgensprint
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Create `.env`:
```
OPENAI_API_KEY=your_key
```

## Usage

### Scraping
```bash
permit-toolkit scrape --state virginia --test 10
```

### Extraction (LangExtract)
```bash
python generate_langextract_comparison.py
```

Configured for:
- Model: gpt-4o-mini (200k TPM)
- Rate limiting: enabled (exponential backoff)

### Consolidation
```bash
permit-toolkit consolidate --input data/extracted/Virginia --output data/outputs/virginia_generators.csv
```

## Implementation

**Core Extraction**:
- `extractor_langextract.py`: Entity extraction (primary)
- `rate_limiter.py`: Exponential backoff
- `pdf_utils.py`: Text extraction

**Prompt Design**:
```
PERMIT INFO - Extract from HEADER/TOP:
  Permit number: "Registration No." (digits only)
  Issue date: Date at VERY TOP (not superseded dates)
  Facility name: In BODY after "Dear" (not company)
  County: Near facility location phrase
```

**Example Strategy**: Fictitious data (Riverside Data Processing Facility, 98765) prevents real permit contamination.

**QC Layer**:
- No inference (evidence only)
- Conservative filtering (removes empty generators)
- Transparent logging
- Schema validation

## Structure

```
src/permit_toolkit/extraction/
├── extractor_langextract.py  # Primary
├── rate_limiter.py
├── text_optimizer.py
├── deduplicator.py
└── pdf_utils.py

data/
├── permits/Virginia/
├── comparison/Virginia/
│   ├── langextract/
│   ├── openai/
│   └── llamaextract/
└── outputs/

generate_langextract_comparison.py  # Main script
archive/                             # Deprecated code
```

## Production Validation

**Tested**: Virginia permits 11541, 11790, 41064, 51232, 52173
- ✓ Facility vs permittee name distinction
- ✓ Primary vs superseded date
- ✓ Evidence-based fuel type
- ✓ Multi-generator extraction

**Performance**: 200k TPM (gpt-4o-mini), 15s/permit avg, ~$0.03/permit
