# Air Quality Permit Data Extraction Toolkit

Production-ready extraction system for structured data from air quality permits. Extracts backup generator specifications, emissions data, and facility information from state regulatory documents.

## Features

- **Hybrid Extraction**: Combines OpenAI (structured fields) + LangExtract (generators with traceability)
- **Advanced Parsing**: Handles range notation (EG01-EG06 → 6 units) and quantity notation ((6) Cummins)
- **Quality Assurance**: Interactive HTML visualizations showing extracted entities in context
- **Smart Deduplication**: Filters example contamination and duplicate documents
- **Cost-Effective**: ~30% cheaper than full LangExtract while improving accuracy

## Quick Start

### Installation

```bash
git clone <repository-url>
cd backupgensprint
python -m venv .venv
source .venv/bin/activate
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
result = extractor.extract_from_pdf(pdf_path)

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
src/permit_toolkit/extraction/
├── base_extractor.py          # Hybrid extraction (OpenAI + LangExtract)
├── virginia_extractor.py      # Virginia-specific patterns
├── extractor_factory.py       # Auto-detection and factory
├── pdf_utils.py               # PDF text extraction
├── rate_limiter.py            # API rate limiting
├── deduplicator.py            # Duplicate detection
└── text_optimizer.py          # Text preprocessing

data/
├── permits/Virginia/          # Input PDFs
├── extracted/Virginia/        # JSON outputs
└── comparison/Virginia/       # HTML visualizations

batch_extract_with_visualization.py  # Production batch script
schemas/air_quality_permits_schema.json  # Extraction schema
```

## Validation Results

**Ground Truth Validation** (Virginia permits):

| Permit | Expected | Extracted | Status | Notes |
|--------|----------|-----------|--------|-------|
| 11790 | 7 | 7 | ✅ PASS | Range notation (EG01-EG06) working |
| 11541 | 3 | 3 | ✅ PASS | Deduplication successful |

**Success Rate**: 100% (2/2 validated)

## Configuration

Edit `VIRGINIA_PERMITS` in `batch_extract_with_visualization.py`:

```python
VIRGINIA_PERMITS = [
    ('11790_DC_Permit.pdf', 7),    # Expected count for validation
    ('11541_DC_Permit.pdf', 3),    
    ('41064_DC_Permit.pdf', None), # No expected count (QA review)
]
```

## Development

### Setup Development Environment

```bash
git clone https://github.com/NREL/backupgensprint.git
cd backupgensprint
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

### Project Structure

Core extraction files (all actively used):
- `base_extractor.py` - Hybrid extraction base class (OpenAI + LangExtract)
- `virginia_extractor.py` - Virginia-specific patterns and examples
- `extractor_factory.py` - State detection and extractor instantiation
- `pdf_utils.py` - PDF text extraction with pypdf
- `rate_limiter.py` - Exponential backoff for API rate limiting
- `deduplicator.py` - Document hash-based duplicate detection
- `text_optimizer.py` - Text preprocessing (optional, off by default)

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
           pass
       
       def _extract_permit_details_fallback(self, text: str) -> Dict:
           # Regex fallback for permit details (county, facility name, etc.)
           pass
   ```

2. **Register in factory**: Update `extractor_factory.py`
   ```python
   elif state_lower == "illinois":
       from permit_toolkit.extraction.illinois_extractor import IllinoisPermitExtractor
       return IllinoisPermitExtractor(...)
   ```

3. **Add detection pattern**: Update `_detect_state()` in factory
   ```python
   if "IEPA" in text or "Illinois EPA" in text:
       return "illinois"
   ```

4. **Validate**: Test with sample permits, create validation dataset

### Code Quality

```bash
# Format code
black src/ tests/

# Lint
ruff check src/ tests/

# Type checking
mypy src/
```

### Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Make your changes
4. Run tests and linting
5. Commit with clear messages
6. Push and create a Pull Request

### Architecture Notes

**Why Hybrid?**
- OpenAI direct API: 3x faster for structured fields, no example contamination
- LangExtract: Provides evidence highlighting for QA, better for complex entities

**Key Design Decisions:**
- Range expansion (EG01-EG06) happens post-extraction to avoid confusing the LLM
- Deduplication filters examples by analyzing reference patterns (alphanumeric vs numeric-only)
- Text optimization disabled by default (hurts accuracy on some permits)
- Regex fallback always runs as backup to LLM extraction

## Performance

- **Speed**: ~15-20 seconds per permit
- **Cost**: ~$0.03 per permit (gpt-4o-mini)
- **Rate Limit**: 200k TPM (gpt-4o-mini)
- **Accuracy**: validated Virginia permits

## License

See LICENSE file for details.

