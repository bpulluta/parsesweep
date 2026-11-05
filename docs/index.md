# ParseSweep

**Find documents, extract schema-defined data with an LLM, and compile it into one table.**

ParseSweep turns a list of targets into a spreadsheet with one row per target.
A JSON schema defines *what* to extract; one `run.yaml` per domain defines
*how* every stage runs.

```text
discover  →  extract  →  compile  →  validate
web/sources  JSON per     one table     models and
→ documents  document     per domain    references
```

Documents can be PDF, Word, text, HTML, CSV, Excel, JSON or audio recordings —
see [Document types and limits](config-reference.md#document-types-and-limits).
Start with [Getting Started](getting-started.md).

## Documentation

```{toctree}
:maxdepth: 2
:caption: Guides

getting-started
sources
config-reference
tuning
schemas/index
commands/index
security
```

```{toctree}
:maxdepth: 1
:caption: Reference

changelog
contributing
```

```{toctree}
:maxdepth: 1
:caption: API

autoapi/index
```

## Indices

- {ref}`genindex`
- {ref}`modindex`
- {ref}`search`
