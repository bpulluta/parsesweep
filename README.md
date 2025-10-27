# Air Quality Permit Toolkit

> **System for extracting structured data from air quality permits for backup generator analysis**

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Extract structured data from air quality permits using state-of-the-art LLMs with **cross-state compatibility**, **intelligent deduplication**, and **error handling**.

---

## ✨ Key Features

- **Hybrid LLM Extraction** - OpenAI for accuracy + optional LangExtract for traceability
- **Cross-State Support** - Handles Virginia, Illinois, and other state permit formats
- **Smart Deduplication** - Automatic detection of duplicate permits
- **Rich Structured Output** - 39+ fields per generator including emissions data
- **Data Consolidation** - CSV/Excel export for analysis
- **Interactive Maps** - Geocoded facility visualization with generator counts

---

## 🚀 Quick Start

### Prerequisites

- **Python 3.9+** (Python 3.9, 3.10, 3.11, or 3.12)
- **API Access**: Either OpenAI API key OR Azure OpenAI service

### Installation with pixi (Recommended)

**Why pixi?** Fast binary installs, reproducible environments, zero configuration, works across all platforms.

```bash
# 1. Install pixi (one-time setup)
curl -fsSL https://pixi.sh/install.sh | bash
# Or on Windows: iwr -useb https://pixi.sh/install.ps1 | iex

# 2. Clone repository
git clone https://github.com/NREL/backupgensprint.git
cd backupgensprint

# 3. Install dependencies (automatic, takes ~30 seconds)
pixi install

# 4. Configure API credentials

# Option A: OpenAI API
echo "OPENAI_API_KEY=your-key-here" > .env

# Option B: Azure OpenAI
echo "AZURE_OPENAI_API_KEY=your-azure-key" > .env
echo "AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/" >> .env
echo "AZURE_OPENAI_API_VERSION=2025-04-01-preview" >> .env

# 5. Verify installation
pixi run permit-toolkit --help
```

**That's it!** No virtual environments, no version conflicts, just works.

**Using pixi commands:**
```bash
# Run commands with 'pixi run' prefix
pixi run permit-toolkit extract data/permits/Virginia/sample.pdf
pixi run permit-toolkit consolidate data/extracted/Virginia

# Or enter pixi shell (no prefix needed)
pixi shell
permit-toolkit extract data/permits/Virginia/sample.pdf
exit
```

### Alternative: pip Installation

If you prefer traditional Python tools:

```bash
# Clone and enter directory
git clone https://github.com/NREL/backupgensprint.git
cd backupgensprint

# Create virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install package with all dependencies
pip install -e .

# Configure API credentials

# Option A: OpenAI API
echo "OPENAI_API_KEY=your-key-here" > .env

# Option B: Azure OpenAI
echo "AZURE_OPENAI_API_KEY=your-azure-key" > .env
echo "AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/" >> .env
echo "AZURE_OPENAI_API_VERSION=2025-04-01-preview" >> .env

# Verify installation
permit-toolkit --help
```

### Extract Single Permit

```bash
# Using OpenAI
permit-toolkit extract data/permits/Virginia/11790_DC_Permit.pdf

# Using Azure OpenAI  
permit-toolkit extract data/permits/Virginia/11790_DC_Permit.pdf --use-azure
```

### Extract Multiple Permits

```bash
# Extract first 10 Virginia permits (OpenAI)
permit-toolkit extract data/permits/Virginia -n 10

# Extract all Illinois permits with GPT-4o via Azure
permit-toolkit extract data/permits/Illinois --model gpt-4o --use-azure
```

### Consolidate to CSV

```bash
# Consolidate Virginia extractions
permit-toolkit consolidate data/extracted/Virginia -o virginia_dataset.csv

# Consolidate with Excel output
permit-toolkit consolidate data/extracted/Illinois --format excel
```

### Visualize on Map

```bash
# Map single state facilities
permit-toolkit map data/extracted --state Virginia

# Map multiple states
permit-toolkit map data/extracted --state "Virginia,Maryland,Ohio"

# Map all available states
permit-toolkit map data/extracted --all-states
```

---

## Azure OpenAI Setup

For users with Azure OpenAI deployments, the toolkit supports Azure as an alternative to OpenAI API with typically higher rate limits and enhanced security.

### Prerequisites for Azure

1. **Azure OpenAI Service** deployed with model access (gpt-4o, gpt-4o-mini, etc.)
2. **API credentials** from your Azure portal

### Configuration

