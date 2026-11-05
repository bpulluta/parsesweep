# ParseSweep — Claude Code Instructions
 
**See [AGENTS.md](AGENTS.md) for full operating instructions** — code quality
standards, config philosophy, working rules, and commands all live there.
 
## Claude-specific context
- Domain inputs: `config/<domain>/{run.yaml,schema.json,targets.csv,samples/}`; outputs derived under `outputs/<domain>/{discovered,extracted,compiled,validated}`
- Consolidated curated discovery dir: `outputs/<domain>/discovered/curated/`
- Pipeline: discover → extract → compile → validate (or `pixi run psweep run`)
- Config keys are documented once, in `docs/config-reference.md`