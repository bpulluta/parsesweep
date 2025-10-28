# Air Quality Permit Toolkit# Air Quality Permit Toolkit



> **Professional toolkit for extracting structured data from air quality permits using advanced LLMs**> **System for extracting structured data from air quality permits for backup generator analysis**



[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)



A production-ready system for extracting and analyzing backup generator data from air quality permits across multiple states. Built for accuracy, scalability, and ease of use.Extract structured data from air quality permits using state-of-the-art LLMs with **cross-state compatibility**, **intelligent deduplication**, and **error handling**.



------



## ✨ Key Features## ✨ Key Features



- **🤖 Hybrid LLM Extraction** - OpenAI GPT-4o/GPT-5 for accuracy + optional LangExtract for source traceability- **Hybrid LLM Extraction** - OpenAI for accuracy + optional LangExtract for traceability

- **🗺️ Multi-State Support** - Handles Virginia, Illinois, and other state permit formats seamlessly- **Cross-State Support** - Handles Virginia, Illinois, and other state permit formats

- **🔍 Smart Deduplication** - Automatic detection and handling of duplicate permits- **Smart Deduplication** - Automatic detection of duplicate permits

- **📊 Rich Structured Output** - Extracts 39+ fields per generator including detailed emissions data- **Rich Structured Output** - 39+ fields per generator including emissions data

- **📈 Data Consolidation** - Export to CSV/Excel for downstream analysis and reporting- **Data Consolidation** - CSV/Excel export for analysis

- **⚡ Fast & Cost-Effective** - 5-15 seconds per permit, ~$0.001-0.016 per extraction

- **🔒 Enterprise Ready** - Supports both OpenAI API and Azure OpenAI for enhanced security---



---## 🚀 Quick Start



## 📋 Table of Contents### Prerequisites



