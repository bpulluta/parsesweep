# GitHub Copilot Instructions for StreamlineExtract

## Project Overview
StreamlineExtract is a universal document extraction system that uses LLMs to extract structured data from documents (PDFs, DOCX, TXT, XLSX, CSV) into JSON, then consolidates into Excel/CSV.

**Version 2.0+**: All schemas MUST include `$metadata` section. No heuristic fallbacks.

## Package Manager
**ALWAYS use `pixi` for running commands, NOT pip or python directly.**

## Standard Commands

### Document Extraction

**Basic extraction (single file or directory):**
```bash
pixi run streamline-extract extract documents/path/
```

**Geothermal ordinances extraction:**
```bash
pixi run streamline-extract extract documents/geothermal_ordinances/
```

**Tariff extraction (with custom schema and max context):**
```bash
pixi run streamline-extract extract documents/tariffs/ --schema schemas/electricity_tariff_schema.json --output extracted/tariffs --max-context 1400000
```

**Key options:**
- `--schema`: Specify custom schema JSON file (auto-detected by default)
- `--output`: Custom output directory (auto-detected by default)
- `--max-context`: Maximum characters to extract (default: 400000, increase for large docs)
- `--enable-qa-qc`: Enable LangExtract QA/QC validation
- `--reprocess`: Reprocess already extracted files
- `-n N`: Limit to N files

### Consolidation

**Consolidate extracted JSONs to Excel/CSV:**
```bash
pixi run streamline-extract consolidate extracted/geothermal_ordinances
pixi run streamline-extract consolidate extracted/tariffs
```

**With custom output:**
```bash
pixi run streamline-extract consolidate extracted/data --output my_analysis/
```

**Outputs:**
- Automatically creates `consolidated/` directory
- Generates both `.xlsx` and `.csv` files
- Auto-deduplicates identical entries
- Auto-sizes Excel columns

### Development Commands

**Run tests:**
```bash
pixi run pytest
```

**Install new dependency:**
```bash
# Edit pixi.toml first, then:
pixi install
```

**Run Python script:**
```bash
pixi run python script_name.py
```

## Project Structure

```
StreamlineExtract/
├── documents/              # Input documents
│   ├── geothermal_ordinances/
│   └── tariffs/
├── schemas/                # JSON schemas
│   ├── electricity_tariff_schema.json
│   └── geothermal_ordinance_schema_streamlined.json
├── extracted/              # Raw JSON extractions
│   ├── geothermal_ordinances/
│   └── tariffs/
├── consolidated/           # Final Excel/CSV outputs
│   ├── geothermal_ordinances/
│   └── tariffs/
└── src/streamline_extract/
    ├── cli/               # CLI commands
    ├── extraction/        # Document extraction logic
    ├── consolidation/     # Data consolidation logic
    └── utils/             # Utilities
```

## Supported Document Formats
- PDF (.pdf) - Primary format, includes OCR for image-based PDFs
- Word Documents (.docx, .doc)
- Text files (.txt)
- Excel (.xlsx)
- CSV (.csv)

## Common Workflows

### Full Pipeline (Extract → Consolidate)
```bash
# 1. Clear old data (optional)
rm -rf extracted/category/* consolidated/category/*

# 2. Extract
pixi run streamline-extract extract documents/category/

# 3. Consolidate
pixi run streamline-extract consolidate extracted/category/
```

### Large Document Processing
For documents like complete tariff books:
```bash
pixi run streamline-extract extract documents/tariffs/ \
  --schema schemas/electricity_tariff_schema.json \
  --output extracted/tariffs \
  --max-context 1400000
```

## Schema Information

**v2.0+ REQUIREMENT: All schemas MUST include $metadata section:**
```json
{
  "$metadata": {
    "extraction": {
      "main_data_array": "your_array_key",
      "identifier_fields": ["path.to.id"],
      "context_objects": ["metadata_object"]
    },
    "consolidation": {
      "deduplication": {
        "key_fields": ["unique_fields"],
        "ignore_fields": ["notes", "timestamp"]
      }
    }
  }
}
```

**Auto-detection:**
- Schema is auto-detected based on document path keywords
- `geothermal` in path → geothermal_ordinance_schema.json
- `tariff` in path → electricity_tariff_schema.json
- `permit` or `aq` in path → air_quality_permits_schema.json

**Custom schema:**
- Use `--schema path/to/schema.json` to override
- Schema MUST have valid $metadata or extraction will fail
- See `schemas/SCHEMA_BEST_PRACTICES.md` for examples

## API Configuration

**Environment variables (.env file):**
```bash
# Azure OpenAI (preferred)
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_OPENAI_MODEL=your-deployment-name

# Or OpenAI
OPENAI_API_KEY=sk-your-key
```

## Important Notes

1. **Always use pixi** - Do not use `pip install`, `python`, or `python3` directly
2. **Model defaults** - Don't specify `--model gpt-4o-mini`, it's the default
3. **Auto-detection** - Schema and output paths are auto-detected from input path
4. **Sanity checks** - Warnings like "Sanity checks found N issues" are informational, not errors
5. **Multi-format** - System automatically handles PDFs, DOCX, TXT, XLSX, CSV in the same directory

## Troubleshooting

**"No documents found":**
- Check file extensions are supported (.pdf, .docx, .txt, .xlsx, .csv)
- Verify path exists and contains files

**"Schema missing required $metadata section" (v2.0+):**
- All schemas MUST have $metadata with `main_data_array` and `identifier_fields`
- Update old schemas following examples in schemas/ directory
- See `schemas/SCHEMA_BEST_PRACTICES.md` for migration guide
- Use production schemas as templates: air_quality_permits_schema.json, electricity_tariff_schema.json, geothermal_ordinance_schema.json

**"Schema not found":**
- Use `--schema` to specify explicit schema path
- Check schemas/ directory for available schemas

**Large document timeout:**
- Increase `--max-context` for very long documents
- Default 400k chars is proven reliable for most documents

## Creating New Schemas (v2.0+)

**REQUIRED $metadata structure:**
1. `extraction.main_data_array` - Key for the array of items to extract
2. `extraction.identifier_fields` - Fields used to identify each document
3. `extraction.context_objects` - Metadata objects (optional but recommended)
4. `consolidation.deduplication.key_fields` - Fields for deduplication

**Steps:**
1. Copy an existing schema from schemas/ as a template
2. Modify the properties to match your document structure
3. Update the $metadata section with correct field paths
4. Validate: `pixi run streamline-extract validate-schema schemas/your_schema.json`
5. Test on 1-2 documents before full batch

**Example minimal valid schema:**
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$metadata": {
    "domain": "Your Domain",
    "version": "1.0.0",
    "extraction": {
      "main_data_array": "items",
      "identifier_fields": ["metadata.id"],
      "context_objects": ["metadata"]
    },
    "consolidation": {
      "deduplication": {
        "key_fields": ["name", "type"],
        "ignore_fields": ["notes"]
      }
    }
  },
  "type": "object",
  "properties": {
    "metadata": {
      "type": "object",
      "properties": {
        "id": {"type": "string"}
      }
    },
    "items": {
      "type": "array",
      "items": {"type": "object"}
    }
  }
}
```
