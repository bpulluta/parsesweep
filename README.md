# Data Center Backup Generation Analysis - PJM Territory

## Project Overview

This project aims to create a comprehensive, curated dataset of backup generation systems at data centers across the PJM (Pennsylvania-New Jersey-Maryland) Interconnection territory. The analysis focuses on extracting detailed information from air quality permits to understand the emergency power infrastructure at these critical facilities.

### Project Objectives

1. **Identify Data Centers**: Use EPA ICIS air quality permit data to identify data centers across 13 PJM states
2. **Collect Permit Documents**: Web scrape county/state environmental agency websites to obtain permit PDFs
3. **Extract Structured Data**: Use LlamaExtract or LangExtract to parse permit PDFs into structured JSON
4. **Build Dataset**: Create a consolidated dataset of backup generator specifications and operating parameters

### Target Data Points

From air quality permits, we extract the following information for each backup generator:

**Generator Specifications:**
- Make and Model
- Fuel Type (Diesel, Natural Gas, etc.)
- Rated Capacity (MW/kW)
- Tank Size (gallons)
- Expected maximum duration without refueling

**Operating Parameters:**
- Number of permitted hours of operation per year
- Number of expected hours per year
- Capacity factor (if available)

**Emissions & Controls:**
- Emission limits (NOx, CO, PM, etc.)
- Control technology
- Fuel sulfur content

## Geographic Scope

**PJM Territory States (13):**
- Delaware (DE)
- Illinois (IL)
- Indiana (IN)
- Kentucky (KY)
- Maryland (MD)
- Michigan (MI)
- New Jersey (NJ)
- North Carolina (NC)
- Ohio (OH)
- Pennsylvania (PA)
- Tennessee (TN)
- Virginia (VA)
- West Virginia (WV)

**Current Status:** 309 operating data centers identified with air quality permits

## Project Structure

```
backupgensprint/
│
├── 01_data_sources/           # Original source data
│   └── raw/                   # Raw ICIS data from EPA
│       └── ICIS-AIR_FACILITIES.csv
│
├── 02_data_lists/             # Processed lists of facilities
│   └── pjm_territory/         # Data centers in PJM states
│       ├── pjm_operating_data_centers.csv
│       ├── pjm_data_centers_complete.csv
│       └── data_centers_major_states.csv
│
├── 03_permit_documents/       # Permit PDFs collected from agencies
│   ├── by_state/              # Organized by state
│   │   ├── Delaware/
│   │   ├── Illinois/
│   │   ├── Virginia/
│   │   └── ...
│   └── by_county/             # Alternative organization (TBD)
│
├── 04_extracted_data/         # Structured data extracted from PDFs
│   ├── llamaextract/          # LlamaExtract JSON outputs
│   │   ├── Delaware/
│   │   ├── Virginia/
│   │   └── ...
│   ├── langextract/           # LangExtract JSON outputs
│   │   └── (same structure)
│   └── consolidated/          # Combined and cleaned datasets
│       └── (final CSV/JSON outputs - TBD)
│
├── 05_scripts/                # Python scripts and tools
│   ├── data_collection/       # Scripts to identify and list facilities
│   │   └── icis_data_center_filter.py  # ✅ Filter ICIS data for data centers
│   ├── extraction/            # Web scraping and PDF extraction
│   │   └── (web scraping scripts - TBD)
│   ├── analysis/              # Data analysis scripts
│   │   └── pjm_data_center_analysis.py  # ✅ Analysis and reporting
│   └── utils/                 # Helper utilities
│       └── (utility scripts - TBD)
│
├── 06_outputs/                # Generated reports and datasets
│   ├── reports/               # Analysis reports and documentation
│   │   └── PJM_DataCenter_Analysis_Report.md
│   └── datasets/              # Final curated datasets
│       └── (final outputs - TBD)
│
├── docs/                      # Documentation
    ├── schemas/               # Data schemas and templates
    │   └── air_quality_permits_schema.json  # ✅ Proven schema for LlamaExtract
    └── references/            # Reference materials
        └── AQPermitDatabases_Data Centers_PJM.docx

```

