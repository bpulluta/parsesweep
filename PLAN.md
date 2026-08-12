# ParseSweep: Defaults + Embedded Documentation — Work Plan

> **Branch:** `feat/defaults-and-docs`
> **Tracking issue:** https://github.com/bpulluta/parsesweep/issues/3
> **Status:** In progress
>
> **For agents picking this up in a new chat:**
> Read this file first. Each phase has a checkbox list. Check off items as you
> complete them. Run `pixi run pytest` after Phase 1 and `pixi run docs-build`
> after Phase 3. Commit after each phase with the phase number in the message.

---

## Context

Two parallel improvements to the tool:

1. **Reasonable defaults** — fill gaps so `psweep extract docs/ --schema s.json` just works
   without surprises, and every `--help` shows the actual default.
2. **Embedded docs** — NumPy docstrings → Sphinx auto-docs → GitHub Pages.
   The code IS the docs; no separate doc writing step.

**Key findings from audit:**
- `extract --model` has NO help text and no `show_default` — biggest UX gap
- `--max-context` default 400k but TEMPLATE.yaml recommends 600k
- `discover` Choice options (`--partition-mode`, `--robots-policy-mode`,
  `--tos-policy-mode`) show no default in `--help` (default=None but real
  defaults hidden in config)
- Provider/model mismatch: default model is `gpt-4o-mini` (OpenAI) but
  `--provider auto` can detect Azure — silent failure risk
- No `docs/` dir, no `conf.py`, no CI workflows at all
- `pyproject.toml` declares `mkdocs` as docs dep but nothing is wired up —
  replace with Sphinx
- README (698 lines) and `schemas/SCHEMA_BEST_PRACTICES.md` (1025 lines) are
  excellent — embed via `literalinclude`, don't duplicate
- ~54 ruff D violations; ~8 are on real public API, rest are CLI helpers
- No `CHANGELOG.md`, no `CONTRIBUTING.md`, no `.github/workflows/`

**Docstring convention:** NumPy (enforced by ruff `convention = "numpy"`)

---

## Phase 1 — Defaults Hardening

> Safe changes only. Verify with `pixi run pytest` after this phase.
> Commit message: `fix: defaults hardening and --help improvements (phase 1)`

- [ ] **`extract --model`**: add `help=` text + `show_default=True`
  - File: `src/psweep/cli/commands_extract.py` lines 565–568
  - New help: `"LLM model name or alias from run.yaml models: block. Default is gpt-4o-mini (cheap, fast). Use your deployment name for Azure (e.g. gpt-4o)."`

- [ ] **`--max-context` default**: raise 400000 → 600000
  - File: `src/psweep/cli/commands_extract.py` line 597
  - Rationale: TEMPLATE.yaml example uses 600000; 400k silently truncates many PDFs

- [ ] **`discover` Choice options**: set explicit defaults + `show_default=True`
  - File: `src/psweep/cli/commands_discover.py`
  - `--partition-mode` → `default="auto"`
  - `--robots-policy-mode` → `default="warn"`
  - `--tos-policy-mode` → `default="warn"`

- [ ] **Provider/model mismatch warning**: emit `logging.warning()` when
  provider resolves to `"azure"` but model is still `DEFAULT_MODEL` (`"gpt-4o-mini"`)
  - File: `src/psweep/extraction/llm_factory.py` in `build_llm_client()`
  - Message: `"Provider resolved to azure but model is still '%s'. Pin your model in run.yaml models: block or pass --model."`

- [ ] **TEMPLATE.yaml**: add `# default: X` inline comments to every key
  that has a non-obvious default
  - File: `config/TEMPLATE.yaml`

- [ ] **README broken image**: fix `![alt text]` on line 10 pointing to
  `src/psweep/img/imagev1.png` (check if file exists; fix alt text and path)

- [ ] Run `pixi run pytest` — all tests must pass before moving on

---

## Phase 2 — Docstring Standardization

> Commit message: `docs: add/complete NumPy docstrings (phase 2)`
> Run `pixi run ruff check src/` before and after to track D violations.

### 2A — Public API targets (highest priority, ~8 items)

- [ ] `src/psweep/pipeline.py:58` — `ExtractionRunResult.success_rate`
- [ ] `src/psweep/discovery/engine.py:3541` — `DiscoveryEngine.run` method
- [ ] `src/psweep/discovery/models.py:26,35,43,73,109`
  — `weighted_total`, `acceptance_class`, `to_dict` (×3)
- [ ] `src/psweep/compilation/synthesizer.py:465` — public method

### 2B — CLI command functions (feed Sphinx CLI reference)

Each Click command function docstring should have:
- One-line summary
- Extended description (what it does, when to use it)
- `Examples` section with shell commands
- `Defaults` note (model, max-context, output path)

Files to update:
- [ ] `src/psweep/cli/commands_extract.py` — `extract()` function docstring
- [ ] `src/psweep/cli/commands_compile.py` — `compile()` function docstring
- [ ] `src/psweep/cli/commands_discover.py` — `discover()` function docstring
- [ ] `src/psweep/cli/app.py` — `run()` Typer command docstring

### 2C — Auto-fix formatting violations

- [ ] Run `pixi run ruff check --select D --fix src/` to clear auto-fixable D406/D407/D301 violations (~110 items)
- [ ] Manually fix remaining non-auto-fixable D violations

---

## Phase 3 — Sphinx Setup

