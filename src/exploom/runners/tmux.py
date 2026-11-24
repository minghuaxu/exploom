"""Phase 1 primary runner: ssh-tmux."""

from __future__ import annotations

import shlex
from typing import Any

from exploom.runners.base import (
    AttemptSpec,
    Runner,
    build_common_probe_snippet,
    encode_script_bootstrap,
)


class TmuxRunner(Runner):
    """Runs an experiment inside a detached tmux session on a remote host."""

    name = "ssh-tmux"

    def build_wrapped_cmd(self, spec: AttemptSpec) -> str:
        ref_q = shlex.quote(spec.runner_ref)
        launch = f'tmux new-session -d -s {ref_q} "bash \\"$META_DIR/run.sh\\""'
        return encode_script_bootstrap(spec, launch)

    def build_probe_cmd(
        self,
        attempt: dict[str, Any],
        *,
        include_patch: bool = False,
        log_tail_lines: int = 80,
    ) -> str:
        ref = str(attempt.get("runner_ref") or "")
        alive_check = f"tmux has-session -t {shlex.quote(ref)} 2>/dev/null"
        return build_common_probe_snippet(
            attempt,
            alive_check,
            include_patch=include_patch,
            log_tail_lines=log_tail_lines,
        )
