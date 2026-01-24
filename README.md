# StreamlineExtract

**Universal document extraction system powered by AI.**

Extract structured data from any document type (PDF, DOCX, TXT, XLSX, CSV) into JSON, then consolidate to Excel/CSV.

---

## 🚀 Quick Start

### 1. Install

```bash
curl -fsSL https://pixi.sh/install.sh | bash
git clone https://github.com/bpulluta/StreamlineExtract.git
cd StreamlineExtract
pixi install
```

### 2. Configure API Credentials

**Option A: Interactive Setup (Recommended)**

```bash
pixi run streamline-extract init
```

The setup wizard will guide you through:
- API provider selection (Azure OpenAI or OpenAI)
- Credential configuration
- Document type and schema selection
- Project structure setup

**Option B: Manual Setup**

Create a `.env` file:

```bash
# Option 1: Azure OpenAI (Recommended)
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_OPENAI_MODEL=gpt-4o-mini

# Option 2: OpenAI
OPENAI_API_KEY=sk-your-key
```

### 3. Extract Documents

```bash
# Extract from documents → JSON
pixi run streamline-extract extract documents/your_folder/

# Consolidate JSON → Excel/CSV
pixi run streamline-extract consolidate extracted/your_folder/
```

---

## 📖 CLI Commands

### Core Commands

#### `extract` - Extract data from documents

```bash
# Basic extraction
pixi run streamline-extract extract documents/your_folder/

# With custom schema
pixi run streamline-extract extract documents/tariffs/ \
  --schema schemas/electricity_tariff_schema.json

# With live dashboard (for large batches)
pixi run streamline-extract extract documents/tariffs/ --live-dashboard

# Verbose mode with detailed statistics
pixi run streamline-extract extract documents/tariffs/ --verbose

# Quiet mode (minimal output, ideal for scripting)
pixi run streamline-extract extract documents/tariffs/ --quiet
```

**Key Options:**
- `--schema PATH` - Custom JSON schema
- `--output PATH` - Custom output directory
- `--max-context N` - Max characters to extract (default: 400000)
- `--limit N` - Process only N files (testing)
- `--enable-qa-qc` - Enable quality validation
- `--reprocess` - Reprocess already extracted files
- `--live-dashboard` - Show real-time progress dashboard
- `--verbose` / `-v` - Detailed output with statistics
- `--quiet` / `-q` - Minimal output for scripting
- `--debug` - Debug mode with full logs

#### `consolidate` - Merge JSON into Excel/CSV

```bash
# Consolidate to Excel and CSV
pixi run streamline-extract consolidate extracted/your_folder/

# With custom output location
pixi run streamline-extract consolidate extracted/tariffs/ \
  --output analysis/results/
```

### New Helper Commands

#### `init` - Interactive Project Setup

```bash
pixi run streamline-extract init
```

Guides you through:
- ✅ API provider and credentials
- ✅ Document type selection
- ✅ Schema configuration
- ✅ Project directory setup

#### `preview` - Preview a Document

```bash
pixi run streamline-extract preview documents/sample.pdf
```

Shows:
- 📄 Document metadata (pages, size)
- 🔍 Schema auto-detection
- 💰 Cost and time estimates
- 👁️ Content preview

#### `estimate` - Cost Estimation

```bash
pixi run streamline-extract estimate documents/tariffs/
```

Provides:
- 📊 Document analysis
- 💰 Estimated API costs
- ⏱️ Time estimates with different worker counts
- 🎯 Recommendations

#### `validate-schema` - Validate Schema File

```bash
pixi run streamline-extract validate-schema schemas/my_schema.json
```

Checks:
- ✅ JSON syntax
- ✅ JSON Schema format
- ✅ Required fields
- ✅ Metadata configuration
- 💡 Best practice recommendations

#### `config` - View Configuration

```bash
pixi run streamline-extract config
```

Shows current settings and paths

---

## 📖 Usage Examples

### Geothermal Ordinances