> Commit message: `build: add Sphinx docs infrastructure (phase 3)`
> Verify with `pixi run docs-build` — must complete without errors.

### 3A — Dependencies

- [ ] Update `pyproject.toml` `[project.optional-dependencies] docs`:
  Replace `mkdocs>=1.5.0` and `mkdocs-material>=9.4.0` with:
  ```toml
  docs = [
      "sphinx>=9.1.0",
      "sphinx-autoapi>=3.0.0",
      "furo>=2024.8.6",
      "myst-parser>=4.0.0",
      "sphinx-click>=6.0.0",
      "sphinx-copybutton>=0.5.2",
  ]
  ```

- [ ] Add `[feature.docs]` to `pixi.toml` with the same deps under
  `[feature.docs.pypi-dependencies]`

- [ ] Add tasks to `pixi.toml`:
  ```toml
  [tasks]
  docs-build = "sphinx-build -b html docs docs/_build/html -W"
  docs-serve = "python -m http.server 8000 --directory docs/_build/html"
  docs-clean = "rm -rf docs/_build"
  ```

### 3B — Directory structure to create

```
docs/
  conf.py
  index.md
  getting-started.md
  commands/
    index.md         ← sphinx-click renders CLI from Click command objects
  config-reference.md  ← literalinclude config/TEMPLATE.yaml with annotations
  schemas/
    index.md         ← literalinclude schemas/SCHEMA_BEST_PRACTICES.md
  changelog.md
  contributing.md
```

### 3C — `docs/conf.py` key settings

```python
project = "ParseSweep"
extensions = [
    "autoapi.extension",
    "sphinx_click",
    "myst_parser",
    "sphinx_copybutton",
]
autoapi_dirs = ["../src/psweep"]
autoapi_options = [
    "members",
    "undoc-members",
    "show-inheritance",
    "show-module-summary",
    "imported-members",
]
html_theme = "furo"
exclude_patterns = ["_build"]
```

- [ ] Create `docs/conf.py`
- [ ] Create `docs/index.md` (landing page, links to all sections)
- [ ] Create `docs/getting-started.md` (install, .env setup, first extraction)
- [ ] Create `docs/commands/index.md` (uses sphinx-click directive)
- [ ] Create `docs/config-reference.md` (literalinclude TEMPLATE.yaml)
- [ ] Create `docs/schemas/index.md` (literalinclude SCHEMA_BEST_PRACTICES.md)
- [ ] Add `docs/_build/` to `.gitignore`
- [ ] Run `pixi run docs-build` — must pass with 0 errors

---

## Phase 4 — GitHub Actions → GitHub Pages

> Commit message: `ci: add docs build and GitHub Pages publish workflow (phase 4)`
> ⚠️ Requires GitHub repo to be PUBLIC (or GitHub Pro for private).

### 4A — Workflow file

- [ ] Create `.github/workflows/docs.yml`:
  ```yaml
  name: Docs
  on:
    push:
      branches: [main]
    pull_request:
      branches: [main]
  jobs:
    build-docs:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - uses: prefix-dev/setup-pixi@v0.8.0
          with:
            pixi-version: "latest"
            environments: docs
        - run: pixi run docs-build
        - name: Deploy to GitHub Pages
          if: github.ref == 'refs/heads/main'
          uses: peaceiris/actions-gh-pages@v4
          with:
            github_token: ${{ secrets.GITHUB_TOKEN }}
            publish_dir: docs/_build/html
  ```

### 4B — Manual GitHub step (owner action required)

1. Merge the PR for this branch
2. Go to **Settings → Pages** in the repo
3. Source: **"Deploy from a branch"** → `gh-pages` branch, `/(root)` folder
4. Docs will be live at `https://bpulluta.github.io/parsesweep/`

---

## Phase 5 — Contributing & Changelog

> Commit message: `docs: add CHANGELOG and CONTRIBUTING (phase 5)`

- [ ] Create `CHANGELOG.md` at repo root (start with v2.0.1 entry)
- [ ] Create `CONTRIBUTING.md` covering: setup, branching, testing, doc build,
  schema authoring
- [ ] Update `docs/changelog.md` and `docs/contributing.md` to include those
  files via `literalinclude`

---

## Files Changed Summary (for PR description)

| File | Change |
|------|--------|
| `src/psweep/cli/commands_extract.py` | defaults + help text |
| `src/psweep/cli/commands_discover.py` | defaults + help text |
| `src/psweep/extraction/llm_factory.py` | provider/model mismatch warning |
| `config/TEMPLATE.yaml` | inline default comments |
| `README.md` | fix broken image |
| `src/psweep/**/*.py` | NumPy docstrings |
| `pyproject.toml` | replace mkdocs with Sphinx deps |
| `pixi.toml` | docs env + tasks |
| `docs/` | entire new directory |
| `.github/workflows/docs.yml` | new CI workflow |
| `CHANGELOG.md` | new file |
| `CONTRIBUTING.md` | new file |
| `.gitignore` | add `docs/_build/` |

---

## How to Continue in a New Chat

1. Open ParseSweep repo in VS Code
2. Make sure branch is `feat/defaults-and-docs` (`git checkout feat/defaults-and-docs`)
3. Say: **"Continue the defaults and docs work from PLAN.md"**
4. The agent reads this file, sees which checkboxes are done, and picks up
   from the first unchecked item in the current phase.
