# StreamlineExtract

AI-powered PDF extraction that transforms any document into structured spreadsheets.

## Quick Start

**1. Install**

```bash
curl -fsSL https://pixi.sh/install.sh | bash
git clone https://github.com/bpulluta/StreamlineExtract.git
cd StreamlineExtract
pixi install
```

**2. Add API Key**

Create `.env` file with your Azure OpenAI credentials:

```bash
AZURE_OPENAI_API_KEY=your-key-here
AZURE_OPENAI_API_VERSION=2025-04-01-preview
AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_OPENAI_MODEL=your-model-name
```

**3. Create Your Schema**

Copy `schemas/schema_template.json` and customize for your documents:

```json
{
  "metadata": {
    "documentType": "permit",
    "jurisdiction": "string"
  },
  "extractedData": [
    {
      "id": "string",
      "category": "string",
      "description": "string",
      "value": "string"
    }
  ]
}
```

**4. Run Extraction**

```bash
# Extract PDFs → JSON
pixi run streamline-extract extract documents/your_folder

# Consolidate JSON → Excel  
pixi run streamline-extract consolidate extracted/your_folder

# View results
open consolidated/your_folder/*.xlsx
```

## Examples

**Example 1: Extract permit data**

```bash
# 1. Create schema: schemas/permits.json
# 2. Add PDFs: documents/permits/*.pdf
# 3. Extract
pixi run streamline-extract extract documents/permits --schema schemas/permits.json

# 4. Consolidate
pixi run streamline-extract consolidate extracted/permits

# Output: Beautiful Excel with color-coded data, summaries, and legends
```

**Example 2: Extract rate tariffs**

```bash
# Use example schema
cp schemas/examples/utility_tariff_schema.json schemas/tariffs.json

# Extract
pixi run streamline-extract extract documents/tariffs --schema schemas/tariffs.json

# Consolidate
pixi run streamline-extract consolidate extracted/tariffs
```

**Example 3: Test with sample data**

```bash
# Use geothermal ordinance example
cp schemas/examples/geothermal_ordinance_schema.json schemas/geothermal.json

# Extract (limit to 3 files for testing)
pixi run streamline-extract extract documents/geothermal --schema schemas/geothermal.json --limit 3

# Consolidate
pixi run streamline-extract consolidate extracted/geothermal
```

## Options

```bash
# Test with few files
--limit 3

# Use specific model
--model your-azure-model-name

# Custom output directory
--output /path/to/output

# Get help
pixi run streamline-extract --help
pixi run streamline-extract extract --help
pixi run streamline-extract consolidate --help
```

## Folder Structure

```
documents/      → Your PDFs (organized by category)
schemas/        → Your custom extraction schemas
extracted/      → JSON outputs (auto-generated)
consolidated/   → Excel outputs (auto-generated)
```

## Output

Each consolidation creates a 3-sheet Excel workbook:

- **Data** - All extracted data, color-coded by category
- **Summary** - Statistics, coverage, quality metrics
- **Legend** - Color codes and field descriptions

## Schema Examples

See `schemas/examples/` for complete examples:

- Air quality permits
- Geothermal ordinances
- Utility rate tariffs

Copy and modify for your use case.

## License

MIT License
