"""Phase 2 runner: ssh-screen (GNU Screen detached session)."""

from __future__ import annotations

import re
import shlex
from typing import Any

from exploom.runners.base import (
    AttemptSpec,
    Runner,
    build_common_probe_snippet,
    encode_script_bootstrap,
)


class ScreenRunner(Runner):
    """Runs an experiment inside a detached GNU Screen session on a remote host."""

    name = "ssh-screen"

    def build_wrapped_cmd(self, spec: AttemptSpec) -> str:
        ref_q = shlex.quote(spec.runner_ref)
        launch = f'screen -dmS {ref_q} bash "$META_DIR/run.sh"'
        return encode_script_bootstrap(spec, launch)

    def build_probe_cmd(
        self,
        attempt: dict[str, Any],
        *,
        include_patch: bool = False,
        log_tail_lines: int = 80,
    ) -> str:
        ref = str(attempt.get("runner_ref") or "")
        escaped_ref = re.escape(ref)
        # GNU screen -ls lines look like: "\t12345.exp_proj_E001_1\t(Detached)"
        pattern = shlex.quote(rf"[0-9]+\.{escaped_ref}([[:space:]]|\(|$)")
        alive_check = f"screen -ls 2>/dev/null | grep -Eq {pattern}"
        return build_common_probe_snippet(
            attempt,
            alive_check,
            include_patch=include_patch,
            log_tail_lines=log_tail_lines,
        )
