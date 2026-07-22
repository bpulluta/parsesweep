---
description: Audit & clean a pipeline module for universality, scale, and no legacy/redundancy
argument-hint: <module/step name, e.g. process | consolidate | curate>
---

# Audit & clean a pipeline module: $1

You are auditing and cleaning **$1** in ParseSweep so it becomes universal,
clean, modular, configurable, optimized, and scalable across many domains — with
**no hardcoding of domain-specific logic, no redundancy, and no dead/legacy code**.

This is the same methodology already applied to the `acquire` module. Read the
worked example first so you match its bar and output style:
`docs/pipeline_audit/acquire_audit_summary.md`.

Scope note: **QA/QC is a work in progress — skip it** unless explicitly asked.

## Ground rules
- **Domain-neutral or die.** No per-source/per-domain modules, prompts, string
  literals, magic lists, or branches baked into code. Anything domain-specific
  must be config-driven or a genuinely reusable primitive. Config-*gated* helpers
  (inert unless a domain opts in) are acceptable but must be flagged.
- **Behavior-preserving.** Every change keeps existing tests green; add tests for
  new behavior and for any bug you fix. Never weaken a test to make it pass.
- **Backward compatible.** Existing `run.yaml` configs and env-only setups must
  behave identically unless a change is an explicit, documented fix.
- **Honest.** If something is risky to migrate, defer it and record it in the
  "Deferred" list with the reason — do not silently leave it or claim it's clean.
- **Cost-aware.** Prefer caching/reuse and per-stage model selection over
  redundant LLM/network calls; only keep separate calls when they clearly beat
  bundling on quality, and say so.

## Procedure

1. **Map the step end-to-end (read-only first).** Identify the entry point, every
   sub-module it calls, every LLM/network call site, and how config flows in
   (`config/runtime_config_loader.py` → command in `cli/commands.py` → the code).
   Use parallel Explore/Plan agents for breadth; collect conclusions with
   file:line, not file dumps.

2. **Inventory problems** into five buckets, each with file:line evidence:
   - **Hardcoding / domain-specificity** — domain strings, prompts, magic
     numbers, source/extension lists, per-domain branches.
   - **Redundancy** — duplicated logic across files, multiple ways to do one
     thing, copy-pasted helpers.
   - **Dead / legacy code** — unused constants/functions, duplicate decorators,
     test-scaffolding leaked into runtime output, commented-out blocks, TODO/FIXME.
   - **Efficiency / cost** — repeated LLM/network calls, missing caches, redundant
     re-parsing, coarse checkpoints; opportunities to bundle or cache.
   - **Config / model surface** — is model selection per-stage and
     provider-agnostic (route through `extraction/llm_factory.py`)? Are knobs
     validated in the loader and documented in the shipped configs?

3. **Confirm the plan with the user** (EnterPlanMode) before editing if the scope
   is non-trivial. Ask: how aggressive (focused high-value vs full migration), and
   whether to run bounded live validation.

4. **Execute** in small, verifiable steps, updating a TodoWrite list:
   - Extract shared primitives instead of duplicating (see
     `acquisition/retry.py`, `acquisition/urls.py`, `extraction/llm_factory.py`).
   - Route every LLM stage through `extraction/llm_factory.py` so `model:` is a
     plain model name (or an optional user-named alias from the top-level
     `models:` block); unset → inherit env model. No reserved tier names.
   - Move domain-specific logic to config; delete dead code; rename
     test-scaffolding fields to domain-neutral ones and update the tests.
   - Add caching/reuse where it removes repeated cost; validate new config keys in
     the loader; show new knobs as commented guidance in shipped configs.

5. **Validate.** `pixi run python -m pytest tests/ -q` all green; strict-load every
   shipped `run.yaml`; run a **bounded** live pass on ≥2–3 distinct domains
   (`set -a && . ./.env && set +a`, tiny target lists, cheap model) and capture
   before/after cost/behavior. Confirm no domain-neutral regression.

6. **Deliver.** Write `docs/pipeline_audit/$1_audit_summary.md` mirroring the
   acquire summary (what changed, before/after, deferred items + why, verification).
   Update the auto-memory index. Then commit on a dedicated branch with a clear
   message; do not push unless asked.

## Output
A cleaned `$1` step, all tests green, a summary doc, and an updated memory note —
so the next step's audit can reuse this exact command.
