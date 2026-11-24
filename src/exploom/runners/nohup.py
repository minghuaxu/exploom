"""Phase 1 runner: ssh-nohup."""

from __future__ import annotations

import shlex
from typing import Any

from exploom.runners.base import (
    AttemptSpec,
    Runner,
    build_common_probe_snippet,
    encode_script_bootstrap,
)


class NohupRunner(Runner):
    """Runs an experiment via nohup in the background, storing PID in $META_DIR/pid."""

    name = "ssh-nohup"

    def build_wrapped_cmd(self, spec: AttemptSpec) -> str:
        launch = (
            '{ nohup bash "$META_DIR/run.sh" >/dev/null 2>&1 </dev/null & '
            'echo $! > "$META_DIR/pid"; }'
        )
        return encode_script_bootstrap(spec, launch)

    def build_probe_cmd(
        self,
        attempt: dict[str, Any],
        *,
        include_patch: bool = False,
        log_tail_lines: int = 80,
    ) -> str:
        ref = str(attempt.get("runner_ref") or "")
        # Prefer $META_DIR/pid; fall back to numeric runner_ref if user provided a PID
        fallback_pid = shlex.quote(ref) if ref.isdigit() else '""'
        alive_check = (
            f'PID=""; [ -f "$META_DIR/pid" ] && PID="$(cat "$META_DIR/pid" 2>/dev/null)"; '
            f'[ -z "$PID" ] && PID={fallback_pid}; '
            f'[ -n "$PID" ] && kill -0 "$PID" 2>/dev/null'
        )
        return build_common_probe_snippet(
            attempt,
            alive_check,
            include_patch=include_patch,
            log_tail_lines=log_tail_lines,
        )
