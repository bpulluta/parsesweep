# ParseSweep

**AI-powered document extraction pipeline.**

ParseSweep discovers, extracts, and compiles structured data from documents
(PDF, DOCX, TXT, XLSX) into Excel/CSV. Define *what* you need with a JSON
schema, configure *how* to run it with a YAML config, and let the LLM handle
the rest.

```
Documents (PDF, DOCX, TXT, XLSX)
        │
        ▼
┌─── discover ───┐    ┌─── extract ───┐    ┌─── compile ───┐
│ Find docs on   │ →  │ AI extracts   │ →  │ Merge + dedup │ → Excel/CSV
│ the web        │    │ structured    │    │ into clean    │
│ (optional)     │    │ JSON per doc  │    │ spreadsheet   │
└────────────────┘    └───────────────┘    └───────────────┘
```

Each step runs independently via the same config file.

## Documentation

```{toctree}
:maxdepth: 2
:caption: Guides

getting-started
commands/index
config-reference
tuning
schemas/index
```

```{toctree}
:maxdepth: 2
:caption: Reference

autoapi/index
changelog
contributing
```

## Indices

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
