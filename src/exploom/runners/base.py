"""Base runner abstraction and zero-install POSIX/Bash wrapper script builder."""

from __future__ import annotations

import base64
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from exploom.config import DEFAULT_MAX_PATCH_BYTES

WRAPPER_MARKER = "exploom-wrapper"


@dataclass(frozen=True)
class AttemptSpec:
    """Parameters needed by a Runner to generate a wrapped launch command."""

    project: str
    exp_id: str
    attempt_no: int
    server: str
    workdir: str
    run_cmd: str
    meta_dir: str
    log_path: str
    runner: str
    runner_ref: str
    max_patch_bytes: int = DEFAULT_MAX_PATCH_BYTES


def _expand_meta_dir_expr(meta_dir: str) -> str:
    """Allow $HOME expansion in meta_dir while safely quoting the rest."""
    if meta_dir.startswith("$HOME/"):
        rest = meta_dir[len("$HOME/") :]
        return f'"$HOME"/{shlex.quote(rest)}'
    if meta_dir.startswith("~/"):
        rest = meta_dir[2:]
        return f'"$HOME"/{shlex.quote(rest)}'
    return shlex.quote(meta_dir)


def build_run_script(spec: AttemptSpec) -> str:
    """Build the self-contained bash script written to $META_DIR/run.sh.

    Performs 4 actions with zero remote dependencies and zero Git pollution:
    1. Captures Git commit, branch, diff SHA256, and <=200KB dirty.patch (gracefully nulls if non-Git).
    2. Writes meta.json with pid, started_at, workdir, log_path, runner info, and git fingerprint.
    3. Executes user run_cmd inside workdir, redirecting stdout+stderr to log_path.
    4. Atomically writes exit.json on EXIT/INT/TERM/HUP via trap.
    """
    meta_expr = _expand_meta_dir_expr(spec.meta_dir)
    log_expr = _expand_meta_dir_expr(spec.log_path)
    workdir_q = shlex.quote(spec.workdir)
    project_q = shlex.quote(spec.project)
    exp_id_q = shlex.quote(spec.exp_id)
    runner_q = shlex.quote(spec.runner)
    runner_ref_q = shlex.quote(spec.runner_ref)

    return f"""#!/usr/bin/env bash
# {WRAPPER_MARKER}: {spec.project}/{spec.exp_id}#{spec.attempt_no}
export EXPLOOM_WRAPPED=1
META_DIR={meta_expr}
LOG_PATH={log_expr}
WORKDIR={workdir_q}
mkdir -p "$META_DIR" "$(dirname "$LOG_PATH")"

_exploom_json_escape() {{
  printf '%s' "$1" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g' | tr '\\n' ' '
}}

_exploom_sha256() {{
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{{print $1}}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{{print $1}}'
  else
    printf ''
  fi
}}

_EXPLOOM_EXIT_WRITTEN=0
_exploom_write_exit() {{
  local rc=$?
  if [ "$_EXPLOOM_EXIT_WRITTEN" -eq 1 ]; then
    return
  fi
  _EXPLOOM_EXIT_WRITTEN=1
  local ended_at
  ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{{"exit_code": %d, "ended_at": "%s"}}\\n' "$rc" "$ended_at" > "$META_DIR/exit.json.tmp"
  mv -f "$META_DIR/exit.json.tmp" "$META_DIR/exit.json"
}}

trap '_exploom_write_exit' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

cd "$WORKDIR" || exit 127

GIT_COMMIT="null"
GIT_BRANCH="null"
DIFF_HASH="null"
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  _hc="$(git rev-parse HEAD 2>/dev/null || true)"
  _br="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [ -n "$_hc" ]; then
    GIT_COMMIT="\\"$(_exploom_json_escape "$_hc")\\""
  fi
  if [ -n "$_br" ]; then
    GIT_BRANCH="\\"$(_exploom_json_escape "$_br")\\""
  fi
  _dh="$(git diff HEAD 2>/dev/null | _exploom_sha256)"
  if [ -n "$_dh" ]; then
    DIFF_HASH="\\"$_dh\\""
  fi
  git diff HEAD 2>/dev/null | head -c {spec.max_patch_bytes} > "$META_DIR/dirty.patch" || true
fi

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "$META_DIR/meta.json.tmp" <<__EXPLOOM_META_EOF__
{{
  "project": "$(_exploom_json_escape {project_q})",
  "exp_id": "$(_exploom_json_escape {exp_id_q})",
  "attempt_no": {spec.attempt_no},
  "pid": $$,
  "started_at": "$STARTED_AT",
  "workdir": "$(_exploom_json_escape "$WORKDIR")",
  "log_path": "$(_exploom_json_escape "$LOG_PATH")",
  "runner": "$(_exploom_json_escape {runner_q})",
  "runner_ref": "$(_exploom_json_escape {runner_ref_q})",
  "git_commit": $GIT_COMMIT,
  "git_branch": $GIT_BRANCH,
  "diff_hash": $DIFF_HASH
}}
__EXPLOOM_META_EOF__
mv -f "$META_DIR/meta.json.tmp" "$META_DIR/meta.json"

set +e
(
{spec.run_cmd}
) >> "$LOG_PATH" 2>&1
rc=$?
exit "$rc"
"""


