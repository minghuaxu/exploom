---
description: Collect and backfill previously run experiments from the current session's conversation memory into Exploom (with deduplication, without re-running anything).
argument-hint: "[--dry-run] [optional scope hint]"
---

Collect all experiments, training/eval runs, data-processing jobs, and ablations that were run or analyzed earlier in **this conversation's memory**, and backfill them into Exploom.

Follow these steps strictly:

1. **Inspect Existing Registry First (Deduplication)**
   - Call `exp_query(view="tree", include_findings=true)` and `exp_query(view="list")` for the current project so you know what is already recorded.
   - Check `$ARGUMENTS`: if `--dry-run` is present, **do not write anything** — only present the proposed collection plan. If a scope hint is given (e.g., a topic or server), restrict collection accordingly.

2. **Extract Experiments from Session Memory**
   - Review the current session's history chronologically and identify each distinct experiment or run (completed, failed, aborted, or still running).
   - For each one, gather only **facts actually present in this session** (never fabricate metrics, commands, server names, or paths):
     - `title`, `hypothesis`, `parent_id` (if derived from an earlier experiment), `tags` (include `"backfilled"`)
     - `server` (**single** valid SSH host name, `local`, or `unknown` — never concatenate multiple hosts like `"hostA + hostB"`; if a workflow spanned multiple servers, record separate attempts for each server), `workdir`, `resource` (e.g., `gpu:0,1`), `run_cmd`, `log_path`
     - `runner` (`ssh-tmux` / `ssh-nohup` / `ssh-screen` / `ssh-slurm` / `ssh-docker` / `local` / `custom`) and actual `runner_ref` (tmux session name, Slurm JobID, or PID) if known
     - `status` (`done`, `running`, `failed_oom`, `failed_disk`, `failed_quota`, `failed_net`, `failed_code`, `aborted`), `exit_code`, `error_summary`
     - `metrics`: **keep strictly compact (3–6 short scalar values such as `F1`, `loss`, `acc`)** so `/exp` stays readable and token-efficient. Put long prose, verdicts, bug fixes, full CLI parameters, hashes, or nested tables into `exp_finding` instead.
     - Key conclusions, error analyses, decisions, or artifact paths for `exp_finding`

3. **Backfill Without Duplicating or Re-running**
   - **Never launch or re-execute any command.** Do NOT call `exp_attempt(action="start")`.
   - If an experiment already exists in Exploom (same title or clearly the same `server`/`workdir`/`run_cmd`):
     - Merge new information via `exp_plan(action="update")`, `exp_attempt(action="update")` (or `action="record"` if this session ran a new attempt), and `exp_finding` (skip duplicate findings).
   - If an experiment is new (process parents before children so `parent_id` links cleanly):
     - Call `exp_plan(action="create", ...)`
     - Call `exp_attempt(action="record", ...)` to record each execution attempt with its actual status and metadata
     - Call `exp_finding(...)` for any diagnosis, analysis, decision, or artifact paths
   - **Upward Stitching (Re-parent Existing Roots)**:
     - After collecting this session's experiments, inspect existing root nodes (`parent_id` is null) from Step 1.
     - If an existing root node is clearly a downstream continuation of a newly collected experiment from this session (e.g., a newer session was collected earlier, and this older session holds its upstream baseline/parent), automatically stitch the trees together via `exp_plan(action="update", exp_id=<existing_root_id>, parent_id=<new_upstream_id>)`.
     - Only re-parent when the lineage relationship is clear from names, checkpoints, configs, or notes; otherwise flag it under **Uncertain / Missing Details** for user confirmation.

4. **Report Concise Summary**
   - **Newly Collected**: `E0xx` | title | status | server/resource | key metrics
   - **Updated / Merged**: existing `E0xx` nodes enriched from this session (if any)
   - **Re-parented / Stitched**: any existing root nodes newly attached under an upstream node (`E001 -> parent E003`)
   - **Uncertain / Missing Details**: any runs where server, log path, final status, or parent link wasn't clear in memory
   - If any collected attempt has `status="running"`, suggest running `/exp-sync` next to verify live status.
