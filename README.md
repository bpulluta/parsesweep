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

- **Universal Extraction** - Works with PDF, DOCX, TXT, XLSX, CSV
- **Schema-Driven** - Define what to extract with JSON schemas
- **Auto-Detection** - Automatically selects schema based on document path
- **Smart Consolidation** - Deduplicates and merges into clean Excel/CSV
- **Production Ready** - Cost tracking, logging, error handling

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

## ⚙️ Common Options

```bash
# Limit number of files (for testing)
--limit 5

# Custom schema
--schema path/to/schema.json

# Custom output directory
--output path/to/output/

# Increase context window for large docs
--max-context 1400000

# Enable QA/QC validation
--enable-qa-qc

# Reprocess already extracted files
--reprocess
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

## 💡 Tips

1. **Schema Auto-Detection**: Put "geothermal" or "tariff" in your document path for automatic schema selection
2. **Large Documents**: Increase `--max-context` for complete tariff books (tested up to 1.4M chars)
3. **Testing**: Use `--limit 3` to test extraction on a few files first
4. **Cost Control**: Check extraction logs for API costs per document

---

## 📚 Documentation

- **Schemas**: See `schemas/SCHEMA_BEST_PRACTICES.md`
- **Examples**: Check `schemas/examples/` for sample schemas
- **CLI Help**: Run `pixi run streamline-extract --help`

---

## 🔮 Roadmap

- ✅ Universal document extraction
- ✅ Multi-format support (PDF, DOCX, TXT, XLSX, CSV)
- ✅ Smart consolidation with deduplication
- 🚧 REST API (in development on `feature/api-development` branch)
- 📋 Web interface
- 📋 Batch processing dashboard

---

## 📄 License

MIT License - See LICENSE file for details
