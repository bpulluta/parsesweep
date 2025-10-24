# Air Quality Permit Data Extraction Toolkit

Production-ready system for extracting structured data from air quality permits using OpenAI GPT-4o-mini with dynamic format adaptation.

## Features

✅ **Dynamic Format Handling** - Automatically adapts to different permit structures without configuration
✅ **Reference Preservation** - Maintains exact reference numbers (numeric, alphanumeric, ranges)
✅ **Field Normalization** - Guarantees all 28 schema fields in output (missing = null)
✅ **Fuel Harmonization** - Standardizes fuel types ("distillate oil" → "no. 2 distillate")
✅ **100% Validation Accuracy** - Tested against ground truth from 3 different permit formats

See [PERMIT_FORMAT_VARIATIONS.md](PERMIT_FORMAT_VARIATIONS.md) for details on supported formats.

## Quick Start

### 1. Installation

```bash
# Using pixi (recommended)
pixi install

# Or using pip
pip install -r requirements.txt
```

Create `.env` file in project root:
```
OPENAI_API_KEY=your_key_here
```

### 2. Extract Permits

**Single permit:**
```bash
python scripts/run_extraction.py \
  --pdf data/permits/Virginia/11790_DC_Permit.pdf \
  --output data/extracted/11790_extracted.json
```

**Batch extraction:**
```bash
python scripts/run_extraction.py \
  --input-dir data/permits/Virginia/ \
  --output-dir data/extracted/
```

### 3. Validate Extractions

```bash
python validate_extractions.py
```

This extracts validation permits (11790, 11541, 73757) and compares with ground truth.

## Schema

The extraction schema (`schemas/air_quality_permits_schema.json`) captures 28 fields per generator:

### Core Fields (Required)
- `referenceNumber` - Generator ID (e.g., "EG01-EG06")
- `make` - Manufacturer (Cummins, Caterpillar, etc.)
- `model` - Model number
- `ratedCapacityBHP` - Brake horsepower
- `ratedCapacityKW` - Kilowatts

### Fuel Information
- `primaryFuelType` - Harmonized to "no. 2 distillate" for diesel
- `secondaryFuelType` - For dual-fuel generators
- `otherFuels` - Additional fuels (free text)
- `fuelSulfurContent` - As decimal (e.g., 0.0015)
- `fuelThroughputLimit` - Max gallons/year

### Technical Specs
- `controlTechnology` - Emission control devices
- `operatingHoursLimit` - Max hours/year
- `maximumCapacityBHP/KW` - Maximum capacities
- `numGenerators` - Count in range notation

### Emission Limits
For each pollutant (NOx, CO, VOC, PM, PM10, PM2.5, SO2):
- `{pollutant}EmissionLimitLbsHr` - Pounds/hour
- `{pollutant}EmissionLimitTonsYr` - Tons/year

## Output Format

```json
{
  "permitDetails": {
    "permitNumber": "11790",
    "permitIssuanceDate": "2016-08-23",
    "facilityName": "DP Facilities Inc. South, LLC",
    "facilityAddress": "5978 Windswept BLVD, Wise, VA  24293",
    "facilityCounty": "Wise County"
  },
  "generatorSets": [
    {
      "referenceNumber": "EG01-EG06",
      "numGenerators": 6,
      "make": "Cummins",
      "model": "QSK78-G12",
      "ratedCapacityBHP": 4060,
      "ratedCapacityKW": 2500,
      "primaryFuelType": "no. 2 distillate",
      "fuelSulfurContent": 0.0015,
      "fuelThroughputLimit": 583500,
      "controlTechnology": "turbocharged engines and aftercooler",
      "operatingHoursLimit": 500,
      "noxEmissionLimitLbsHr": 53.7,
      "noxEmissionLimitTonsYr": 83.75
    }
  ]
}
```

## Fuel Type Harmonization

Automatic standardization:

| Original | Standardized |
|----------|-------------|
| Diesel | no. 2 distillate |
| Distillate oil | no. 2 distillate |
| #2 fuel oil | no. 2 distillate |
| #1 fuel oil | no. 1 distillate |

## Key Features

### Dynamic Format Adaptation
- **No configuration needed** - Automatically handles different permit structures
- **Reference number preservation** - Maintains exact format from source (numeric, alphanumeric, ranges)
- **Smart field mapping** - Correctly distinguishes PM, PM10, and PM2.5 emission limits
- **Null handling** - Missing fields set to `null` (not zero or empty string)

### Data Quality
- **28 schema fields guaranteed** - All fields present in every output
- **Generator structure preserved** - Maintains source organization (no artificial consolidation)
- **Range notation support** - "EG01-EG06" stays as 1 entry with `numGenerators: 6`
- **Fuel standardization** - Harmonizes fuel types across different naming conventions

See [PERMIT_FORMAT_VARIATIONS.md](PERMIT_FORMAT_VARIATIONS.md) for detailed examples of supported formats.

## Project Structure

```
backupgensprint/
├── schemas/
│   └── air_quality_permits_schema.json    # Production schema
├── src/permit_toolkit/
│   ├── extraction/
│   │   ├── permit_extractor.py            # Main extraction
│   │   └── pdf_utils.py
│   └── utils/config.py
├── scripts/
│   └── run_extraction.py                  # CLI tool
├── data/
│   ├── permits/                           # Input PDFs
│   ├── extracted/                         # Outputs
│   └── validation/ground_truth.json       # Validation data
├── validate_extractions.py                # Validation script
└── README.md
```

## Validation

## Validation

**Ground Truth Testing** - 3 permits with different formats:

| Permit | Year | Format | Reference Numbers | Entries | Status |
|--------|------|--------|-------------------|---------|--------|
| 11541 | 2008 | Numeric | "3", "2", "1" | 3/3 | ✅ PASS |
| 11790 | 2016 | Range | "EG01-EG06", "EG07" | 2/2 | ✅ PASS |
| 73757 | 2023 | Hybrid | "1510-1", "1510-2", etc. | 4/4 | ✅ PASS |

**Success Rate**: 100% (9/9 generators extracted correctly)

See [PERMIT_FORMAT_VARIATIONS.md](PERMIT_FORMAT_VARIATIONS.md) for detailed format documentation.

## Performance

- **Speed**: ~2-3 seconds per permit
- **Cost**: ~$0.0015-0.003 per permit (gpt-4o-mini)
- **Accuracy**: 100% on validated permits across different formats

## Development

### Setup

```bash
git clone <repository-url>
cd backupgensprint
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Make your changes
4. Run validation: `python validate_extractions.py`
5. Commit with clear messages
6. Push and create a Pull Request

## License

See LICENSE file for details.