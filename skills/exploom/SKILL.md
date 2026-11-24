---
name: exploom
description: Experiment lineage, multi-server SSH run tracking, failure attribution, and cross-session / subagent handoff via Exploom MCP tools (or exploom CLI --json fallback).
---

# Exploom Experiment Collaboration Rules

Use the 5 Exploom MCP tools (`exp_plan`, `exp_attempt`, `exp_finding`, `exp_query`, `exp_ops`). If MCP is unavailable (e.g., restricted subagent), fall back to `exploom <cmd> --json`.

## Trigger -> Mandatory Action

| Trigger | Mandatory Action |
| :--- | :--- |
| **1. Before launching a training, eval, or data-processing job (remote or local)** | Call `exp_query(view="resources")` to avoid GPU/workdir collisions -> `exp_plan(action="create")` if no `EXP_ID` exists -> `exp_attempt(action="start", ...)` -> execute the returned `ssh_cmd` via native Bash (so tool approval applies). Never bypass with raw `ssh ... tmux/nohup`. |
| **2. Main agent dispatching a subagent for an experiment or investigation** | Call `exp_query(view="resources")` -> `exp_plan(action="create")` to allocate `E0xx` -> start the subagent prompt with `[EXP_ID=E0xx]`. Subagent restores state via `exp_query(view="context", exp_id="E0xx")` instead of relying on long chat summaries. |
| **3. Branching an idea or script in `/side`** | Infer `parent_id` from the most recent active node shown in `SessionStart`, briefly confirm with the user, and call `exp_plan(action="create", parent_id=..., tags=["/side", ...])`. |
| **4. Analysis script finishes, bad cases are found, or a run fails/aborts** | Persist immediately via `exp_finding(exp_id=..., kind=..., summary=..., artifacts=...)` and/or `exp_attempt(action="update", ...)`. Never leave conclusions only in chat text. |
| **5. User mentions an experiment ID (e.g. "continue E015")** | First action MUST be `exp_query(view="context", exp_id="E015")`. Do not guess from chat history. |

## Subagent Handoff & Return Format

- **Receive**: If your prompt contains `[EXP_ID=E0xx]`, call `exp_query(view="context", exp_id="E0xx")` first.
- **Execute & Persist**: Launch via `exp_attempt(action="start")` + native Bash; log findings via `exp_finding`.
- **Report Back (<= 3 lines)**: Return only a compact summary to protect the main context:
  1. `E0xx#<attempt> | <status> | <key metrics or short error>`
  2. `Finding: <E0xx-Fn> — <one-line takeaway>`
  3. `Next: <suggested follow-up or rerun command>`

## Safety Boundary

- `exp_ops` only runs read-only SSH (`sync`, `tail_log`, `prepare_rerun`).
- Always run returned `ssh_cmd` from `exp_attempt(start)` or `exp_ops(prepare_rerun)` through native Bash so the user can approve remote execution.
