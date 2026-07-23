---
agent: agent
description: "Resume ParseSweep web discovery implementation from checklist, execute next unchecked items, keep code clean, run tests, and update tracker."
---

Use this prompt to continue web discovery work in a new chat with no lost context.

Anchor files:
- `archive/private_repo_history/modernization-2026-03-26/plans/temp_web_acquisition/WEB_ACQUISITION_IMPLEMENTATION_CHECKLIST.md`
- `archive/private_repo_history/modernization-2026-03-26/plans/temp_web_acquisition/WEB_ACQUISITION_INTEGRATION_PLAN.md`
- `archive/private_repo_history/modernization-2026-03-26/plans/temp_web_acquisition/UNIFIED_CONFIG_RECONFIGURATION_SPEC.md`
 
Context hygiene rules:
1. Treat these anchor files as compact continuation summaries, not exhaustive historical logs.
2. Prefer current code, tests, and permanent docs over temporary tracker prose when they disagree.
3. Do not expand the tracker with long completed-history notes; keep only active decisions, open work, and recent evidence.

Workflow requirements:
1. Read the checklist and identify the next highest-priority unchecked item.
2. Implement only a coherent slice (one item or tightly related sub-items).
3. Keep code clean and streamlined; do not preserve dead/legacy compatibility paths unless explicitly required.
4. Add/update tests for the slice and run focused `pixi run pytest` commands.
5. Update checklist status and progress log entry in-place.
6. Report changed files, test outcomes, and the next unchecked item.

Execution constraints:
- Use `pixi` for commands.
- Avoid the Git integration tool and use lightweight git terminal commands
- Follow contract-first architecture boundaries.
- Prefer small, reviewable diffs.
