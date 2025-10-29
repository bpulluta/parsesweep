# Air Quality Permit Toolkit

> **Production-ready system for extracting structured data from air quality permits using advanced LLMs**

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Extract structured backup generator data from air quality permits across multiple states with **high accuracy**, **smart validation**, and **easy-to-use CLI**.

---

## ✨ Key Features

- **Advanced LLM Extraction** - OpenAI GPT-4/5 or Azure OpenAI with structured outputs
- **Multi-State Support** - Handles Virginia, Illinois, and other state permit formats  
- **Rich Data Output** - 46 fields per generator including emissions, capacity, fuel, monitoring
- **Smart Validation** - Built-in sanity checks and extraction notes for transparency
- **CSV Export** - One-command consolidation to analysis-ready datasets
- **⚡ Fast** - 10-80 seconds per permit, ~$0.003-0.013 per extraction

---

## 🚀 Quick Start (3 minutes)

### Prerequisites

- **Python 3.9-3.12** (Python 3.13 not yet supported)
- **API Access**: OpenAI API key OR Azure OpenAI credentials

### Installation

We use **pixi** for dependency management (fast, reproducible, cross-platform):

```bash
# 1. Install pixi (one-time setup)
curl -fsSL https://pixi.sh/install.sh | bash
# Windows: iwr -useb https://pixi.sh/install.ps1 | iex

# 2. Clone and setup
git clone https://github.com/NREL/backupgensprint.git
cd backupgensprint
pixi install  # Installs all dependencies (takes ~30 seconds)

# 3. Configure API (choose one)

# Option A: OpenAI API
echo "OPENAI_API_KEY=sk-your-key-here" > .env

# Option B: Azure OpenAI
cat > .env << 'EOF'
AZURE_OPENAI_API_KEY=your-azure-key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_VERSION=2024-02-15-preview
AZURE_OPENAI_MODEL=compassop-gpt-5
EOF
```

### First Extraction

```bash
# Extract a single permit
pixi run permit-toolkit extract validation/permits/11790_DC_Permit.pdf

# Extract all permits in a directory  
pixi run permit-toolkit extract validation/permits/ --use-azure

# Results appear in data/extracted/
```

---

## 📖 Usage Guide

### Extract Command

```bash
pixi run permit-toolkit extract <PATH> [OPTIONS]
```

**Common Examples:**

```bash
# Single file
pixi run permit-toolkit extract validation/permits/11790_DC_Permit.pdf

# Full directory
pixi run permit-toolkit extract validation/permits/

# With Azure OpenAI (recommended - higher rate limits)
pixi run permit-toolkit extract validation/permits/ --use-azure

# Custom output location
pixi run permit-toolkit extract validation/permits/ --output validation/aqtoolkit

# Test with first 5 files only
pixi run permit-toolkit extract data/permits/Illinois/ -n 5
```

### Consolidate Command

Convert extracted JSONs to analysis-ready CSV:

```bash
pixi run permit-toolkit consolidate <JSON_DIR> --output <CSV_FILE>
```

**Examples:**

```bash
# Consolidate validation data
pixi run permit-toolkit consolidate validation/aqtoolkit/ \
  --output validation/aqtoolkit/validation_consolidated.csv

# Consolidate all Virginia permits
pixi run permit-toolkit consolidate data/extracted/Virginia/ \
  --output data/outputs/virginia_generators.csv
```

---

## 📊 Output Format

### Extraction (JSON)

Each PDF produces a JSON with:
- **Metadata**: Processing time, cost, completeness score
- **Permit Details**: Number, dates, facility info, state
- **Generator Sets**: One entry per equipment reference

Key fields per generator:
- Equipment: make, model, capacity (BHP/kW)
- Fuel: type, sulfur content, normalized category
- **Emissions (reorganized for clarity)**:
  - Instant (lbs/hr): NOx, CO, VOC, PM, PM10, PM2.5, SO2 + aggregation type
  - Cumulative (tons/yr): NOx, CO, VOC, PM, PM10, PM2.5, SO2 + aggregation type
- Monitoring: hour meter, fuel flow meter, observation frequency
- Regulations: NSPS Subpart IIII, MACT Subpart ZZZZ
- **Extraction Notes**: Documents LLM decisions when alternatives exist

### Consolidated CSV

One row per generator with 58 columns including:
- All permit and facility details
- All equipment specifications
- All emissions limits (instant → aggregation type → cumulative → aggregation type)
- All monitoring and regulatory requirements
- Extraction notes for transparency

---

## 🏗️ Project Structure

```
backupgensprint/
├── schemas/
│   └── air_quality_permits_schema.json    # Extraction schema (46 fields)
├── src/permit_toolkit/
│   ├── extraction/                        # PDF → JSON extraction
│   ├── consolidation/                     # JSON → CSV consolidation
│   └── cli/                               # Command-line interface
├── validation/
│   ├── permits/                           # Test PDFs (7 permits)
│   └── aqtoolkit/                         # Ground truth data
│       ├── Virginia/                      # 5 Virginia permit JSONs
│       ├── Illinois/                      # 2 Illinois permit JSONs
│       └── validation_consolidated.csv    # Consolidated validation data
├── .env                                   # API credentials (create this)
├── pixi.toml                              # Dependency configuration
└── README.md                              # This file
```

---

## 🐛 Troubleshooting

### API Key Issues

```bash
# Verify .env file
cat .env
# Should show: OPENAI_API_KEY=sk-... OR AZURE_OPENAI_API_KEY=...

# Test with single file
pixi run permit-toolkit extract validation/permits/11790_DC_Permit.pdf --use-azure
```

### Rate Limits

```bash
# Use Azure OpenAI (much higher limits)
pixi run permit-toolkit extract <path> --use-azure

# Or process in smaller batches
pixi run permit-toolkit extract <path> -n 10
```

### Slow Extractions

```bash
# Use faster model
AZURE_OPENAI_MODEL=compassop-gpt-4.1-mini pixi run permit-toolkit extract <path> --use-azure
```

### Module Not Found

```bash
# Reinstall
pixi install

# Or use pixi shell
pixi shell
python -m pip install -e .
```

### Get Help

```bash
# Command-specific help
pixi run permit-toolkit extract --help
pixi run permit-toolkit consolidate --help
```

---

## 📈 Validation Results

Tested on 7 permits (Virginia + Illinois):

| Metric | Result |
|--------|--------|
| Total generators | 696 |
| Avg processing time | 81.9 sec/permit |
| Avg cost | $0.009/permit |
| Field completeness | 95%+ |
| Extraction notes | 59% of generators |

All validation data in `validation/aqtoolkit/`.

---

## 🤝 Team Usage

**For your teammates:**

1. **First time setup** (5 minutes):
   ```bash
   curl -fsSL https://pixi.sh/install.sh | bash
   git clone <repo>
   cd backupgensprint
   pixi install
   # Add .env file with API credentials
   ```

2. **Daily usage** (2 commands):
   ```bash
   # Extract
   pixi run permit-toolkit extract <your_pdfs>/ --use-azure
   
   # Consolidate
   pixi run permit-toolkit consolidate data/extracted/<state>/ --output results.csv
   ```

3. **If issues arise**:
   - Check `.env` file has credentials
   - Try `--use-azure` flag for higher rate limits
   - Use `-n 5` to test with small batch first
   - Run with `--help` to see all options

---

## 📄 License

MIT License - see [LICENSE](LICENSE) file.

---

## 🙏 Credits

Developed by NREL Buildings team for backup generator analysis.

For questions, open a GitHub issue.
