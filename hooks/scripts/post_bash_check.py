#!/usr/bin/env python3
"""Exploom PostToolUse(Bash) hook (pure Python stdlib, low-noise Strategy A).

Emits a non-blocking lightweight reminder ONLY when a Bash command satisfies ALL 3 conditions:
1. Invokes `ssh `
2. Launches a likely background/long-running job (`tmux`, `nohup`, `screen`, `sbatch`, or `&`)
   (excluding read-only `tmux ls`/`has-session`/`capture-pane` and `screen -ls`)
3. Does NOT contain Exploom wrapper markers (`exploom-wrapper`, `EXPLOOM_WRAPPED`, `.cache/exploom/`)

Daily SSH inspection commands (`nvidia-smi`, `ls`, `cat`, `ps`) stay completely silent.
"""

from __future__ import annotations

import json
import re
import sys

SSH_CMD_RE = re.compile(
    r"(?:^|[;&|(\n]\s*)(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*ssh\s+",
    re.MULTILINE,
)

WRAPPER_MARKERS = (
    "exploom-wrapper",
    "EXPLOOM_WRAPPED",
    ".cache/exploom/",
    "exploom_meta",
)

# Read-only tmux/screen inspection subcommands that should not trigger alerts
READONLY_MUX_RE = re.compile(
    r"\b(?:tmux\s+(?:ls|list-sessions|list-windows|list-panes|has-session|capture-pane)\b"
    r"|screen\s+-ls\b)",
    re.IGNORECASE,
)

BG_LAUNCHER_RE = re.compile(r"\b(?:tmux|nohup|screen|sbatch)\b", re.IGNORECASE)

# Redirections and logical AND that contain '&' but are NOT backgrounding
NON_BG_AMP_RE = re.compile(r"&&|\d*>&\d*|&>")


def _has_background_ampersand(cmd: str) -> bool:
    stripped = NON_BG_AMP_RE.sub(" ", cmd)
    return "&" in stripped


def should_remind(command: str) -> bool:
    """Return True iff the command looks like an untracked remote background SSH job."""
    if not command or not command.strip():
        return False

    # Condition 3: ignore anything already wrapped or managed by Exploom
    for marker in WRAPPER_MARKERS:
        if marker in command:
            return False

    # Condition 1: must invoke ssh
    if not SSH_CMD_RE.search(command):
        return False

    # Condition 2: must launch a background/long-running job
    if _has_background_ampersand(command):
        return True

    # Remove read-only tmux/screen inspection invocations before checking launchers
    cleaned = READONLY_MUX_RE.sub("", command)
    if BG_LAUNCHER_RE.search(cleaned):
        return True

    return False


def _extract_command(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        return tool_input["command"]
    inp = payload.get("input")
    if isinstance(inp, dict) and isinstance(inp.get("command"), str):
        return inp["command"]
    if isinstance(payload.get("command"), str):
        return payload["command"]
    return ""


def main() -> int:
    try:
        raw = sys.stdin.read().strip()
        if not raw:
            return 0
        payload = json.loads(raw)
        command = _extract_command(payload)
        if should_remind(command):
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        "[Exploom] Detected an untracked remote background SSH command. "
                        "If this is a formal experiment, register it with exp_plan + exp_attempt "
                        "so Exploom can track lineage, logs, drift, and recovery."
                    ),
                }
            }
            sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    except Exception:
        # Never block or fail tool execution
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
