"""Phase 1 runner: local (no SSH)."""

from __future__ import annotations

from typing import Any

from exploom.runners.nohup import NohupRunner


class LocalRunner(NohupRunner):
    """Runs an experiment locally in the background without SSH."""

    name = "local"

    def build_probe_cmd(
        self,
        attempt: dict[str, Any],
        *,
        include_patch: bool = False,
        log_tail_lines: int = 80,
    ) -> str:
        return super().build_probe_cmd(
            attempt,
            include_patch=include_patch,
            log_tail_lines=log_tail_lines,
        )
