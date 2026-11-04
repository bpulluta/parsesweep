# ParseSweep — Claude Code Instructions
 
**See [AGENTS.md](AGENTS.md) for full operating instructions** — code quality
standards, config philosophy, working rules, and commands all live there.
 
## Claude-specific context
- Domain inputs: `config/<domain>/{run.yaml,schema.json}`; outputs derived under `outputs/<domain>/`
- Consolidated curated discovery dir: `outputs/<domain>/discovered/curated/`
- Pipeline: discover → extract → compile (or `pixi run psweep run`)