# Configuration Files

This directory contains runtime configuration files for acquisition, processing,
and consolidation, organized by domain/category.

## Structure

```
config/
├── tariffs/
│   ├── run.yaml             # Optional unified runtime config (acquisition+processing+consolidation)
│   └── page_ranges.csv      # Page range specifications for tariff documents
└── solar/
        └── page_ranges.csv
```

## Runtime Config (Gate 0)

You can now configure `process` and `consolidate` with a runtime config file.

Supported commands:
- `pixi run streamline-extract process --config config/<domain>/run.yaml --validate-config`
- `pixi run streamline-extract process --config config/<domain>/run.yaml --show-effective-config`
- `pixi run streamline-extract consolidate --config config/<domain>/run.yaml --validate-config`

Validation controls:
- `--validate-config`: validate resolved inputs and exit.
- `--show-effective-config`: show resolved values with source attribution.
- `--config-strict`: fail on unknown keys in runtime config sections.

Current supported runtime sections:
- `processing`
- `consolidation`
- `acquisition` (active for `acquire` command)

### Acquisition and SerpApi Notes

For web acquisition runs that use `--enable-serpapi`, set one of:
- `SERPAPI_API_KEY=...`
- `SERPAPI_KEY=...` (fallback supported)

If your environment uses TLS interception or custom cert chains and SerpApi SSL verification fails, disable verification explicitly:
- `SERPAPI_SSL_VERIFY=false`

You can set this in your shell for a single run:

```bash
SERPAPI_SSL_VERIFY=false pixi run streamline-extract acquire ... --enable-serpapi
```

Or place it in `.env` for recurring local runs.

For non-dry acquisition downloads, TLS verification is controlled separately:
- `ACQUISITION_SSL_VERIFY=false`

Example download run with SSL disabled:

```bash
ACQUISITION_SSL_VERIFY=false pixi run streamline-extract acquire --domain <domain> --seed-url <url>
```

### Acquisition Output Organization (Scalable Layout)

For non-dry `acquire` runs, outputs are run-scoped and partitioned by strategy:

```text
documents/<domain>/acquired/runs/<run_id>/
    by_jurisdiction/<state>/<jurisdiction>/...   # when partition mode resolves to jurisdiction
    by_host/<source-host>/...                    # host fallback/default for generic web sources

output/acquisition/<domain>/runs/<run_id>/manifest.json
output/acquisition/<domain>/runs/<run_id>/download_index.csv
```

Manifest download records include:
- `partition_mode`: `jurisdiction` or `host`
- `source_host`: normalized host partition key
- `source_state`: normalized state key when jurisdiction mode is used
- `source_jurisdiction`: normalized jurisdiction key when jurisdiction mode is used
- `relative_path`: path relative to run documents directory
- `path`: full saved path in workspace
- `status`: `downloaded`, `skipped_unsupported_type`, `failed`, etc.

This layout keeps large cross-state, cross-jurisdiction, and cross-website collections organized without domain-specific hardcoding.

### Run-Level Download Index

Each non-dry `acquire` run writes `download_index.csv` next to the manifest.
This file is intended as a machine-readable handoff for downstream batch processing.

Key columns include:
- `run_id`, `domain`
- `partition_mode`
- `source_state`, `source_jurisdiction`, `source_host`
- `status` (`downloaded`, `failed`, `skipped_unsupported_type`, ...)
- `url`, `final_url`, `mime_type`, `bytes`
- `relative_path`, `path`
- `error`

For large multi-state pipelines, prefer ingesting `download_index.csv` rather than walking filesystem trees.

Recommended modes:
- Jurisdiction-centric workloads (for example geothermal ordinances): use `--partition-mode jurisdiction` with `--state` and `--jurisdiction` when known.
- Mixed/social/web-scrape workloads (for example Reddit/X/forums): use `--partition-mode host`.
- General default: `--partition-mode auto` (uses jurisdiction when hints are available, else host).

### Example `run.yaml`

```yaml
domain: geothermal_ordinances

processing:
    input_dir: documents/geothermal_ordinances
    schema: schemas/personal/geothermal_ordinance_schema.json
    output_dir: processed/geothermal_ordinances
    pages_csv: config/geothermal_ordinances/page_ranges.csv
    max_context: 400000

consolidation:
    input_dir: processed/geothermal_ordinances
    schema: schemas/personal/geothermal_ordinance_schema.json
    output_dir: consolidated/geothermal_ordinances
    dry_run: false
```

### Variable Categories

Processing:
- Required: `input_dir`, `schema`
- Optional: `output_dir`, `pages_csv`, `pages`, `profile`, `provider`, `model`, `limit`
- Advanced: `max_context`, `skip_existing`, `enable_qaqc`, `qaqc_lane`, `live_dashboard`

Consolidation:
- Required: `input_dir`, `schema`
- Optional: `output_dir`, `dry_run`, `report_format`
- Advanced: `fail_on_suspicious`

## Page Ranges CSV Format

Page range CSV files specify which pages to extract from specific documents:

```csv
file_path,start_page,end_page
document1.pdf,615,650
document2.pdf,100,200
full_document.pdf,,
```

- **file_path**: Name of the document file (can be filename only or full path)
- **start_page**: First page to extract (1-indexed, inclusive)
- **end_page**: Last page to extract (1-indexed, inclusive)
- Leave both start_page and end_page empty to process the full document

## Usage

Specify page ranges CSV when processing documents:

```bash
pixi run streamline-extract process documents/tariffs/ --pages-csv config/tariffs/page_ranges.csv
```

## Why config/ Instead of documents/?

Configuration files are separated from input documents because:
- **Clear separation of concerns**: Config vs. data
- **Scalability**: Each category can have multiple config files without cluttering documents/
- **Maintainability**: Easy to find and update configurations
- **Version control**: Easier to track config changes separately from large document files
