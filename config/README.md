# config/

One folder per domain, each with the same layout:

```text
config/
├── TEMPLATE.yaml          # valid starting run.yaml covering all four stages
└── <domain>/
    ├── run.yaml           # runtime behavior: models, discovery, extraction, compilation, validation
    ├── schema.json        # extraction contract (default: schema.json beside run.yaml)
    ├── targets.csv        # optional roster, referenced by discovery.targets
    └── samples/           # optional hand-labelled sheets, referenced by validation.references
```

Start a domain with:

```bash
mkdir -p config/my_domain
cp config/TEMPLATE.yaml config/my_domain/run.yaml      # then add targets.csv
pixi run psweep discover --config config/my_domain/run.yaml --validate-config
pixi run psweep run --config config/my_domain/run.yaml --validate-config  # after schema.json
```

`targets`, `pages.csv` and `validation.references` paths resolve against the
domain folder; all outputs go to `outputs/<domain>/`. Every key is documented once, in the
[Configuration Reference](../docs/config-reference.md); schemas are covered in
[Schema Authoring](../docs/schemas/index.md).
