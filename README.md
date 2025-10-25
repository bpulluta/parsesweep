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

### Prerequisites

- **Python 3.9+** (Python 3.9, 3.10, 3.11, or 3.12)
- OpenAI API key

### Installation with pixi (⭐ Recommended)

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

# 4. Configure API key
echo "OPENAI_API_KEY=your-key-here" > .env

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

# Install package
pip install -e .

# Configure API key
echo "OPENAI_API_KEY=your-key-here" > .env

# Verify installation
permit-toolkit --help
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

---

## 🔧 Requirements

### Core Dependencies
- Python 3.9+ (tested with 3.9, 3.10, 3.11, 3.12, 3.13)
- pandas >= 2.0.0
- numpy >= 1.24.0
- openai >= 1.0.0
- langextract >= 1.0.0
- pymupdf4llm >= 0.0.5
- click >= 8.1.0
- All others listed in `requirements.txt`, `pyproject.toml`, and `pixi.toml`

### Environment Variables
```bash
OPENAI_API_KEY=sk-your-key-here  # Required
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