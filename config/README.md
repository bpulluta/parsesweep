# config/

```text
config/
├── README.md          # this file
├── TEMPLATE.yaml      # valid starting run.yaml covering all four stages
├── example/           # runnable three-county demo, laid out like <domain>/
└── <domain>/          # one folder per domain you create
    ├── run.yaml       # runtime behavior: models, discovery, extraction, compilation, validation
    ├── schema.json    # extraction contract (default: schema.json beside run.yaml)
    ├── targets.csv    # optional roster, referenced by discovery.targets
    ├── page_ranges.csv # optional page ranges, referenced by extraction.pages.csv
    └── samples/       # optional hand-labelled sheets, referenced by validation.references
```

Only `README.md`, `TEMPLATE.yaml` and `example/` are tracked. Domain folders you
create here are git-ignored, so your domains stay local.

Start a domain from `TEMPLATE.yaml` as shown in
[Getting Started](../docs/getting-started.md). `targets`, `extraction.pages.csv` and
`validation.references` paths resolve against the domain folder; all outputs go
to `outputs/<domain>/` (or `outputs/KEY/` with `--domain KEY` on any stage, for a second
workspace from the same config). Every key is documented once, in the
[Configuration Reference](../docs/config-reference.md); schemas are covered in
[Schema Authoring](../docs/schemas/index.md).