```bash
pixi run streamline-extract extract documents/geothermal_ordinances/
pixi run streamline-extract consolidate extracted/geothermal_ordinances/
```

### Electricity Tariffs

```bash
pixi run streamline-extract extract documents/tariffs/ \
  --schema schemas/electricity_tariff_schema.json \
  --max-context 1400000
  
pixi run streamline-extract consolidate extracted/tariffs/
```

### Custom Schema

```bash
# 1. Create your schema in schemas/my_schema.json
# 2. Extract with custom schema
pixi run streamline-extract extract documents/my_docs/ \
  --schema schemas/my_schema.json \
  --output extracted/my_docs/
```

---

## 🎯 Key Features

### Extraction Features
- **Universal Extraction** - Works with PDF, DOCX, TXT, XLSX, CSV
- **Schema-Driven** - Define what to extract with JSON schemas
- **Auto-Detection** - Automatically selects schema based on document path
- **Live Dashboard** - Real-time progress tracking with cost monitoring
- **Cost Transparency** - See API costs before and during extraction
- **Interactive Prompts** - Confirmations for expensive operations

### User Experience
- **Modern CLI** - Beautiful, informative terminal interface
- **Three Output Modes** - Quiet (scripting), Normal (default), Verbose (detailed)
- **Progress Tracking** - Real-time progress bars and statistics
- **Smart Error Messages** - Helpful errors with suggestions
- **Syntax Highlighting** - Colored JSON/YAML output

### Data Processing
- **Smart Consolidation** - Deduplicates and merges into clean Excel/CSV
- **Multi-Format Output** - Excel (.xlsx) and CSV (.csv)
- **Auto-Sizing** - Excel columns automatically sized for readability
- **Production Ready** - Logging, error handling, state management

---

## 📁 Project Structure

```
documents/          # Input documents
  ├── geothermal_ordinances/
  └── tariffs/
  
schemas/            # JSON extraction schemas
  ├── geothermal_ordinance_schema_streamlined.json
  └── electricity_tariff_schema.json
  
extracted/          # JSON extraction results (auto-generated)
consolidated/       # Excel/CSV outputs (auto-generated)
```

---

## ⚙️ Output Modes & Formatting

### Output Verbosity Modes

```bash
# Quiet mode - minimal output for scripting/automation
pixi run streamline-extract extract documents/ --quiet

# Normal mode - default, balanced output
pixi run streamline-extract extract documents/

# Verbose mode - detailed statistics and progress
pixi run streamline-extract extract documents/ --verbose

# Debug mode - full logs, API details, stack traces
pixi run streamline-extract extract documents/ --debug
```

### Live Dashboard (Multi-Document Batches)

For processing multiple documents (>3 files), enable the live dashboard:

```bash
pixi run streamline-extract extract documents/tariffs/ --live-dashboard
```

**Dashboard shows:**
- 🔄 Real-time progress bar
- 📊 Success/failure counts
- 💰 Running cost total and per-document average
- 🎯 Current document being processed
- ⚡ Token usage statistics

### Cost Confirmation

For large batches (estimated cost > $1), you'll be prompted:

```
⚠ Cost Estimate: $5.23
  Processing 234 documents with ~2,400,000 tokens

Continue? [y/N]
```

Use `--quiet` mode to skip confirmations for automation.

---

## ⚙️ Advanced Options

### Extract Command Options

```bash
# Full list of options
--schema PATH           # Custom JSON schema file
--output PATH          # Custom output directory
--max-context N        # Max characters to extract (default: 400000)
--limit N              # Process only N files
--enable-qa-qc        # Enable QA/QC validation
--reprocess           # Reprocess already extracted files
--live-dashboard      # Show live progress dashboard
--verbose / -v        # Detailed output
--quiet / -q          # Minimal output
--debug               # Debug mode
```

### Performance Tuning

```bash
# For very large documents (e.g., complete tariff books)
--max-context 1400000  # Increase context window

# For testing on sample data
--limit 5              # Process only 5 files

# For batch processing with detailed monitoring
--live-dashboard --verbose
```

