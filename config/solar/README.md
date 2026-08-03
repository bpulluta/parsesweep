# Solar Ordinances

Document type: Solar Ordinance  
Domain: `solar`

---

## Files

- `run.yaml` — full pipeline config (create this from `config/TEMPLATE.yaml`)
- `page_ranges.csv` — optional manual page-range overrides per document

---

## Workflow

```bash
# 1. Put source documents in documents/solar/

# 2. Extract
pixi run psweep extract --config config/solar/run.yaml

# 3. Compile into Excel
pixi run psweep compile --config config/solar/run.yaml
```

To target specific pages in large PDFs, edit `page_ranges.csv` and reference it from `run.yaml`:

```yaml
extraction:
  pages:
    csv: config/solar/page_ranges.csv
```

## QA/QC (optional)

```bash
pixi run psweep validate --config config/solar/run.yaml

# Rebuild QA/QC reports later without re-running extraction
pixi run psweep validate --config config/solar/run.yaml --compare-only
```