def encode_script_bootstrap(spec: AttemptSpec, launch_cmd: str) -> str:
    """Wrap script installation ($META_DIR/run.sh via base64) + runner launch command.

    Using base64 completely avoids nested shell quoting issues across SSH + tmux/nohup.
    """
    script = build_run_script(spec)
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    meta_expr = _expand_meta_dir_expr(spec.meta_dir)
    return (
        f"# {WRAPPER_MARKER}\n"
        f"META_DIR={meta_expr}; mkdir -p \"$META_DIR\" && "
        f"printf '%s' {shlex.quote(b64)} | base64 -d > \"$META_DIR/run.sh\" && "
        f"chmod +x \"$META_DIR/run.sh\" && {launch_cmd}"
    )


def build_common_probe_snippet(
    attempt: dict[str, Any],
    alive_check_bash: str,
    *,
    include_patch: bool = False,
    log_tail_lines: int = 80,
) -> str:
    """Build a self-contained bash probe snippet outputting structured key/value sections.

    Output sections:
      ALIVE:0|1
      LOG_MTIME:<epoch_seconds or empty>
      META_BEGIN ... META_END
      EXIT_BEGIN ... EXIT_END
      TAIL_BEGIN ... TAIL_END   (only when not alive or exit.json exists)
      PATCH_BEGIN ... PATCH_END (only when include_patch is True and dirty.patch is non-empty)
    """
    meta_expr = _expand_meta_dir_expr(str(attempt["meta_dir"]))
    log_path = attempt.get("log_path") or f"{attempt['meta_dir']}/run.log"
    log_expr = _expand_meta_dir_expr(str(log_path))
    patch_block = ""
    if include_patch:
        patch_block = """
echo "PATCH_BEGIN"
if [ -s "$META_DIR/dirty.patch" ]; then
  cat "$META_DIR/dirty.patch"
fi
echo "PATCH_END"
"""

    return f"""(
META_DIR={meta_expr}
LOG_PATH={log_expr}
ALIVE=0
if {alive_check_bash}; then
  ALIVE=1
fi
echo "ALIVE:$ALIVE"
MTIME=""
if [ -e "$LOG_PATH" ]; then
  MTIME="$(stat -c %Y "$LOG_PATH" 2>/dev/null || stat -f %m "$LOG_PATH" 2>/dev/null || true)"
fi
echo "LOG_MTIME:$MTIME"
echo "META_BEGIN"
if [ -f "$META_DIR/meta.json" ]; then
  cat "$META_DIR/meta.json"
fi
echo "META_END"
echo "EXIT_BEGIN"
if [ -f "$META_DIR/exit.json" ]; then
  cat "$META_DIR/exit.json"
fi
echo "EXIT_END"
echo "TAIL_BEGIN"
if [ "$ALIVE" -eq 0 ] || [ -f "$META_DIR/exit.json" ]; then
  if [ -f "$LOG_PATH" ]; then
    tail -n {log_tail_lines} "$LOG_PATH" 2>/dev/null || true
  fi
fi
echo "TAIL_END"
{patch_block.strip()}
)"""


class Runner(ABC):
    """Abstract runner interface: only 2 methods required."""

    name: str

    @abstractmethod
    def build_wrapped_cmd(self, spec: AttemptSpec) -> str:
        """Build the command string that launches this attempt with Exploom metadata."""

    @abstractmethod
    def build_probe_cmd(
        self,
        attempt: dict[str, Any],
        *,
        include_patch: bool = False,
        log_tail_lines: int = 80,
    ) -> str:
        """Build the read-only probe command for checking liveness, metadata, and logs."""
