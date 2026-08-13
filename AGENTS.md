# ParseSweep Agent Operating Instructions

## Purpose
These instructions keep work aligned with the accepted ParseSweep production baseline.

## Code Quality Standards (always apply — every change, no exceptions)
- No legacy code, backward compatibility layers, or deprecated paths
- No redundancy — single source of truth for every concept
- No hardcoding — everything configurable via schema or config YAML
- Modular: each component has one clear responsibility
- Scalable: must work for 10 targets and 100,000 targets
- Clean: remove old code when replacing it, never leave dead paths
- Cohesive: the entire system should feel like one product (CLI style, config patterns, naming)
- Optimized: prefer efficient approaches (generators over lists, parallel over sequential)

## Config Philosophy
- Two files per domain: schema (data contract) + config YAML (runtime behavior)
- Reuse concepts across stages when possible (e.g., nice_to_have_keywords serves classifier AND reviewer)
- Don't duplicate information between schema and config
- User-facing config should be minimal — derive what you can from what's already specified

## Before Writing Code
- Check if the concept already exists elsewhere (don't duplicate)
- Check if an existing mechanism can be extended (don't add new surface area)
- Consider: does this scale? Does this add maintenance burden? Is this the simplest solution?

## Complexity Budget (Value Before Features)
- Default stance: do **not** add new knobs/config unless there is demonstrated user value from a real run.
- Any new config surface must pass all checks:
  1) fixes a reproduced issue in current workflows,
  2) cannot be solved cleanly by existing mechanisms,
  3) is opt-in or behavior-safe by default,
  4) has strict validation (no silent no-op on typos),
  5) includes targeted tests for failure modes and interactions.
- If a feature adds ongoing maintenance but only solves a one-off case, prefer removing it or keeping it domain-local.
- If the same pattern is needed by 2+ domains, consolidate into one shared mechanism; do not fork behavior per domain.

## Default Operating Mode
- Treat the contract-first runtime as the source of truth for this repository.
- Do not reintroduce legacy pathways, compatibility layers, or retired architecture unless explicitly requested by the user.
- Prefer runtime hardening, repo hygiene, and user-facing workflow quality over feature expansion.

## Working Rules
- Always work in a feature branch for substantive repo changes.
- Keep changes phase-gated and test-first.
- Record major decisions in stable user-relevant docs only when they materially affect architecture or operating policy.
- Use `pixi` for repo commands.

## Commands
- Use `pixi` for all repo commands.
- Full pipeline: `pixi run psweep run --config config/<domain>/run.yaml`
- Run tests: `pixi run test`

## Common Pitfall Warning
- Avoid tool flows that hang on "Reading changed files" when simple terminal commands are sufficient.
- Prefer terminal Git commands for quick status/diff checks (`git status --short`, `git diff --name-only`) to keep runs fast and predictable.

## Repository Focus
- The active repo surface should reflect the current production runtime only.
- Keep implementation plans, deprecated experiments, release bookkeeping, and retired helpers out of the tracked repo surface.
- If private historical reference is needed, keep it in an ignored local archive rather than in tracked docs.
- Keep `.github/` and `.agents/` customizations only while they actively help current runtime or onboarding work.

## Completion Criteria for Any Production-System Task
- Code change is implemented or docs/task state is updated.
- Relevant tests are run (or blocker is clearly documented).
- Active user-facing docs and instructions stay consistent with the current runtime surface.
