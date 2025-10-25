# Air Quality Permit Toolkit

> **System for extracting structured data from air quality permits for backup generator analysis**

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Extract structured data from air quality permits using state-of-the-art LLMs with **cross-state compatibility**, **intelligent deduplication**, and **production-grade error handling**.

---

## ✨ Key Features

- **Hybrid LLM Extraction** - OpenAI for accuracy + optional LangExtract for traceability
- **Cross-State Support** - Handles Virginia, Illinois, and other state permit formats
- **Smart Deduplication** - Automatic detection of duplicate permits
- **Rich Structured Output** - 39+ fields per generator including emissions data
- **Production Ready** - Rate limiting, retry logic, comprehensive error handling
- **Modern CLI** - Beautiful terminal interface with progress tracking
- **Data Consolidation** - CSV/Excel export for analysis

---

## 🚀 Quick Start

### Installation

```bash
# Clone repository
git clone https://github.com/NREL/backupgensprint.git
cd backupgensprint

# Install package
pip install -e .

# Configure API key
echo "OPENAI_API_KEY=your-key-here" > .env
```

### Extract Single Permit

```bash
permit-toolkit extract data/permits/Virginia/11790_DC_Permit.pdf
```

### Extract Multiple Permits

```bash
# Extract first 10 Virginia permits
permit-toolkit extract data/permits/Virginia -n 10

# Extract all Illinois permits with GPT-4o
permit-toolkit extract data/permits/Illinois --model gpt-4o
```

### Consolidate to CSV

```bash
# Consolidate Virginia extractions
permit-toolkit consolidate data/extracted/Virginia -o virginia_dataset.csv

# Consolidate with Excel output
permit-toolkit consolidate data/extracted/Illinois --format excel
```


---

## 📋 CLI Commands

### `extract` - Extract Data from PDFs

```bash
permit-toolkit extract PATH [OPTIONS]
```

**Options:**
- `-o, --output PATH` - Output directory
- `--state TEXT` - State name (auto-detected)
- `--model TEXT` - `gpt-4o`, `gpt-4o-mini` (default)
- `--enable-qa-qc` - Enable QA/QC validation
- `-n, --limit INT` - Process first N files
- `--skip-existing/--reprocess` - Skip/reprocess existing

### `consolidate` - Convert JSON to CSV

```bash
permit-toolkit consolidate INPUT_DIR [OPTIONS]
```

**Options:**
- `-o, --output PATH` - Output file path
- `--state TEXT` - Filter by state
- `--format [csv|excel|json]` - Output format

---

## 📊 Output Format

### JSON Structure

Each extraction produces structured JSON with:
- **Metadata**: source_file, extraction_date, state, model, cost_usd, processing_time_sec
- **Permit Details**: permit_number, facility_name, facility_address, etc.
- **Generator Sets**: 28 fields per generator including emissions data

### CSV Consolidation

39 columns per generator set including:
- Permit info (facility, dates, location)
- Generator specs (make, model, capacity)
- Fuel data (type, sulfur content, throughput)
- Emissions limits (NOx, CO, VOC, PM, SO2)
- Operating parameters

---

## 🏗️ Architecture

```
PDF → Text Extraction → OpenAI GPT-4o → Structured JSON → CSV Export
         (PyMuPDF4LLM)    (~5s, $0.0016)                  (Analysis)
                            ↓
                      [Optional QA/QC]
                       (LangExtract)
```

---

## 📈 Performance

| Metric | Value |
|--------|-------|
| **Speed** | 5-15 sec/permit |
| **Cost (gpt-4o-mini)** | $0.0008-0.0016/permit |
| **Cost (gpt-4o)** | $0.008-0.016/permit |
| **States** | Virginia, Illinois (extensible) |

---

## Project Structure

```
src/permit_toolkit/
├── cli/                    # Command-line interface
├── extraction/             # PDF extraction & LLM processing
├── consolidation/          # Data aggregation to CSV
├── scrapers/              # Web scraping utilities
└── utils/                 # Configuration & logging
```