1. **Get your Azure credentials** from the Azure portal:
   ```bash
   # Your Azure OpenAI resource endpoint
   AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"
   
   # Your API key from Azure portal > Keys and Endpoint
   AZURE_OPENAI_API_KEY="your-32-character-key"
   
   # API version (optional, defaults to 2025-04-01-preview)
   AZURE_OPENAI_API_VERSION="2025-04-01-preview"
   
   # Deployment name (optional, uses your Azure model deployment name)
   AZURE_OPENAI_MODEL="your-deployment-name"
   ```

2. **Set environment variables**:
   ```bash
   # Create .env file with Azure credentials
   cat > .env << EOF
   AZURE_OPENAI_API_KEY=your-azure-key
   AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
   AZURE_OPENAI_API_VERSION=2025-04-01-preview
   AZURE_OPENAI_MODEL=your-deployment-name
   EOF
   ```

3. **Verify Azure connection**:
   ```bash
   # Test with a single permit
   pixi run permit-toolkit extract data/permits/Virginia/sample.pdf --use-azure --model gpt-4o
   ```

### Azure Usage Examples

```bash
# Load environment variables from .env file
export $(cat .env | grep -v '^#' | xargs)

# Extract with Azure OpenAI (uses AZURE_OPENAI_MODEL from .env)
permit-toolkit extract data/permits/Virginia --use-azure

# Override model with --model flag (ignores AZURE_OPENAI_MODEL)
permit-toolkit extract data/permits/Illinois --use-azure --model compassop-gpt-4o

# Batch processing with Azure (higher rate limits)
permit-toolkit extract data/permits/Virginia --use-azure -n 50
```

**Changing Azure Models:**

You have three options to select which Azure model deployment to use:

1. **Set in .env (recommended)** - Edit `AZURE_OPENAI_MODEL` in your `.env` file:
   ```bash
   AZURE_OPENAI_MODEL=compassop-gpt-4.1-mini  # Change this line
   ```

2. **Use --model flag** - Override on command line:
   ```bash
   permit-toolkit extract file.pdf --use-azure --model compassop-gpt-5
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
- `--model TEXT` - Model selection: `gpt-4o`, `gpt-4o-mini` (default)
- `--use-azure` - Use Azure OpenAI
- `--enable-qa-qc` - Enable QA/QC validation
- `-n, --limit INT` - Process first N files
- `--skip-existing/--reprocess` - Skip/reprocess existing files

### `consolidate` - Convert JSON to Datasets

```bash
permit-toolkit consolidate INPUT_DIR [OPTIONS]
```

**Options:**
- `-o, --output PATH` - Output file path
- `--state TEXT` - Filter by state
- `--format [csv|excel|json]` - Output format (default: csv)

### `map` - Visualize Facilities

```bash
permit-toolkit map INPUT_DIR [OPTIONS]
```

**Options:**
- `-o, --output PATH` - Output HTML file path
- `--state TEXT` - State name(s) to map (comma-separated)
- `--all-states` - Map all available states
- `-n, --limit INT` - Limit facilities per state
- `--title TEXT` - Custom map title
- `--no-cache` - Force fresh geocoding

### `validate` - Validate Extraction

```bash
permit-toolkit validate EXTRACTION_FILE [OPTIONS]
```

**Options:**
- `-v, --verbose` - Enable verbose logging

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
                          ┌─────────────────┐
                          │   PDF Permits   │
                          └────────┬────────┘
                                   │
                          ┌────────▼────────┐
                          │ Text Extraction │
                          │  (PyMuPDF4LLM)  │
                          └────────┬────────┘
                                   │
                  ┌────────────────┼────────────────┐
                  │                                 │
         ┌────────▼────────┐              ┌────────▼────────┐
         │  OpenAI GPT-4o  │              │  Azure OpenAI   │
         │  (~5s, $0.0016) │              │  (Enterprise)   │
         └────────┬────────┘              └────────┬────────┘
                  │                                 │
                  └────────────────┬────────────────┘
                                   │
                                   │  (Optional: --enable-qa-qc)
                                   │  ┌──────────────────┐
                                   ├─▶│  LangExtract     │
                                   │  │  Comparison      │
                                   │  └──────────────────┘
                                   │
                          ┌────────▼────────┐
                          │ Structured JSON │
                          └────────┬────────┘
                                   │
                  ┌────────────────┼────────────────┐
                  │                                 │
         ┌────────▼────────┐              ┌────────▼────────┐
         │  CSV/Excel      │              │  HTML Map       │
         │  Consolidation  │              │  Visualization  │
         └─────────────────┘              └─────────────────┘
```

**Pipeline:**
1. `extract` - PDFs to structured JSON (OpenAI or Azure)
2. `consolidate` - JSON to CSV/Excel datasets
3. `map` - JSON to interactive HTML maps
4. `validate` - Quality assurance checks

---

## 📈 Performance

