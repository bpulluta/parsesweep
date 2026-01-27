# Configuration Files

This directory contains configuration files for document processing, organized by category.

## Structure

```
config/
├── tariffs/
│   └── page_ranges.csv      # Page range specifications for tariff documents
└── examples/
    └── page_ranges.csv      # Page range specifications for example documents
```

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
