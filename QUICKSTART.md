# Quick Start Guide

## Getting Started with Data Center Backup Generation Analysis

### Prerequisites
```bash
# Python 3.8 or higher required
python --version
```

### Installation

1. **Clone/Navigate to repository**
   ```bash
   cd /Users/bpulluta/backupgensprint
   ```

2. **Create and activate virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

### Current Status

✅ **Phase 1 Complete**: Data center identification
- 309 operating data centers identified in PJM territory
- Facility lists exported to `02_data_lists/pjm_territory/`

🔄 **Phase 2 In Progress**: Permit document collection
- Virginia: 12 permits collected and extracted (pilot)
- Other states: Awaiting web scraping development

🔄 **Phase 3 Pilot Complete**: Data extraction
- LlamaExtract tested successfully on 12 Virginia permits
- Schema validated: `docs/schemas/air_quality_permits_schema.json`

### What You Can Do Now

#### 1. View Identified Data Centers
```bash
# See list of 309 operating data centers
head -20 02_data_lists/pjm_territory/pjm_operating_data_centers.csv
```

#### 2. Review Extracted Data (Virginia Pilot)
```bash
# View extracted permit data
ls 04_extracted_data/llamaextract/Virginia/

# View a sample extraction
cat 04_extracted_data/llamaextract/Virginia/llama-extract-afbbb238-4012-45f6-a72e-4f3fafe31f33-52432_DC_Permit.json | python -m json.tool | head -50
```

#### 3. Validate Extractions
```bash
cd 05_scripts/utils
python validate_extractions.py
```

#### 4. Consolidate Data to CSV
```bash
cd 05_scripts/utils
python consolidate_extractions.py
```

#### 5. Analyze Facility Distribution
```bash
cd 05_scripts/analysis
python pjm_data_center_analysis.py
```

### Next Steps for Development

#### Priority 1: Scale Data Extraction
- Process remaining Virginia permits (132 facilities remaining)
- Move to high-volume states (Illinois: 48, Pennsylvania: 22)

#### Priority 2: Build Web Scrapers
- Review permit database URLs in `docs/references/AQPermitDatabases_Data Centers_PJM.docx`
- Create state-specific scrapers in `05_scripts/extraction/`
- Start with Virginia DEQ online database

#### Priority 3: Data Consolidation
- Run consolidation script on all extracted data
- Generate summary statistics and visualizations
- Identify data gaps and quality issues

### File Locations

**Key Data Files:**
- Raw ICIS data: [`ICIS-Air Dataset`](https://echo.epa.gov/tools/data-downloads)
- Operating facilities: `02_data_lists/pjm_territory/pjm_operating_data_centers.csv`
- Permit PDFs: `03_permit_documents/by_state/{StateName}/`
- Extracted JSON: `04_extracted_data/llamaextract/{StateName}/`
- Final datasets: `06_outputs/datasets/`

**Key Scripts:**
- Data collection: `05_scripts/data_collection/icis_data_center_filter.py`
- Analysis: `05_scripts/analysis/pjm_data_center_analysis.py`
- Validation: `05_scripts/utils/validate_extractions.py`
- Consolidation: `05_scripts/utils/consolidate_extractions.py`

**Documentation:**
- Main README: `README.md`
- Extraction workflow: `docs/workflows/extraction_workflow.md`
- Web scraping workflow: `docs/workflows/web_scraping_workflow.md`
- Schema: `docs/schemas/air_quality_permits_schema.json`

### Getting Help

- Review workflow documentation in `docs/workflows/`
- Check the main `README.md` for project overview
- Examine extracted JSON examples in `04_extracted_data/llamaextract/Virginia/`

### Common Issues

**"Module not found" errors:**
```bash
# Make sure virtual environment is activated
source .venv/bin/activate
pip install -r requirements.txt
```

**"File not found" errors:**
```bash
# Run scripts from repository root or use absolute paths
cd /Users/bpulluta/backupgensprint
python 05_scripts/utils/validate_extractions.py
```

**CSV encoding issues:**
```bash
# The ICIS CSV is UTF-8 encoded and may have special characters
# pandas should handle this automatically with low_memory=False
```

---

**Last Updated:** October 22, 2025
