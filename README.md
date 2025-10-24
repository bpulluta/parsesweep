# Air Quality Permit Data Extraction Toolkit

Production-ready extraction system for structured data from air quality permits. Extracts backup generator specifications, emissions data, and facility information from state regulatory documents.

## Features

- **Extraction**: Combines OpenAI (structured fields) + LangExtract (generators with traceability)
- **Quality Assurance**: Interactive HTML visualizations showing extracted entities in context
- **Smart Deduplication**: Filters example contamination and duplicate documents
- **Cost-Effective**: ~30% cheaper than full LangExtract while improving accuracy

## Quick Start

### Installation

```bash
git clone <repository-url>
cd backupgensprint
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

Create `.env` file:
```
OPENAI_API_KEY=your_key_here
```

### Extract Single PDF

```python
from pathlib import Path
from permit_toolkit.extraction import ExtractorFactory, load_schema
from permit_toolkit.utils import get_config

# Setup
config = get_config()
schema = load_schema(Path('schemas/air_quality_permits_schema.json'))

# Create extractor
extractor = ExtractorFactory.create_extractor(
    pdf_path=Path('data/permits/Virginia/11790_DC_Permit.pdf'),
    api_key=config.openai_api_key,
    schema=schema,
    model_id='gpt-4o-mini',
)

# Extract data
result = extractor.extract_from_pdf(
    Path('data/permits/Virginia/11790_DC_Permit.pdf')
)

# Generate QA visualization
html_path = extractor.generate_visualization(
    output_dir=Path("visualizations")
)

print(f"Extracted {len(result['generatorSets'])} generators")
print(f"Review at: {html_path}")
```

### Batch Processing

```bash
python batch_extract_with_visualization.py
```

Processes multiple PDFs and generates:
- **JSON outputs**: `data/extracted/Virginia/`
- **HTML visualizations**: `data/comparison/Virginia/visualizations/`

## Architecture

### Hybrid Extraction Strategy

1. **OpenAI Direct** (Structured Fields)
   - Facility name, address, county
   - Permit numbers and dates
   - Faster, more accurate, no example contamination
   - Falls back to regex for validation

2. **LangExtract** (Generators with Traceability)
   - Equipment specifications (make, model, capacity)
   - Emissions data (NOx, CO, VOC, PM, SO2)
   - Operating restrictions
   - Provides evidence highlighting for QA

### Key Capabilities

**Range Notation Expansion**
```
Input:  EG01-EG06 (6) Cummins QSK78-G12 diesel engines
Output: 6 individual generators (EG01, EG02, EG03, EG04, EG05, EG06)
```

**Example Contamination Filtering**
- Detects primary equipment make in document
- Filters phantom generators from examples
- Preserves legitimate identical units

**Interactive Visualizations**
- Shows extracted entities highlighted in original text
- Hover to see attributes and confidence
- Enables rapid QA review

## Project Structure

```
src/permit_toolkit/
├── extraction/
│   ├── base_extractor.py          # Hybrid extraction base class
│   ├── virginia_extractor.py      # Virginia-specific implementation
│   ├── extractor_factory.py       # State detection and factory
│   ├── pdf_utils.py               # PDF text extraction
│   ├── rate_limiter.py            # API rate limiting
│   ├── deduplicator.py            # Duplicate detection
│   └── text_optimizer.py          # Text preprocessing
├── scrapers/
│   ├── base.py                    # Base scraper class
│   └── virginia.py                # Virginia DEQ scraper
├── consolidation/
│   └── consolidator.py            # JSON to CSV conversion
└── utils/
    └── config.py                  # Configuration management

data/
├── permits/Virginia/              # Input PDFs
├── extracted/Virginia/            # JSON outputs
└── comparison/Virginia/           # HTML visualizations

schemas/
└── air_quality_permits_schema.json  # Extraction schema

examples/
├── 01_scrape_permits.py           # Download permits
├── 02_extract_data.py             # Extract structured data
└── 03_consolidate_data.py         # Convert to CSV

batch_extract_with_visualization.py  # Batch processing script
```

## Validation Results

**Ground Truth Validation** (Virginia permits):

| Permit | Expected | Extracted | Status | Notes |
|--------|----------|-----------|--------|-------|
| 11790 | 7 | 7 | ✅ PASS | Range notation (EG01-EG06) expansion |
| 11541 | 3 | 3 | ✅ PASS | Standard extraction |
| 52173 | 3 | 3 | ✅ PASS | Image-based PDF with OCR |

**Success Rate**: 100% (3/3 validated)

## Development

### Setup

```bash
git clone <repository-url>
cd backupgensprint
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

### Run Tests

```bash
pytest tests/
```

### Adding a New State

1. **Create state-specific extractor**: `src/permit_toolkit/extraction/{state}_extractor.py`
   ```python
   from permit_toolkit.extraction.base_extractor import BasePermitExtractor
   
   class IllinoisPermitExtractor(BasePermitExtractor):
       def _create_state_examples(self) -> List[Dict]:
           # Define state-specific examples for LangExtract
           return [
               {
                   "text": "Example permit text...",
                   "entities": [
                       {"text": "G-1", "class": "REF"},
                       {"text": "Caterpillar", "class": "MAKE"},
                   ]
               }
           ]
       
       def _extract_permit_details_fallback(self, text: str) -> Dict:
           # State-specific regex patterns for permit details
           import re
           county_match = re.search(r'County:\s*(\w+)', text)
           return {
               "county": county_match.group(1) if county_match else None,
               # ... other fields
           }
   ```

2. **Register in factory**: Update `src/permit_toolkit/extraction/extractor_factory.py`
   ```python
   elif state_lower == "illinois":
       from permit_toolkit.extraction.illinois_extractor import IllinoisPermitExtractor
       return IllinoisPermitExtractor(api_key, schema, model_id)
   ```

3. **Add detection pattern**: Update `_detect_state()` in factory
   ```python
   if "IEPA" in text or "Illinois EPA" in text:
       return "illinois"
   ```

4. **Validate**: Test with sample permits and create validation dataset

### Code Quality

```bash
# Format code
black src/ tests/

# Lint
ruff check src/ tests/

# Type checking
mypy src/
```

## Performance

- **Speed**: ~15-20 seconds per permit
- **Cost**: ~$0.03 per permit (gpt-4o-mini)
- **Rate Limit**: 200k TPM (gpt-4o-mini tier 1)
- **Accuracy**: 100% on validated Virginia permits

## Troubleshooting

**Issue**: `OPENAI_API_KEY not found`
- Ensure `.env` file exists in project root
- Verify `OPENAI_API_KEY=sk-...` is set correctly

**Issue**: No text extracted from PDF
- Check if PDF is corrupted or encrypted
- Try opening PDF manually to verify content
- Image-based PDFs are supported via pymupdf4llm OCR

**Issue**: Missing generators in output
- Review HTML visualization to see what was extracted
- Check if reference numbers use unexpected format
- Verify schema matches permit structure

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Make your changes
4. Run tests and linting
5. Commit with clear messages
6. Push and create a Pull Request

## License

See LICENSE file for details.