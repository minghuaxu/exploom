---
description: Display the current project's experiment lineage tree and highlight running, stale, and resumable failed runs.
argument-hint: "[--all] [--findings]"
---

Call `exp_query(view="tree")` for the current project (pass `include_findings=true` if `--findings` / `-a` is requested, or `all_projects=true` if `--all` is requested).

Present the lineage tree concisely and highlight:
1. Active `running` experiments (especially any marked `stale`).
2. Resumable failures (`failed_quota`, `failed_net`, `failed_oom`, `failed_disk`) that can be resumed via `/exp-rerun <E0xx>`.
3. The `last_synced_at` timestamp (if stale or `never`, mention `/exp-sync`).
