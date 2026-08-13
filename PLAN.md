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

## Phase 1 — Defaults Hardening ✅ (redone with single-source-of-truth)

> Verified with `pixi run python -m pytest` (857 passed).
> Commit message: `fix: single-source defaults hardening + accurate --help (phase 1)`
>
> **Design:** every user-facing default is one named constant referenced by the
> dataclass field, the CLI option, the CLI resolution fallback, and (for policy)
> the evaluator signature — so `--help` provably matches runtime behavior. The
> earlier commit `850d27a` hardcoded literals in the decorators, which silently
> flipped the effective robots/ToS default from `ignore` to `warn` while the
> `DiscoveryRequest`/`policies.py` layers still said `ignore` (three conflicting
> sources). This redo makes `warn` the single intentional default everywhere.

- [x] **Shared default constants** (single source):
  - `DEFAULT_ROBOTS_POLICY_MODE = "warn"`, `DEFAULT_TOS_POLICY_MODE = "warn"`
    in `src/psweep/discovery/policies.py` (used by `evaluate()` + dataclass + CLI)
  - `DEFAULT_PARTITION_MODE = "auto"` in `src/psweep/discovery/engine.py`
  - re-exported from `src/psweep/discovery/__init__.py`

- [x] **`extract --model`**: `show_default=True` renders `DEFAULT_MODEL`; help text
  no longer restates the literal (was a stale duplicate)

- [x] **`--max-context` default**: 400000 → 600000 (decorator is its single source)

- [x] **`discover` Choice options**: `default=<constant>` + `show_default=True`;
  resolution fallbacks reference the same constants (no behavioral leak)

- [x] **Provider/model mismatch warning**: `logging.warning()` in
  `llm_factory.build_llm_client()` when provider=azure but model=`DEFAULT_MODEL`

- [x] **TEMPLATE.yaml**: `# default: X` inline comments (verified accurate)

- [x] **README broken image**: alt text fixed

- [x] **Tests**: `tests/test_defaults_single_source.py` locks the constant⇄dataclass
  ⇄CLI⇄evaluator contract; download-retry and topology tests pinned to
  `robots/tos = ignore` so the new `warn` default doesn't pull them onto the network

- [x] Run `pixi run python -m pytest` — 857 passed

---

## Phase 2 — Docstring Standardization ✅ (reviewed + completed to 0 D)

> Commit messages: `docs: add/complete NumPy docstrings (phase 2)` (90e804f) +
> `docs: complete NumPy docstrings to zero D violations (phase 2)`
>
> **Enforced metric:** `pixi run ruff check src/` reports **0 pydocstyle (D)
> violations** (was 58). Note `ruff --select D` re-enables config-ignored codes
> (D105/D205/D400/D401) and inflates the count — the enforced number is the one
> that matters. W505 (doc-line >72) is pre-existing, unenforced style debt
> across the whole codebase and is intentionally left as-is for consistency.

### 2A — Public API targets ✅ (from 90e804f; reviewed for accuracy)

- [x] `pipeline.py` — `ExtractionRunResult.success_rate` (documents 0.0 floor)
- [x] `discovery/engine.py` — `DiscoveryEngine.run`
- [x] `discovery/models.py` — `weighted_total`, `acceptance_class` (thresholds
  verified against code), `to_dict` (×3)
- [x] `compilation/synthesizer.py` — `synthesize_from_directory`

### 2B — CLI command docstrings ✅

- [x] extract / compile / discover / run — summary + description + Examples.
  Removed the `Defaults: …` literal lines (same single-source anti-pattern
  fixed in Phase 1): per-option defaults come from `--help`/sphinx-click, not a
  hand-maintained restatement.

### 2C — Docstring completeness + format ✅

- [x] Documented 46 undocumented public methods/functions (D102/D103) across
  cli (`ui.py`, `run_view.py`, `dashboard.py`, `app.py`), discovery
  (`connectors/digger.py`, `connectors/base.py`, `browser.py`,
  `link_prioritizer.py`, `policies.py`)
- [x] Reformatted `candidate_selector.select` tuple-return docstring (cleared 5
  section-format violations; also completed missing params)
- [x] **D301 / Click conflict:** the `\b` no-rewrap marker in Click command
  docstrings must stay a real escape, so the `r"""` auto-fix would break
  `--help`. Resolved with a per-file-ignore for `src/psweep/cli/**` (framework
  conflict, documented in `pyproject.toml`) — verified `--help` still renders

---

## Phase 3 — Sphinx Setup ✅ (autoapi + sphinx-click; 0-warning `-W` build)

> Commit message: `build: add Sphinx docs infrastructure (phase 3)`
> Verified: `pixi run -e docs docs-build` → **build succeeded, 0 warnings**
> (built with `-W`, warnings-as-errors). `pixi run -e dev ruff check src/` →
> **0 enforced D violations**. `pixi run python -m pytest` → **857 passed**.
>
> **Docs tasks run in the `docs` pixi environment** (`pixi run -e docs docs-build`),
> since Sphinx lives in `[feature.docs]`, not the default env.
>
> **Design decisions beyond the original outline:**
> - **API reference = autoapi (static, no import); CLI reference = sphinx-click**
>   (renders every command/option/**default** from the live Click objects, so
>   `--help` and the docs never drift). The `cli/` package is excluded from
>   autoapi (`autoapi_ignore`) to avoid duplicating sphinx-click and to sidestep
>   Click's `\b`/`EXAMPLES:` help idioms, which are not valid reStructuredText.
> - **`sphinx.ext.napoleon`** renders the NumPy docstrings; `napoleon_use_ivar`
>   stops dataclass `Attributes` sections colliding with autoapi's attribute
>   docs. `imported-members` is intentionally **omitted** (re-exports would be
>   documented twice → duplicate-object + ambiguous-xref warnings).
> - **Docstring cleanup (prereq for a 0-warning build):** converted **93
>   Google-style sections across 33 files** (`Args:`/`Usage:`/`Returns:` …) to
>   NumPy, matching the ruff-enforced convention Phase 2 standardized. Kept
>   enforced D at 0 and all 857 tests green.
> - **`.gitignore`:** the whole `docs/` tree was previously ignored — now only
>   `docs/_build/` is, so the docs source is tracked.

### 3A — Dependencies

- [x] Update `pyproject.toml` `[project.optional-dependencies] docs`:
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

- [x] Add `[feature.docs]` to `pixi.toml` with the same deps under
  `[feature.docs.pypi-dependencies]`

- [x] Add tasks to `pixi.toml`:
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

- [x] Create `docs/conf.py`
- [x] Create `docs/index.md` (landing page, links to all sections)
- [x] Create `docs/getting-started.md` (install, .env setup, first extraction)
- [x] Create `docs/commands/index.md` (uses sphinx-click directive)
- [x] Create `docs/config-reference.md` (literalinclude TEMPLATE.yaml)
- [x] Create `docs/schemas/index.md` (literalinclude SCHEMA_BEST_PRACTICES.md)
- [x] Add `docs/_build/` to `.gitignore`
- [x] Run `pixi run docs-build` — must pass with 0 errors

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