- [Quick Start](#-quick-start)- **Python 3.9+** (Python 3.9, 3.10, 3.11, or 3.12)

- [Installation](#-installation)- **API Access**: Either OpenAI API key OR Azure OpenAI service

- [Usage](#-usage)

- [Azure OpenAI Setup](#-azure-openai-setup)### Installation with pixi (Recommended)

- [CLI Reference](#-cli-reference)

- [Output Format](#-output-format)**Why pixi?** Fast binary installs, reproducible environments, zero configuration, works across all platforms.

- [Architecture](#%EF%B8%8F-architecture)

- [Project Structure](#-project-structure)```bash

- [Troubleshooting](#-troubleshooting)# 1. Install pixi (one-time setup)

- [Contributing](#-contributing)curl -fsSL https://pixi.sh/install.sh | bash

# Or on Windows: iwr -useb https://pixi.sh/install.ps1 | iex

---

# 2. Clone repository

## 🚀 Quick Startgit clone https://github.com/NREL/backupgensprint.git

cd backupgensprint

### Prerequisites

# 3. Install dependencies (automatic, takes ~30 seconds)

- **Python 3.9+** (tested with 3.9, 3.10, 3.11, 3.12, 3.13)pixi install

- **API Access**: OpenAI API key OR Azure OpenAI service credentials

# 4. Configure API credentials

### 3-Minute Setup

# Option A: OpenAI API

```bashecho "OPENAI_API_KEY=your-key-here" > .env

# 1. Clone repository

git clone https://github.com/NREL/backupgensprint.git# Option B: Azure OpenAI

cd backupgensprintecho "AZURE_OPENAI_API_KEY=your-azure-key" > .env

echo "AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/" >> .env

# 2. Install with pixi (recommended - fastest, most reliable)echo "AZURE_OPENAI_API_VERSION=2025-04-01-preview" >> .env

curl -fsSL https://pixi.sh/install.sh | bash  # One-time install

pixi install  # ~30 seconds# 5. Verify installation

pixi run permit-toolkit --help

# 3. Configure API key```

echo "OPENAI_API_KEY=sk-your-key-here" > .env

**That's it!** No virtual environments, no version conflicts, just works.

# 4. Extract your first permit

pixi run permit-toolkit extract data/permits/sample.pdf**Using pixi commands:**

``````bash

# Run commands with 'pixi run' prefix

**That's it!** Your structured data is now in `data/extracted/`pixi run permit-toolkit extract data/permits/Virginia/sample.pdf

pixi run permit-toolkit consolidate data/extracted/Virginia

---

# Or enter pixi shell (no prefix needed)

## 💻 Installationpixi shell

permit-toolkit extract data/permits/Virginia/sample.pdf

### Option 1: pixi (Recommended)exit

```

**Why pixi?** Fast binary installs, reproducible environments, zero configuration, cross-platform.

### Alternative: pip Installation

```bash

# Install pixi (one-time setup)If you prefer traditional Python tools:

curl -fsSL https://pixi.sh/install.sh | bash

# Windows: iwr -useb https://pixi.sh/install.ps1 | iex```bash

# Clone and enter directory

# Install dependencies (automatic)git clone https://github.com/NREL/backupgensprint.git

pixi installcd backupgensprint



# Verify installation# Create virtual environment (recommended)

pixi run permit-toolkit --helppython -m venv .venv

```source .venv/bin/activate  # Windows: .venv\Scripts\activate



**Using pixi:**# Install package

```bashpip install -e .

# Run commands with 'pixi run' prefix

pixi run permit-toolkit extract file.pdf# Configure API credentials



# Or enter pixi shell (no prefix needed)# Option A: OpenAI API

pixi shellecho "OPENAI_API_KEY=your-key-here" > .env

permit-toolkit extract file.pdf

exit# Option B: Azure OpenAI

```echo "AZURE_OPENAI_API_KEY=your-azure-key" > .env

echo "AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/" >> .env

### Option 2: pip (Traditional)echo "AZURE_OPENAI_API_VERSION=2025-04-01-preview" >> .env



```bash# Verify installation

# Create virtual environment (recommended)permit-toolkit --help

python -m venv .venv```

source .venv/bin/activate  # Windows: .venv\Scripts\activate

### Extract Single Permit

# Install package in editable mode

pip install -e .```bash

# Using OpenAI

# Verify installationpermit-toolkit extract data/permits/Virginia/11790_DC_Permit.pdf

permit-toolkit --help

```# Using Azure OpenAI  

permit-toolkit extract data/permits/Virginia/11790_DC_Permit.pdf --use-azure

---```



## 🔧 Configuration### Extract Multiple Permits



### OpenAI API (Default)```bash

# Extract first 10 Virginia permits (OpenAI)

Create a `.env` file in the project root:permit-toolkit extract data/permits/Virginia -n 10



```bash# Extract all Illinois permits with GPT-4o via Azure

# Requiredpermit-toolkit extract data/permits/Illinois --model gpt-4o --use-azure

OPENAI_API_KEY=sk-your-key-here```

```

### Consolidate to CSV

Get your API key from [OpenAI Platform](https://platform.openai.com/api-keys).

```bash

### Azure OpenAI (Optional)# Consolidate Virginia extractions

permit-toolkit consolidate data/extracted/Virginia -o virginia_dataset.csv

For enterprise deployments with enhanced security and higher rate limits:

# Consolidate with Excel output

```bashpermit-toolkit consolidate data/extracted/Illinois --format excel

# Required```

AZURE_OPENAI_API_KEY=your-32-character-key

AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/---



# Optional (with defaults shown)## 🔷 Azure OpenAI Setup

AZURE_OPENAI_API_VERSION=2025-04-01-preview

AZURE_OPENAI_MODEL=your-deployment-name  # e.g., compassop-gpt-5For users with Azure OpenAI deployments, the toolkit supports Azure as an alternative to OpenAI API with typically higher rate limits and enhanced security.

```

### Prerequisites for Azure

See [Azure OpenAI Setup](#-azure-openai-setup) for detailed configuration.

1. **Azure OpenAI Service** deployed with model access (gpt-4o, gpt-4o-mini, etc.)

---2. **API credentials** from your Azure portal



## 📖 Usage### Configuration



### Extract Single Permit1. **Get your Azure credentials** from the Azure portal:

   ```bash

```bash   # Your Azure OpenAI resource endpoint

# Using OpenAI API   AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"

permit-toolkit extract data/permits/Virginia/11790_DC_Permit.pdf   

   # Your API key from Azure portal > Keys and Endpoint

# Using Azure OpenAI (with .env configured)   AZURE_OPENAI_API_KEY="your-32-character-key"

permit-toolkit extract data/permits/Virginia/11790_DC_Permit.pdf --use-azure   

   # API version (optional, defaults to 2025-04-01-preview)

# Specify output directory   AZURE_OPENAI_API_VERSION="2025-04-01-preview"

permit-toolkit extract permit.pdf --output my_extractions/   

```   # Deployment name (optional, uses your Azure model deployment name)

   AZURE_OPENAI_MODEL="your-deployment-name"

### Extract Multiple Permits   ```



```bash2. **Set environment variables**:

# Extract all permits in a directory   ```bash

permit-toolkit extract data/permits/Virginia/   # Create .env file with Azure credentials

   cat > .env << EOF

# Extract first 10 permits   AZURE_OPENAI_API_KEY=your-azure-key

permit-toolkit extract data/permits/Virginia/ -n 10   AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/

   AZURE_OPENAI_API_VERSION=2025-04-01-preview

# Skip already processed files (default)   AZURE_OPENAI_MODEL=your-deployment-name

permit-toolkit extract data/permits/Virginia/ --skip-existing   EOF

   ```

# Reprocess all files

permit-toolkit extract data/permits/Virginia/ --reprocess3. **Verify Azure connection**:

```   ```bash

   # Test with a single permit

### Consolidate to Dataset   pixi run permit-toolkit extract data/permits/Virginia/sample.pdf --use-azure --model gpt-4o

   ```

```bash

# Convert JSON extractions to CSV### Azure Usage Examples

permit-toolkit consolidate data/extracted/Virginia/ -o virginia_dataset.csv

```bash

# Export to Excel# Load environment variables from .env file

permit-toolkit consolidate data/extracted/Illinois/ -o illinois.xlsx --format excelexport $(cat .env | grep -v '^#' | xargs)



# Filter by state# Extract with Azure OpenAI (uses AZURE_OPENAI_MODEL from .env)

permit-toolkit consolidate data/extracted/ --state Virginia -o va_only.csvpermit-toolkit extract data/permits/Virginia --use-azure

```

# Override model with --model flag (ignores AZURE_OPENAI_MODEL)

### Advanced Optionspermit-toolkit extract data/permits/Illinois --use-azure --model compassop-gpt-4o



```bash# Batch processing with Azure (higher rate limits)

# Use GPT-4o for higher accuracy (higher cost)permit-toolkit extract data/permits/Virginia --use-azure -n 50

permit-toolkit extract permit.pdf --model gpt-4o```



# Enable QA/QC validation with LangExtract (adds source citations)**Changing Azure Models:**

permit-toolkit extract permit.pdf --enable-qa-qc

You have three options to select which Azure model deployment to use:

# Use Azure with specific model

permit-toolkit extract permit.pdf --use-azure --model compassop-gpt-51. **Set in .env (recommended)** - Edit `AZURE_OPENAI_MODEL` in your `.env` file:

```   ```bash

   AZURE_OPENAI_MODEL=compassop-gpt-4.1-mini  # Change this line

---   ```



## 🔷 Azure OpenAI Setup2. **Use --model flag** - Override on command line:

   ```bash

Azure OpenAI provides enterprise-grade security, higher rate limits, and data residency controls.   permit-toolkit extract file.pdf --use-azure --model compassop-gpt-5

   ```

### Prerequisites

---

1. **Azure OpenAI Service** deployed in your Azure subscription

2. **Model Deployment** (gpt-4o, gpt-4o-mini, gpt-4.1, gpt-5, etc.)## 📋 CLI Commands

3. **API Credentials** from Azure Portal

### `extract` - Extract Data from PDFs

### Step-by-Step Configuration

```bash

#### 1. Get Azure Credentialspermit-toolkit extract PATH [OPTIONS]

```

Navigate to your Azure OpenAI resource in the Azure Portal:

**Options:**

- **Endpoint**: `Keys and Endpoint` → `Endpoint` (e.g., `https://your-resource.openai.azure.com/`)- `-o, --output PATH` - Output directory

- **API Key**: `Keys and Endpoint` → `KEY 1` or `KEY 2`- `--state TEXT` - State name (auto-detected)

- **Deployment Name**: `Model deployments` → Your deployment name (e.g., `compassop-gpt-5`)- `--model TEXT` - `gpt-4o`, `gpt-4o-mini` (default)

- `--use-azure` - Use Azure OpenAI instead of OpenAI API

#### 2. Configure Environment Variables- `--enable-qa-qc` - Enable QA/QC validation

- `-n, --limit INT` - Process first N files

Create or update your `.env` file:- `--skip-existing/--reprocess` - Skip/reprocess existing



```bash### `consolidate` - Convert JSON to CSV

cat > .env << EOF

# Azure OpenAI Configuration```bash

AZURE_OPENAI_API_KEY=your-32-character-azure-keypermit-toolkit consolidate INPUT_DIR [OPTIONS]

AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/```

AZURE_OPENAI_API_VERSION=2025-04-01-preview

AZURE_OPENAI_MODEL=your-deployment-name**Options:**

EOF- `-o, --output PATH` - Output file path

```- `--state TEXT` - Filter by state

- `--format [csv|excel|json]` - Output format

#### 3. Verify Connection

---

```bash

# Test with a sample permit## 📊 Output Format

pixi run permit-toolkit extract data/permits/sample.pdf --use-azure

```### JSON Structure



### Usage ExamplesEach extraction produces structured JSON with:

- **Metadata**: source_file, extraction_date, state, model, cost_usd, processing_time_sec

```bash- **Permit Details**: permit_number, facility_name, facility_address, etc.

# Use Azure with model from .env- **Generator Sets**: 28 fields per generator including emissions data

permit-toolkit extract data/permits/Virginia/ --use-azure

### CSV Consolidation

# Override model deployment

permit-toolkit extract permit.pdf --use-azure --model compassop-gpt-4o39 columns per generator set including:

- Permit info (facility, dates, location)

# Batch processing with Azure (higher rate limits)- Generator specs (make, model, capacity)

permit-toolkit extract data/permits/Virginia/ --use-azure -n 50- Fuel data (type, sulfur content, throughput)

```- Emissions limits (NOx, CO, VOC, PM, SO2)

- Operating parameters

### Available Azure Models

---

Depending on your Azure deployment, you may have access to:

## 🏗️ Architecture

| Model | Deployment Name Example | Best For |

|-------|------------------------|----------|```

| GPT-4.1 | `compassop-gpt-4.1` | High accuracy |PDF → Text Extraction → OpenAI GPT-4o / Azure OpenAI → Structured JSON → CSV Export

| GPT-4.1 Mini | `compassop-gpt-4.1-mini` | Balanced performance |         (PyMuPDF4LLM)        (~5s, $0.0016)                              (Analysis)

| GPT-4o | `compassop-gpt-4o` | Optimized for speed |                                       ↓

| GPT-5 | `compassop-gpt-5` | Latest, most capable |                                 [Optional QA/QC]

| GPT-5 Mini | `compassop-gpt-5-mini` | Fast, cost-effective |                                  (LangExtract)

```

**To change models:**

---

1. **In .env (recommended)** - Edit `AZURE_OPENAI_MODEL`:

   ```bash## 📈 Performance

   AZURE_OPENAI_MODEL=compassop-gpt-5

   ```| Metric | Value |

|--------|-------|

2. **Via CLI flag** - Override for specific runs:| **Speed** | 5-15 sec/permit |

   ```bash| **Cost (gpt-4o-mini)** | $0.0008-0.0016/permit |

   permit-toolkit extract file.pdf --use-azure --model compassop-gpt-4.1-mini| **Cost (gpt-4o)** | $0.008-0.016/permit |

   ```| **States** | Virginia, Illinois (extensible) |



------



## 📋 CLI Reference## Project Structure



### `extract` - Extract Data from Permits```

src/permit_toolkit/

```bash├── cli/                    # Command-line interface

permit-toolkit extract PATH [OPTIONS]├── extraction/             # PDF extraction & LLM processing

```├── consolidation/          # Data aggregation to CSV

├── scrapers/              # Web scraping utilities

**Arguments:**└── utils/                 # Configuration & logging

- `PATH` - Single PDF file or directory of PDFs```



**Options:**---

| Option | Type | Default | Description |

|--------|------|---------|-------------|## 🔧 Requirements

| `-o, --output` | PATH | `data/extracted/{state}` | Output directory for JSON files |

| `--state` | TEXT | Auto-detected | State name (Virginia, Illinois, etc.) |### Core Dependencies

| `--model` | TEXT | `gpt-4o-mini` | Model to use (`gpt-4o`, `gpt-4o-mini`, etc.) |- Python 3.9+ (tested with 3.9, 3.10, 3.11, 3.12, 3.13)

| `--use-azure` | FLAG | False | Use Azure OpenAI instead of OpenAI API |- pandas >= 2.0.0

| `--enable-qa-qc` | FLAG | False | Enable LangExtract QA/QC validation |- numpy >= 1.24.0

| `-n, --limit` | INT | None | Process only first N files (directory mode) |- openai >= 1.0.0

| `--skip-existing` | FLAG | True | Skip already processed files |- langextract >= 1.0.0

| `--reprocess` | FLAG | False | Reprocess all files, ignoring existing |- pymupdf4llm >= 0.0.5

- click >= 8.1.0

**Examples:**- All others listed in `requirements.txt`, `pyproject.toml`, and `pixi.toml`

```bash

# Basic extraction### Environment Variables

permit-toolkit extract data/permits/Virginia/permit.pdf

**OpenAI API (default):**

# Azure with custom model```bash

permit-toolkit extract data/permits/ --use-azure --model compassop-gpt-5OPENAI_API_KEY=sk-your-key-here  # Required

```

# Batch processing with limit

permit-toolkit extract data/permits/Illinois/ -n 20 --skip-existing**Azure OpenAI (alternative):**

``````bash

AZURE_OPENAI_API_KEY=your-azure-key      # Required

### `consolidate` - Convert JSON to Tabular FormatAZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/  # Required

AZURE_OPENAI_API_VERSION=2025-04-01-preview  # Optional (default shown)

```bashAZURE_OPENAI_MODEL=your-deployment-name  # Optional (e.g., compassop-gpt-4.1-mini)

permit-toolkit consolidate INPUT_DIR [OPTIONS]```

```

---

**Arguments:**

- `INPUT_DIR` - Directory containing JSON extraction files## 🐛 Troubleshooting



**Options:**### Installation Issues

| Option | Type | Default | Description |

|--------|------|---------|-------------|**Issue: `permit-toolkit: command not found`**

| `-o, --output` | PATH | `{state}_dataset.csv` | Output file path |```bash

| `--state` | TEXT | None | Filter by state name |# For pip: Ensure virtual environment is activated

| `--format` | CHOICE | `csv` | Output format: `csv`, `excel`, or `json` |source .venv/bin/activate



**Examples:**# For pixi: Use 'pixi run' prefix

```bashpixi run permit-toolkit --help

# Basic consolidation

permit-toolkit consolidate data/extracted/Virginia/# Or reinstall

pip install -e .  # pip

# Custom output with Excel formatpixi install      # pixi

permit-toolkit consolidate data/extracted/ -o results.xlsx --format excel```



# Filter by state**Issue: `No module named 'permit_toolkit'`**

permit-toolkit consolidate data/extracted/ --state Illinois -o il_data.csv```bash

```# Ensure you installed with -e flag

pip install -e .

### `validate` - Validate Extraction Results

# Not: pip install -r requirements.txt

```bash```

permit-toolkit validate EXTRACTION_FILE [OPTIONS]

```**Issue: Python version error**

```bash

**Arguments:**# Check version

- `EXTRACTION_FILE` - Path to JSON extraction filepython --version



**Options:**# Must be >= 3.9

| Option | Type | Description |# Install newer Python if needed

|--------|------|-------------|```

| `-v, --verbose` | FLAG | Enable verbose logging |

### Runtime Issues

**Example:**

```bash**Issue: OpenAI API errors**

permit-toolkit validate data/extracted/Virginia/11790_DC_Permit.json```bash

```# Check API key is set

echo $OPENAI_API_KEY

---

# Set it if empty

## 📊 Output Formatexport OPENAI_API_KEY="sk-your-key-here"



### JSON Structure# Or load from .env

export $(cat .env | grep -v '^#' | xargs)

Each extraction produces a structured JSON file with the following format:```



```json**Issue: Azure OpenAI errors**

{```bash

  "source_file": "11790_DC_Permit.pdf",# Check Azure credentials are set

  "extraction_date": "2025-10-28 12:00:00",echo $AZURE_OPENAI_API_KEY

  "state": "Virginia",echo $AZURE_OPENAI_ENDPOINT

  "model": "compassop-gpt-5",

  "qa_qc_enabled": false,# Set them if empty

  "cost_usd": 0.0094,export AZURE_OPENAI_API_KEY="your-azure-key"

  "processing_time_sec": 84.1,export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"

  "completeness_score": 1.0,

  "generator_count": 26,# Test Azure connection

  "permit_number": "41064",permit-toolkit extract sample.pdf --use-azure --model gpt-4o-mini

  "data": {```

    "permitDetails": {

      "permitNumber": "41064",**Issue: PDF extraction fails**

      "permitIssuanceDate": "2018-08-10",```bash

      "facilityName": "Equinix",# Check PDF file exists and is readable

      "facilityAddress": "18155 Technology Drive Culpeper, VA 22701",ls -lh data/permits/Virginia/your_file.pdf

      "facilityCounty": "Culpeper County"

    },# Try with verbose logging

    "generatorSets": [pixi run permit-toolkit extract your_file.pdf --model gpt-4o-mini

      {```

        "referenceNumber": "DCD-D, DCD-E, DCD-R",

        "numGenerators": 3,**Issue: Pixi installation slow or fails**

        "make": "Caterpillar",```bash

        "model": "3516C-HD",# Clean cache and retry

        "ratedCapacityBHP": 3634,pixi clean

        "ratedCapacityKW": 2500,pixi install

        "fuelType": "no. 2 distillate",

        "fuelSulfurContent": 0.0015,# Check your platform is supported

        "fuelThroughputLimitGallonsPerYear": 2678400,uname -a  # Should be: macOS (Intel/ARM), Linux (x64), or Windows (x64)

        "noxEmissionLimitLbsHr": 14.59,```

        "coEmissionLimitLbsHr": 5.47,

        "vocEmissionLimitLbsHr": 0.82,---

        "pmEmissionLimitLbsHr": 0.55,

        "so2EmissionLimitLbsHr": 0.07## 📦 Dependency Management

      }

    ]This project supports **both pip and pixi** installation methods:

  },

  "validation_notes": []| Method | Files | Best For |

}|--------|-------|----------|

```| **pip** | `requirements.txt`, `pyproject.toml` | Development, CI/CD |

| **pixi** | `pixi.toml` | Production, reproducible environments |

### CSV Consolidation

All dependency files are synchronized to ensure consistency.

The consolidated CSV contains 39 columns per generator:

**Adding new dependencies:**

**Metadata Fields (9):**1. Add to `requirements.txt` and `pyproject.toml`

- source_file, extraction_date, state, model, cost_usd, processing_time_sec, completeness_score, permit_number, generator_count2. Add to `pixi.toml` ([dependencies] or [pypi-dependencies])

3. Test both: `pip install -e .` and `pixi install`

**Permit Details (6):**

- permit_issuance_date, facility_name, facility_address, facility_city, facility_county, facility_state---



**Generator Information (10):**## 🤝 Contributing

- reference_number, num_generators, make, model, rated_capacity_bhp, rated_capacity_kw, maximum_capacity_bhp, maximum_capacity_kw, engine_family_name, serial_number

Contributions are welcome! Please:

**Fuel Data (4):**1. Fork the repository

- fuel_type, fuel_sulfur_content, fuel_throughput_limit_gallons_per_year, natural_gas_usage_mcf_per_year2. Create a feature branch

3. Install dev dependencies: `pip install -e ".[dev]"` or use pixi dev environment

**Emissions Limits (10):**4. Run tests: `pytest`

- nox_emission_limit_lbs_hr, nox_emission_limit_tons_yr, co_emission_limit_lbs_hr, co_emission_limit_tons_yr, voc_emission_limit_lbs_hr, voc_emission_limit_tons_yr, pm_emission_limit_lbs_hr, pm_emission_limit_tons_yr, so2_emission_limit_lbs_hr, so2_emission_limit_tons_yr5. Format code: `black src/` and `ruff check src/`

6. Submit a pull request

---

---

## 🏗️ Architecture

## 📄 License

The toolkit uses a two-stage extraction pipeline:

MIT License - See [LICENSE](LICENSE) file for details.

```

┌─────────────┐---

│   PDF File  │

└──────┬──────┘## 🙏 Acknowledgments

       │

       ▼Developed by the NREL team for air quality permit analysis and backup generator data collection.

┌─────────────────────────────┐

│  Text Extraction            │---

│  (PyMuPDF4LLM)             │

│  • OCR for scanned docs    │## 📞 Support

│  • Preserves structure     │

└──────┬──────────────────────┘- **Issues**: https://github.com/NREL/backupgensprint/issues

       │- **Documentation**: This README

       ▼- **API Costs**: Monitor usage at https://platform.openai.com/usage
┌─────────────────────────────┐
│  Stage 1: OpenAI/Azure      │
│  • GPT-4o/GPT-5            │
│  • Structured extraction   │
│  • ~5-15 seconds           │
│  • $0.001-0.016/permit     │
└──────┬──────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│  Stage 2: QA/QC (Optional)  │
│  • LangExtract validation  │
│  • Source citations        │
│  • Cross-validation        │
│  • +2-5 seconds            │
└──────┬──────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│  Structured JSON Output     │
│  • 39+ fields/generator    │
│  • Metadata & cost info    │
│  • Validation notes        │
└──────┬──────────────────────┘
       │
       ▼
┌─────────────────────────────┐
│  CSV/Excel Consolidation    │
│  • Tabular format          │
│  • Ready for analysis      │
└─────────────────────────────┘
```

### Performance Metrics

| Metric | Value |
|--------|-------|
| **Speed** | 5-15 seconds per permit |
| **Cost (GPT-4o-mini)** | $0.0008-0.0016 per permit |
| **Cost (GPT-4o)** | $0.008-0.016 per permit |
| **Cost (Azure GPT-5)** | $0.009-0.018 per permit |
| **Accuracy** | 90%+ on validation set |
| **Supported States** | Virginia, Illinois (extensible) |

---

## 📁 Project Structure

```
backupgensprint/
├── src/
│   └── permit_toolkit/           # Main package
│       ├── cli/                   # Command-line interface
│       │   ├── main.py           # CLI entry point
│       │   └── commands.py       # Command implementations
│       ├── extraction/            # PDF extraction & LLM processing
│       │   ├── permit_extractor.py  # Main extraction logic
│       │   ├── pdf_utils.py      # PDF text extraction
│       │   ├── qa_qc.py          # QA/QC validation
│       │   └── validator.py      # Cross-validation
│       ├── consolidation/         # Data aggregation
│       │   └── consolidator.py   # JSON to CSV/Excel
│       ├── scrapers/             # Web scraping utilities
│       └── utils/                # Configuration & logging
│           ├── config.py         # Configuration management
│           └── logger.py         # Logging utilities
├── schemas/                       # JSON schemas
│   └── air_quality_permits_schema.json
├── data/                         # Data directories (created on use)
│   ├── permits/                  # Input PDFs
│   ├── extracted/                # JSON outputs
│   └── outputs/                  # Consolidated datasets
├── validation/                   # Validation datasets
│   ├── permits/                  # Validation PDFs
│   └── aqtoolkit/               # Ground truth data
├── tests/                        # Unit tests
├── docs/                         # Documentation
├── .env                          # API credentials (create this)
├── pyproject.toml               # Python package config
├── pixi.toml                    # Pixi dependencies
└── README.md                    # This file
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

# Not: pip install -r requirements.txt (this doesn't install the package)
```

**Issue: Python version error**

```bash
# Check version (must be 3.9+)
python --version

# If too old, install newer Python:
# macOS: brew install python@3.11
# Ubuntu: sudo apt install python3.11
# Windows: Download from python.org
```

### API Issues

**Issue: OpenAI API errors**

```bash
# Verify API key is set
cat .env | grep OPENAI_API_KEY

# If empty, add it
echo "OPENAI_API_KEY=sk-your-key-here" >> .env

# Test with a small file
permit-toolkit extract small_test.pdf
```

**Issue: Azure OpenAI connection fails**

```bash
# Check all required variables are set
cat .env | grep AZURE

# Required variables:
# - AZURE_OPENAI_API_KEY
# - AZURE_OPENAI_ENDPOINT

# Test connection
permit-toolkit extract sample.pdf --use-azure --model gpt-4o-mini
```

**Issue: "Deployment not found" with Azure**

```bash
# Verify your deployment name in Azure Portal
# Update .env with correct deployment name
AZURE_OPENAI_MODEL=your-actual-deployment-name

# Or specify via CLI
permit-toolkit extract file.pdf --use-azure --model your-deployment-name
```

### Runtime Issues

**Issue: PDF extraction fails**

```bash
# Check file exists and is readable
ls -lh data/permits/your_file.pdf

# Ensure PDF is not corrupted
file data/permits/your_file.pdf
# Should say: "PDF document"

# Try with a known-good PDF first
```

**Issue: Slow extraction**

```bash
# Use faster model (less accurate but cheaper)
permit-toolkit extract file.pdf --model gpt-4o-mini

# Disable QA/QC (enabled by default in some configs)
# QA/QC adds 2-5 seconds per permit

# Use Azure for higher rate limits
permit-toolkit extract directory/ --use-azure
```

**Issue: High costs**

```bash
# Monitor usage at: https://platform.openai.com/usage

# Use gpt-4o-mini for lower costs
permit-toolkit extract file.pdf --model gpt-4o-mini

# Expected costs:
# - gpt-4o-mini: $0.0008-0.0016 per permit
# - gpt-4o: $0.008-0.016 per permit
```

---

## 🔧 Development

### Setup Development Environment

```bash
# Clone repository
git clone https://github.com/NREL/backupgensprint.git
cd backupgensprint

# Install with dev dependencies
pip install -e ".[dev]"

# Or with pixi
pixi install --environment dev
```

### Run Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=permit_toolkit

# Run specific test file
pytest tests/test_extraction.py
```

### Code Quality

```bash
# Format code
black src/

# Lint code
ruff check src/

# Type checking
mypy src/
```

---

## 🤝 Contributing

Contributions are welcome! Please follow these guidelines:

1. **Fork the repository** and create a feature branch
2. **Install dev dependencies**: `pip install -e ".[dev]"` or `pixi install --environment dev`
3. **Write tests** for new features
4. **Format code**: `black src/` and `ruff check src/`
5. **Run tests**: `pytest`
6. **Submit a pull request** with a clear description

### Areas for Contribution

- Additional state permit formats
- Improved extraction accuracy
- Performance optimizations
- Documentation improvements
- Bug fixes and error handling

---

## 📄 License

MIT License - See [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

Developed by the NREL team for air quality permit analysis and backup generator data collection.

Special thanks to:
- OpenAI for GPT models
- LangExtract team for validation tools
- PyMuPDF team for PDF processing

---

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/NREL/backupgensprint/issues)
- **Documentation**: This README and inline code documentation
- **API Costs**: Monitor at [OpenAI Usage Dashboard](https://platform.openai.com/usage)

---

## 📈 Roadmap

- [ ] Support for additional states (California, Texas, etc.)
- [ ] Real-time web scraping integration
- [ ] Automated data validation against regulatory databases
- [ ] Web UI for non-technical users
- [ ] Docker containerization
- [ ] Batch processing optimization

---

**Built with ❤️ by NREL**