---

## 📊 Output Format

### Extraction Output
- Individual JSON files in `extracted/` directory
- One JSON per document with structured data
- Metadata includes costs, completeness scores

### Consolidation Output
- Excel workbook (`.xlsx`) with auto-sized columns
- CSV file (`.csv`) for easy data import
- Automatic deduplication of identical entries
- Clean, analysis-ready format

---

## 🔧 Supported Document Types

| Format | Extension | Notes |
|--------|-----------|-------|
| PDF | `.pdf` | Includes OCR for scanned docs |
| Word | `.docx`, `.doc` | Full text extraction |
| Text | `.txt` | Plain text |
| Excel | `.xlsx` | Tabular data |
| CSV | `.csv` | Comma-separated values |

---

## 💡 Tips & Best Practices

### Getting Started
1. **Use the Setup Wizard**: Run `streamline-extract init` for guided setup
2. **Test First**: Use `--limit 3` to test extraction on a few files before processing large batches
3. **Preview Documents**: Use `streamline-extract preview` to see what will be extracted
4. **Estimate Costs**: Use `streamline-extract estimate` for cost projections

### Schema Management
1. **Auto-Detection**: Put "geothermal" or "tariff" in your document path for automatic schema selection
2. **Validate Schemas**: Always run `streamline-extract validate-schema` before using a new schema
3. **See Best Practices**: Check `schemas/SCHEMA_BEST_PRACTICES.md` for schema design tips

### Performance
1. **Large Documents**: Increase `--max-context` for complete tariff books (tested up to 1.4M chars)
2. **Batch Processing**: Use `--live-dashboard` for real-time monitoring of large batches
3. **Cost Control**: Check cost estimates before processing; use `--limit` for testing

### Output & Debugging
1. **Use Verbose Mode**: Add `--verbose` to see detailed statistics and identify issues
2. **Debug Mode**: Use `--debug` for full API request/response logs when troubleshooting
3. **Quiet Mode for Scripts**: Use `--quiet` when automating or scripting

### Production Use
1. **State Management**: Extraction state is saved - you can safely stop and resume
2. **Reprocessing**: Use `--reprocess` to re-extract failed or updated documents
3. **Quality Validation**: Enable `--enable-qa-qc` for additional validation checks

---

## 📚 Documentation

### Quick Reference
- **All Commands**: Run `pixi run streamline-extract --help`
- **Command Help**: Run `pixi run streamline-extract <command> --help`
- **Examples**: See usage examples above

### In-Depth Guides
- **Schema Design**: `schemas/SCHEMA_BEST_PRACTICES.md`
- **CLI Improvements**: `CLI_IMPROVEMENTS.md` - Full CLI feature documentation
- **Project Setup**: `.github/copilot-instructions.md` - Complete project guide

### Getting Help
- **GitHub Issues**: [Create an issue](https://github.com/bpulluta/StreamlineExtract/issues)
- **GitHub Discussions**: [Ask questions](https://github.com/bpulluta/StreamlineExtract/discussions)

---

## 🔮 Roadmap

### Completed ✅
- ✅ Universal document extraction (PDF, DOCX, TXT, XLSX, CSV)
- ✅ Schema-driven extraction with auto-detection
- ✅ Smart consolidation with deduplication
- ✅ Modern CLI with progress tracking
- ✅ Live dashboard for batch processing
- ✅ Cost tracking and estimation
- ✅ Interactive setup wizard
- ✅ Multiple output modes (quiet, normal, verbose, debug)
- ✅ Preview and validation commands

### In Progress 🚧
- 🚧 REST API (in development on `feature/api-development` branch)
- 🚧 Comprehensive test coverage for CLI features
- 🚧 Video tutorials and walkthroughs

### Planned 📋
- 📋 Web interface for document management
- 📋 Advanced batch processing dashboard
- 📋 Custom plugin system for extractors
- 📋 Cloud deployment templates

---

## 📄 License

MIT License - See LICENSE file for details
