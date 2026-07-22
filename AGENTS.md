# ParseSweep Agent Operating Instructions

## Purpose
These instructions keep work aligned with the accepted ParseSweep production baseline.

## Default Operating Mode
- Treat the contract-first runtime as the source of truth for this repository.
- Do not reintroduce legacy pathways, compatibility layers, or retired architecture unless explicitly requested by the user.
- Prefer runtime hardening, repo hygiene, and user-facing workflow quality over feature expansion.

## Working Rules
- Always work in a feature branch for substantive repo changes.
- Keep changes phase-gated and test-first.
- Record major decisions in stable user-relevant docs only when they materially affect architecture or operating policy.
- Use `pixi` for repo commands.

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