| Metric | Value |
|--------|-------|
| **Speed** | 5-15 sec/permit |
| **Cost (gpt-4o-mini)** | $0.0008-0.0016/permit |
| **Cost (gpt-4o)** | $0.008-0.016/permit |
| **States** | Virginia, Illinois (extensible) |

---

## 📂 Project Structure

```
src/permit_toolkit/
├── cli/                    # Command-line interface
├── extraction/             # PDF extraction & LLM processing
├── consolidation/          # Data export (CSV/Excel)
├── visualization/          # Map generation (HTML)
├── scrapers/              # Web scraping utilities
└── utils/                 # Configuration & logging
```

---

## 🔧 Requirements

### Core Dependencies
- Python 3.9+ (tested with 3.9, 3.10, 3.11, 3.12, 3.13)
- pandas >= 2.0.0
- openai >= 1.0.0
- langextract >= 1.0.0
- pymupdf4llm >= 0.0.5
- click >= 8.1.0
- folium >= 0.15.0
- geopy >= 2.4.0
- openpyxl >= 3.1.0

See `requirements.txt`, `pyproject.toml`, or `pixi.toml` for complete list.

### Environment Variables

**OpenAI API (default):**
```bash
OPENAI_API_KEY=sk-your-key-here  # Required
```

**Azure OpenAI (alternative):**
```bash
AZURE_OPENAI_API_KEY=your-azure-key      # Required
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/  # Required
AZURE_OPENAI_API_VERSION=2025-04-01-preview  # Optional (default shown)
AZURE_OPENAI_MODEL=your-deployment-name  # Optional (e.g., compassop-gpt-4.1-mini)
```

---

## 🐛 Troubleshooting

### Installation Issues

**Issue: `permit-toolkit: command not found`**
```bash
# For pip: Ensure virtual environment is activated
source .venv/bin/activate

# For pixi: Use 'pixi run' prefix
pixi run permit-toolkit --help

# Or reinstall
pip install -e .  # pip
pixi install      # pixi
```

**Issue: `No module named 'permit_toolkit'`**
```bash
# Ensure you installed with -e flag
pip install -e .

# Not: pip install -r requirements.txt
```

**Issue: Python version error**
```bash
# Check version
python --version

# Must be >= 3.9
# Install newer Python if needed
```

### Runtime Issues

**Issue: OpenAI API errors**
```bash
# Check API key is set
echo $OPENAI_API_KEY

# Set it if empty
export OPENAI_API_KEY="sk-your-key-here"

# Or load from .env
export $(cat .env | grep -v '^#' | xargs)
```

**Issue: Azure OpenAI errors**
```bash
# Check Azure credentials are set
echo $AZURE_OPENAI_API_KEY
echo $AZURE_OPENAI_ENDPOINT

# Set them if empty
export AZURE_OPENAI_API_KEY="your-azure-key"
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"

# Test Azure connection
permit-toolkit extract sample.pdf --use-azure --model gpt-4o-mini
```

**Issue: Geocoding failures**
```bash
# Requires internet access to OpenStreetMap
curl https://nominatim.openstreetmap.org/

# Corporate networks may need proxy configuration
```

**Issue: PDF extraction fails**
```bash
# Check PDF file exists and is readable
ls -lh data/permits/Virginia/your_file.pdf

# Try with verbose logging
pixi run permit-toolkit extract your_file.pdf --model gpt-4o-mini
```

**Issue: Pixi installation slow or fails**
```bash
# Clean cache and retry
pixi clean
pixi install

# Check your platform is supported
uname -a  # Should be: macOS (Intel/ARM), Linux (x64), or Windows (x64)
```

---

## 📦 Dependency Management

This project supports **both pip and pixi** installation methods:

| Method | Files | Best For |
|--------|-------|----------|
| **pip** | `requirements.txt`, `pyproject.toml` | Development, CI/CD |
| **pixi** | `pixi.toml` | Production, reproducible environments |

All dependency files are synchronized to ensure consistency.

**Adding new dependencies:**
1. Add to `requirements.txt` and `pyproject.toml`
2. Add to `pixi.toml` ([dependencies] or [pypi-dependencies])
3. Test both: `pip install -e .` and `pixi install`

---

## 🤝 Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Install dev dependencies: `pip install -e ".[dev]"` or use pixi dev environment
4. Run tests: `pytest`
5. Format code: `black src/` and `ruff check src/`
6. Submit a pull request

---

## 📄 License

MIT License - See [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

Developed by the NREL team for air quality permit analysis and backup generator data collection.

---

## 📞 Support

- **Issues**: https://github.com/NREL/backupgensprint/issues
- **Documentation**: This README
- **API Costs**: Monitor usage at https://platform.openai.com/usage