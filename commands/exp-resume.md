---
description: Restore full context for a specific experiment (ancestors, attempts, findings, markdown note, and patch files) to resume work immediately.
argument-hint: "<E0xx>"
---

Extract the target experiment ID from `$ARGUMENTS` (e.g., `E015`). If no ID was provided, call `exp_query(view="tree")` first and ask which experiment to resume.

Call `exp_query(view="context", exp_id="<E0xx>")` and present a concise restoration brief:
- **Lineage & Hypothesis**: ancestor chain, `based_on_finding`, tags, and goal.
- **Execution State**: server, workdir, resource, latest attempt status/metrics/error, and changed patch files.
- **Findings & Notes**: key conclusions and analysis artifact paths.
- **Ready Next Step**: propose the immediate next action (e.g., `/exp-rerun`, code fix, or new child experiment).
