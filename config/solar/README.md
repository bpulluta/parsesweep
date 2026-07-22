# solar Configuration

Document Type: Solar Ordinance

Domain: Energy - Solar Regulations

This directory contains configuration files for the domain onboarding scaffold.

Files:
- page_ranges.csv: Optional page-range overrides for document processing

Suggested workflow:
1. Add source documents under documents/solar/
2. Update page_ranges.csv if extraction should target a subset of pages
3. Run: pixi run psweep process documents/solar/ --schema schemas/personal/solar_ordinance_schema.json --pages-csv /Users/bpulluta/ParseSweep/config/solar/page_ranges.csv
4. Run: pixi run psweep consolidate processed/solar --schema schemas/personal/solar_ordinance_schema.json

Optional QA/QC workflow:
1. Run: pixi run psweep process documents/solar/ --schema schemas/personal/solar_ordinance_schema.json --pages-csv /Users/bpulluta/ParseSweep/config/solar/page_ranges.csv --enable-qa-qc
2. Run: pixi run psweep compare processed/solar/qa_qc --schema schemas/personal/solar_ordinance_schema.json
3. Optional qualitative review: pixi run psweep compare processed/solar/qa_qc --schema schemas/personal/solar_ordinance_schema.json --qaqc-lane qualitative
