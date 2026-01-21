# StreamlineExtract

> **User-friendly tool for extracting structured data from air quality permits using AI**

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Convert air quality permit PDFs into structured spreadsheets with just a few commands!

---

## ✨ What Does This Do?

This tool reads PDF permit documents and automatically extracts information about backup generators into a spreadsheet you can analyze in Excel or Google Sheets.

### Key Features

- **Easy to Use** - Simple commands, clear error messages, helpful guidance
- **Accurate** - Uses advanced AI (GPT-5) to read and understand permits
- **Fast** - Process a permit in 10-80 seconds
- **Validated** - Built-in checks to ensure data quality
- **Multi-State** - Works with Virginia, Illinois, Michigan, Kentucky permits
- **Affordable** - ~$0.003-0.013 per permit extraction

---

## 🚀 Quick Start (5 minutes)

### Step 1: Install

We use **pixi** for easy setup (it handles all the technical stuff):

```bash
# Install pixi (one-time, takes ~30 seconds)
curl -fsSL https://pixi.sh/install.sh | bash
# Windows users: iwr -useb https://pixi.sh/install.ps1 | iex

# Clone the project
git clone https://github.com/bpulluta/StreamlineExtract.git
cd StreamlineExtract

# Install everything automatically
pixi install
```

### Step 2: Add Your API Key

