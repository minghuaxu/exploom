"""Phase 2 runner: ssh-docker (host-mounted shared meta_dir + docker inspect/exec)."""

from __future__ import annotations

import shlex
from typing import Any

from exploom.runners.base import (
    AttemptSpec,
    Runner,
    build_common_probe_snippet,
    encode_script_bootstrap,
)


def extract_docker_container(runner_ref: str | None, resource: str | None = None) -> str:
    """Resolve the target Docker container name from runner_ref ('container:ref') or resource."""
    ref = (runner_ref or "").strip()
    if ":" in ref:
        container = ref.split(":", 1)[0].strip()
        if container:
            return container
    res = (resource or "").strip()
    for prefix in ("docker:", "container:"):
        if res.lower().startswith(prefix):
            container = res[len(prefix) :].strip()
            if container:
                return container
    if ref:
        return ref
    raise ValueError(
        "ssh-docker requires a container name via runner_ref ('<container>:<ref>'), "
        "resource ('docker:<container>'), or [servers.<name>].docker_container in config.toml"
    )


class DockerRunner(Runner):
    """Runs an experiment inside a running Docker container sharing a mounted meta_dir.

    Host-side SSH writes $META_DIR/run.sh into the shared mount (configured via
    `[servers.<name>].meta_dir` in ~/.exploom/config.toml), launches it detached via
    `docker exec -d`, and probes via `docker inspect` + `docker exec kill -0` while
    reading exit.json / meta.json / dirty.patch directly from the host mount.
    """

    name = "ssh-docker"

    def build_wrapped_cmd(self, spec: AttemptSpec) -> str:
        container = extract_docker_container(spec.runner_ref)
        container_q = shlex.quote(container)
        launch = (
            f"docker exec -d {container_q} "
            f'bash -c \'echo $$ > "$1/pid"; exec bash "$1/run.sh"\' _ "$META_DIR"'
        )
        return encode_script_bootstrap(spec, launch)

    def build_probe_cmd(
        self,
        attempt: dict[str, Any],
        *,
        include_patch: bool = False,
        log_tail_lines: int = 80,
    ) -> str:
        container = extract_docker_container(
            attempt.get("runner_ref"), attempt.get("resource")
        )
        container_q = shlex.quote(container)
        alive_check = (
            f'[ "$(docker inspect -f \'{{{{.State.Running}}}}\' {container_q} 2>/dev/null)" = "true" ] && '
            f'[ -f "$META_DIR/pid" ] && '
            f'PID="$(tr -d "[:space:]" < "$META_DIR/pid" 2>/dev/null)" && '
            f'[ -n "$PID" ] && '
            f'docker exec {container_q} kill -0 "$PID" 2>/dev/null'
        )
        return build_common_probe_snippet(
            attempt,
            alive_check,
            include_patch=include_patch,
            log_tail_lines=log_tail_lines,
        )
