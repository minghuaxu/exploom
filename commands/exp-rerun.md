---
description: Check remote code drift and prepare a rerun/resume command for an experiment, then execute via native Bash after user approval.
argument-hint: "<E0xx> [--fresh] [--force]"
---

Parse `$ARGUMENTS`:
- `exp_id`: required experiment ID (e.g., `E015`).
- `use_resume`: `false` if `--fresh` is present, otherwise `true`.
- `force_on_drift`: `true` if `--force` is present, otherwise `false`.

Call `exp_ops(action="prepare_rerun", exp_id=..., use_resume=..., force_on_drift=...)`:
- If `blocked=true` due to `code_drift` (or SSH error), clearly show `expected_commit`, `current_commit`, and `diff_changed`, and ask the user whether to inspect the diff, create a new child experiment (`exp_plan`), or override with `--force`.
- If `blocked=false`, show any `conflict_warnings`, display the chosen command (`resume_cmd` vs `run_cmd`) and `attempt.ssh_cmd`, and execute `attempt.ssh_cmd` through the native Bash tool so normal tool approval applies.
