# Command Reference

Every command is generated directly from the CLI itself, so the options and
their **defaults shown here always match runtime behavior**.

Options shared across commands:

- `--config` — the domain `run.yaml`; every key is in the
  [Configuration Reference](../config-reference.md), and
  `psweep config --show-runtime-catalog` lists them all from the registry the
  validator uses.
- `--validate-config` — check the config offline and exit.
- `-n` — on `run` and `discover` (`--target-limit`), only the first N targets
  (pilot runs);
  `run` then extracts only those targets' documents. On `extract` (`--limit`),
  only the first N pending files.
- `--fresh` — ignore reusable earlier work for that stage; on `run`, for every
  stage.
- `--quiet`, `--verbose`, `--debug` — minimal, detailed or diagnostic output;
  `run` forwards the choice to every stage. `--debug` adds ParseSweep's own
  debug logs, such as each LLM request's size, timeout and response time.
  Third-party libraries stay at warnings.
- Exit codes: `0` success, `1` failure, `2` nothing found, `3` incomplete
  (re-run to retry the rest).

```{eval-rst}
.. click:: psweep.cli.app:cli
   :prog: psweep
   :nested: full
```
