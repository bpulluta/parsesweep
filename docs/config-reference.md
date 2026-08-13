# Configuration Reference

A ParseSweep run is driven by a single YAML config. Each command reads only its
own section, so a config can contain any subset of `extraction`, `compilation`,
`discovery`, and `qaqc`.

Copy the annotated template below to `config/<your_domain>/<your_domain>.yaml`
and edit the sections you need.

```{literalinclude} ../config/TEMPLATE.yaml
:language: yaml
:caption: config/TEMPLATE.yaml
```
