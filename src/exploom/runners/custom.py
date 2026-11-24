"""Phase 1 runner: custom (universal fallback)."""

from __future__ import annotations

from typing import Any

from exploom.runners.base import (
    AttemptSpec,
    Runner,
    build_common_probe_snippet,
    encode_script_bootstrap,
)


class CustomRunner(Runner):
    """Universal fallback runner: executes run.sh directly and probes via exit.json."""

    name = "custom"

    def build_wrapped_cmd(self, spec: AttemptSpec) -> str:
        launch = 'bash "$META_DIR/run.sh"'
        return encode_script_bootstrap(spec, launch)

    def build_probe_cmd(
        self,
        attempt: dict[str, Any],
        *,
        include_patch: bool = False,
        log_tail_lines: int = 80,
    ) -> str:
        # Custom runner has no fixed process supervisor; treat as alive until exit.json appears.
        alive_check = '[ ! -f "$META_DIR/exit.json" ]'
        return build_common_probe_snippet(
            attempt,
            alive_check,
            include_patch=include_patch,
            log_tail_lines=log_tail_lines,
        )
