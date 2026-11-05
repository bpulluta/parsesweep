# Solar Ordinances

Domain `solar`: extraction and compilation of solar ordinances from local
documents (no discovery section).

- `run.yaml` — extraction and compilation settings
- `schema.json` — extraction contract
- `page_ranges.csv` — optional manual page ranges for large PDFs

```bash
pixi run psweep extract documents/solar/ --config config/solar/run.yaml
pixi run psweep compile --config config/solar/run.yaml
```

To use the page ranges, add to `run.yaml`:

```yaml
extraction:
  pages:
    csv: page_ranges.csv
```

Keys: [Configuration Reference](../../docs/config-reference.md).