## Workflow

### Phase 1: Data Center Identification ✅ COMPLETE
- [x] Download EPA ICIS air quality facilities data
- [x] Filter for data center NAICS codes (518210, 541511-519)
- [x] Filter for PJM territory states
- [x] Identify operating facilities (309 data centers)
- [x] Export facility lists with contact info and addresses

### Phase 2: Permit Document Collection 🔄 IN PROGRESS
- [ ] Review state/county permit databases (see docs/references/)
- [ ] Develop web scraping scripts for each jurisdiction
- [ ] Download permit PDFs for identified facilities
- [ ] Organize documents by state and facility
- [ ] Track download status and missing permits

### Phase 3: Data Extraction 🔄 IN PROGRESS
- [x] Define extraction schema (see docs/schemas/air_quality_permits_schema.json)
- [x] Test LlamaExtract for extraction (Virginia pilot: 12 permits extracted successfully)
- [ ] Process remaining Virginia permits
- [ ] Scale extraction to all states
- [ ] Quality control and validation

### Phase 4: Data Consolidation 📋 PLANNED
- [ ] Combine extracted JSON files
- [ ] Standardize units and formats
- [ ] Handle missing or incomplete data
- [ ] Calculate derived metrics (capacity factors, etc.)
- [ ] Export final curated dataset

### Phase 5: Analysis & Publication 📋 PLANNED
- [ ] Statistical analysis of backup generation capacity
- [ ] State and regional comparisons
- [ ] Fuel type and capacity distributions
- [ ] Documentation and methodology writeup

## Data Schema

The extraction schema is defined in `docs/schemas/air_quality_permits_schema.json`. This schema has been tested and works well with LlamaExtract. Key entities:

### Permit Details
- Permit number, issuance/expiration dates
- Facility name, address, county

### Generator Sets (array)
- Reference number (e.g., "EG01", "EG02")
- Make and model
- Rated capacity (BHP, kW, MW)
- Fuel type and specifications
- Operating limits (hours/year, fuel throughput)
- Emission limits and control technology

### Fuel Storage
- Tank capacity
- Fuel type
- Number of tanks

## Web Scraping Resources

The document `docs/references/AQPermitDatabases_Data Centers_PJM.docx` contains:
- Links to state/county air quality permit databases
- Agency contact information
- Notes on data availability and access methods

Each state has different systems:
- Some offer online searchable databases
- Some require direct county contact
- PDF availability varies by jurisdiction

## Technology Stack

- **Python 3.x**: Core scripting language
- **pandas**: Data manipulation and CSV processing
- **LlamaExtract / LangExtract**: PDF data extraction (evaluation pending)
- **Web scraping**: TBD (requests, selenium, scrapy, etc.)

## Getting Started

## Development Setup

1. Install pixi: https://pixi.sh/latest/#installation
2. Clone repository
3. Run `pixi shell -e dev`, and you're ready to go!

### Prerequisites
```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Running Scripts

**Filter ICIS data for PJM data centers:**
```bash
cd 05_scripts/data_collection
python pjm_data_center_analysis.py
```

**Analyze facility distributions:**
```bash
cd 05_scripts/analysis
python pjm_data_center_analysis.py
```

## Next Steps

### Immediate Priorities
1. **Web Scraping Development**: Build scrapers for state permit databases (start with Virginia, Illinois, Pennsylvania)
2. **Batch Extraction**: Process all collected permits using LlamaExtract with the proven schema
3. **Data Consolidation**: Build script to merge extracted JSON files into a unified CSV dataset
4. **County Database Mapping**: Create tracking spreadsheet mapping facilities to permit URLs

### Questions to Resolve
- What extraction tool works best? **Answer: LlamaExtract with current schema works well**
- Should we use Selenium for JavaScript-heavy permit portals? **TBD based on state requirements**
- How to handle OCR for scanned PDFs? **TBD when encountered**
- Best way to track extraction errors and missing data? **Build validation script**

## Contributors

This project analyzes critical infrastructure data to understand emergency power capacity at data centers across the PJM grid territory.

## License

TBD

---

**Last Updated:** October 22, 2025