You need an OpenAI API key (like a password for using AI). [Get one here](https://platform.openai.com/api-keys) (costs ~$0.01 per permit).

```bash
# Create a .env file with your API key
echo "OPENAI_API_KEY=sk-your-key-here" > .env

# Alternative: Use Azure OpenAI if you have it
cat > .env << 'EOF'
AZURE_OPENAI_API_KEY=your-azure-key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
EOF
```

### Step 3: Extract Data

```bash
# Put your PDF permits in a folder, then:
pixi run permit-toolkit extract permits/Virginia

# The tool will:
# ✓ Find all PDF files
# ✓ Extract data from each permit
# ✓ Save results as JSON files in extracted/Virginia/
```

### Step 4: Create Spreadsheet

```bash
# Combine all extracted data into one spreadsheet
pixi run permit-toolkit consolidate extracted/Virginia

# Opens the file outputs/Virginia/virginia_consolidated.csv
# Open this in Excel or Google Sheets!
```

That's it! 🎉

---

## 📖 Detailed Usage

### Extract Command

Extract data from PDF permits into structured JSON files.

```bash
pixi run permit-toolkit extract <path-to-permits> [options]
```

**Examples:**

```bash
# Extract a single permit
pixi run permit-toolkit extract permits/Virginia/12345.pdf

# Extract all permits in a folder
pixi run permit-toolkit extract permits/Virginia

# Test with just 5 permits first (recommended!)
pixi run permit-toolkit extract permits/Virginia -n 5

# Use Azure OpenAI (faster, higher limits)
pixi run permit-toolkit extract permits/Illinois --use-azure

# Reprocess files that were already extracted
pixi run permit-toolkit extract permits/Virginia --reprocess

# Custom output location
pixi run permit-toolkit extract permits/Virginia -o custom_output/
```

**Options:**
- `-n, --limit N` - Only process first N files (great for testing)
- `--use-azure` - Use Azure OpenAI instead of regular OpenAI
- `--reprocess` - Re-extract files that were already processed
- `-o, --output DIR` - Custom output directory (auto-detected by default)
- `--state NAME` - State name (auto-detected from folder name)

**What It Does:**
- Reads PDF files and extracts text
- Uses AI to identify generator information
- Saves structured data as JSON files
- Shows progress and success/failure for each file
- Automatically organizes output: `permits/Virginia/` → `extracted/Virginia/`

### Consolidate Command

Combine extracted JSON files into a single spreadsheet.

```bash
pixi run permit-toolkit consolidate <path-to-extracted> [options]
```

**Examples:**
**Examples:**

```bash
# Consolidate all extracted permits
pixi run permit-toolkit consolidate extracted/Virginia

# Result: outputs/Virginia/virginia_consolidated.csv

# Consolidate to Excel format
pixi run permit-toolkit consolidate extracted/Illinois --format excel

# Result: outputs/Illinois/illinois_consolidated.xlsx

# Custom output file
pixi run permit-toolkit consolidate extracted/Virginia -o my_data.csv

# Filter specific state from mixed directory
pixi run permit-toolkit consolidate extracted/ --state Virginia
```

**Options:**
- `--format FORMAT` - Output format: `csv`, `excel`, or `json` (default: csv)
- `-o, --output FILE` - Custom output file path
- `--state NAME` - Filter by specific state

**What It Does:**
- Reads all JSON files from extraction
- Combines into a single table (one row per generator)
- Saves as CSV/Excel/JSON in `outputs/` folder
- Automatically organizes: `extracted/Virginia/` → `outputs/Virginia/`

---

## 📊 Understanding the Output

### Extracted JSON Files

Each PDF creates one JSON file with:

```json
{
  "permitDetails": {
    "permitNumber": "11790-DC",
    "facilityName": "1600 Wilson Boulevard",
    "facilityState": "Virginia",
    ...
  },
  "generatorSets": [
    {
      "equipmentId": "GEN-1",
      "manufacturer": "Caterpillar",
      "modelNumber": "3512B",
      "horsepowerBhp": 1700,
      "fuelType": "Diesel",
      "noxLimitLbsHr": 14.5,
      ...
    }
  ]
}
```

### Consolidated Spreadsheet

One row per generator with 52 columns:

| Column Group | Examples |
|--------------|----------|
| **Permit Info** | permit_number, permit_issuance_date, permit_expiration_date |
| **Location** | facility_name, facility_address, facility_county, facility_state |
| **Generator Details** | make, model, rated_capacity_hp, rated_capacity_bhp, rated_capacity_kw |
| **Fuel Information** | primary_fuel_type, fuel_grade, fuel_sulfur_content_pct, fuel_specification |
| **Fuel Limits** | fuel_throughput_limit, fuel_throughput_scope, fuel_certification_required |
| **Operating Limits** | operating_hours_limit, operating_hours_rolling_window, allowed_operating_modes |
| **Monitoring** | hour_meter_required, observation_frequency, recordkeeping_window_years |
| **Compliance** | nsps_subpart_iiii, mact_subpart_zzzz, control_technology |

---

## 🔧 Command Reference

### Get Help Anytime

```bash
# General help
pixi run permit-toolkit --help

# Command-specific help  
pixi run permit-toolkit extract --help
pixi run permit-toolkit consolidate --help
```

### Common Workflows

```bash
# Workflow 1: Process new permits
pixi run permit-toolkit extract permits/NewState
pixi run permit-toolkit consolidate extracted/NewState

# Workflow 2: Test before processing many files
pixi run permit-toolkit extract permits/Virginia -n 3
# Check the results, then process all
pixi run permit-toolkit extract permits/Virginia

# Workflow 3: Update existing extractions
pixi run permit-toolkit extract permits/Virginia --reprocess
pixi run permit-toolkit consolidate extracted/Virginia

# Workflow 4: Multiple states
pixi run permit-toolkit extract permits/Virginia
pixi run permit-toolkit extract permits/Illinois  
pixi run permit-toolkit consolidate extracted/  # All states
```

---

## 🐛 Troubleshooting

### "API key not found" Error

**Problem:** Tool can't find your OpenAI API key.

**Solution:**
```bash
# Check if .env file exists
cat .env

# Should show: OPENAI_API_KEY=sk-...
# If not, create it:
echo "OPENAI_API_KEY=sk-your-actual-key" > .env

# Make sure you're in the project directory
pwd  # Should end with /backupgensprint
```

### "No PDF files found" Error

**Problem:** Tool can't find any PDF files in the directory.

**Solution:**
```bash
# Check what's in your directory
ls permits/Virginia/

# Make sure files end in .pdf
# Check you're using the correct path
```

### "Directory not found" Error

**Problem:** The path you specified doesn't exist.

**Solution:**
```bash
# See where you are
pwd

# List available directories
ls

# Use the correct path, for example:
pixi run permit-toolkit extract ./permits/Virginia
```

### Rate Limit Errors

**Problem:** "Rate limit exceeded" from OpenAI.

**Solution:**
```bash
# Option 1: Use Azure OpenAI (much higher limits)
pixi run permit-toolkit extract permits/ --use-azure

# Option 2: Process in smaller batches
pixi run permit-toolkit extract permits/ -n 10
# Wait a minute, then run again

# Option 3: Upgrade your OpenAI plan
# Visit: https://platform.openai.com/settings/organization/billing
```

### "All files already processed" Message

**Problem:** Tool skips files because they were extracted before.

**Solution:**
```bash
# Use --reprocess to extract again
pixi run permit-toolkit extract permits/Virginia --reprocess
```

### Extraction Quality Issues

**Problem:** Missing or incorrect data in results.

**Solution:**
```bash
# Use more accurate model (slower, more expensive)
pixi run permit-toolkit extract permits/ --model gpt-4o

# Enable detailed validation
pixi run permit-toolkit extract permits/ --enable-qa-qc
```

### Need More Help?

```bash
# Check command help
pixi run permit-toolkit extract --help

# View example outputs
ls validation/aqtoolkit/Virginia/
cat validation/aqtoolkit/Virginia/11790_DC_Permit.json

# Open an issue on GitHub with:
# - The command you ran
# - The error message
# - A sample PDF (if possible)
```

---

## 🏗️ Project Structure

```
StreamlineExtract/
├── permits/              # Put your PDF permits here
│   ├── Virginia/
│   ├── Illinois/
│   └── ...
├── extracted/            # Auto-created: JSON files from extraction
│   ├── Virginia/
│   └── ...
├── outputs/              # Auto-created: Consolidated spreadsheets
│   ├── Virginia/
│   │   └── virginia_consolidated.csv
│   └── ...
├── validation/           # Example data for testing
│   ├── permits/          # Sample PDF permits
│   └── aqtoolkit/        # Sample extracted data
├── schemas/              # Technical: Data structure definitions
├── src/permit_toolkit/   # Technical: Source code
├── .env                  # YOUR API KEY GOES HERE (create this file)
├── pixi.toml             # Technical: Dependencies
└── README.md             # This file
```

**Key Folders:**
- `permits/` - Where you put PDF files to process
- `extracted/` - Where JSON results are saved (auto-created)
- `outputs/` - Where final spreadsheets are saved (auto-created)
- `validation/` - Example data to test the tool

---

## � Tips for Best Results

### Before You Start

1. **Organize your PDFs** - Put them in state-specific folders:
   ```
   permits/
   ├── Virginia/
   │   ├── permit1.pdf
   │   └── permit2.pdf
   └── Illinois/
       └── permit3.pdf
   ```

2. **Test with a few files first** - Use `-n 5` to process just 5 permits
   ```bash
   pixi run permit-toolkit extract permits/Virginia -n 5
   ```

3. **Check the results** - Look at a few JSON files before processing everything
   ```bash
   cat extracted/Virginia/permit1.json | jq
   ```

### For Large Batches

1. **Use Azure OpenAI** if available (faster rate limits)
2. **Process in batches** if you hit rate limits:
   ```bash
   pixi run permit-toolkit extract permits/Virginia -n 20
   # Wait a minute
   pixi run permit-toolkit extract permits/Virginia -n 20
   ```
3. **Use --skip-existing** (default) to resume interrupted processing

### Data Quality

- The tool adds **extraction_notes** to explain uncertain data
- Check the **completeness_score** in JSON files (closer to 1.0 is better)
- Use `--enable-qa-qc` for extra validation (slower but more reliable)

---

## 🔬 Technical Details

<details>
<summary>Click to expand technical information</summary>

### AI Models

- **Default**: `gpt-4o-mini` (fast, $0.15/$0.60 per 1M tokens)
- **Accurate**: `gpt-4o` (slower, $2.50/$10.00 per 1M tokens)
- **Azure**: Configure in `.env` file

### Extraction Schema

46 fields per generator across 7 categories:
1. **Equipment** (9 fields): Reference number, make, model, rated HP/BHP/kW, maximum BHP/kW, generator count
2. **Fuel Specifications** (10 fields): Primary/secondary/other fuel types, grade, specification, sulfur content, certification requirements, change triggers
3. **Fuel Throughput** (3 fields): Limit, scope (per-unit/combined/facility-wide), group reference
4. **Operating Limits** (4 fields): Hours limit, rolling window period, scope, allowed operating modes
5. **Control & Monitoring** (8 fields): Control technology, opacity limit, hour meter, observation frequency, recordkeeping, operation logs, maintenance requirements
6. **Regulations** (2 fields): NSPS Subpart IIII, MACT Subpart ZZZZ compliance
7. **Notes** (1 field): Extraction notes for documenting decisions and ambiguities

**Note:** This schema focuses on operational limits and fuel specifications. Emission limits (NOx, CO, VOC, PM) are referenced through regulatory compliance (NSPS/MACT) rather than extracted as individual numeric limits.

### Performance

- **Speed**: 10-80 seconds per permit (depends on complexity and model)
- **Cost**: $0.003-0.013 per permit with gpt-4o-mini
- **Accuracy**: ~95% field accuracy on validation set

### Dependencies

Managed via `pixi.toml`:
- Python 3.9-3.12
- langextract (LLM extraction framework)
- pandas, openpyxl (data processing)
- pypdf (PDF parsing)
- openai, litellm (AI providers)

</details>

---
## ❓ FAQ

**Q: Do I need to know how to code?**  
A: No! Just follow the commands in this guide. Copy and paste them into your terminal.

**Q: How much does it cost?**  
A: About $0.003-0.013 per permit with OpenAI's gpt-4o-mini model. Processing 100 permits costs ~$0.30-1.30.

**Q: What states does it work with?**  
A: Currently tested with Virginia, Illinois, Michigan, and Kentucky permits. It should work with other states too, but accuracy may vary.

**Q: Can I process permits offline?**  
A: No, the tool needs internet access to communicate with OpenAI's API.

**Q: What if the extraction is wrong?**  
A: Check the `extraction_notes` field in the JSON output - it explains uncertain decisions. You can also use `--enable-qa-qc` for extra validation or manually verify critical data.

**Q: How do I process 1000+ permits?**  
A: Use Azure OpenAI for higher rate limits, or process in batches. The tool automatically resumes if interrupted.

**Q: Can I customize what data is extracted?**  
A: Yes! Edit `schemas/air_quality_permits_schema.json` to add or modify fields. Advanced users only.

**Q: Where can I get help?**  
A: Open a GitHub issue with your question, error message, and sample data.

---

## 📄 License

MIT License - see [LICENSE](LICENSE) file.

---

## 🙏 Credits

Developed for streamlined air quality permit data extraction and analysis.

**Contributors:**
- Data extraction framework
- Schema design and validation
- CLI interface and user experience

**Powered by:**
- OpenAI GPT-4 (AI extraction)
- LangExtract (structured extraction framework)
- Pixi (dependency management)

For questions or issues, please open a GitHub issue.

---

## 🚦 Quick Reference Card

```bash
# Setup (one time)
curl -fsSL https://pixi.sh/install.sh | bash
git clone https://github.com/bpulluta/StreamlineExtract.git
cd StreamlineExtract
pixi install
echo "OPENAI_API_KEY=sk-your-key" > .env

# Extract permits
pixi run permit-toolkit extract permits/Virginia

# Create spreadsheet
pixi run permit-toolkit consolidate extracted/Virginia

# Get help
pixi run permit-toolkit --help
pixi run permit-toolkit extract --help
pixi run permit-toolkit consolidate --help
```

**Common Patterns:**
- Test first: `extract permits/State -n 5`
- Use Azure: `extract permits/State --use-azure`
- Reprocess: `extract permits/State --reprocess`
- Excel output: `consolidate extracted/State --format excel`
