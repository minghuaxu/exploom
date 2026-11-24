---
description: Concurrently probe all running remote/local experiments via read-only SSH, classify failures, and detect stale logs.
argument-hint: "[E0xx] [--all]"
---

Call `exp_ops(action="sync")` (pass `exp_id` if a specific ID is in `$ARGUMENTS`, or `all_projects=true` if `--all` is specified).

Summarize the sync result cleanly:
- **Status Changes**: completed (`done`) or newly classified failures (`failed_oom`, `failed_disk`, `failed_quota`, `failed_net`, `failed_code`) with short error summaries.
- **Stale Warnings**: any `running` attempts whose logs have been silent past the stale threshold.
- **SSH Errors**: any unreachable servers (noting their previous status was safely preserved).
