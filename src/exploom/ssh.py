"""Concurrent read-only SSH engine: batch probing, drift detection, and log tailing."""

from __future__ import annotations

import json
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from exploom.runners import get_runner
from exploom.runners.base import _expand_meta_dir_expr

MAX_LOG_TAIL_LINES = 200
DEFAULT_CONTROL_PATH = "/tmp/exploom-ssh-%C"


@dataclass
class CommandOutput:
    """Result of executing a command locally or over SSH."""

    returncode: int
    stdout: str
    stderr: str
    unreachable: bool = False
    error: str | None = None


@dataclass
class ProbeResult:
    """Parsed probe snapshot for a single attempt."""

    alive: bool = False
    meta: dict[str, Any] | None = None
    exit_info: dict[str, Any] | None = None
    log_mtime: str | None = None
    log_tail: str = ""
    dirty_patch: str | None = None
    sync_error: str | None = None
    slurm_state: str | None = None
    slurm_exit_code: str | None = None


@dataclass
class DriftResult:
    """Result of comparing target server:workdir Git state against an attempt's snapshot."""

    drifted: bool
    commit_changed: bool = False
    diff_changed: bool = False
    expected_commit: str | None = None
    current_commit: str | None = None
    expected_diff_hash: str | None = None
    current_diff_hash: str | None = None
    current_branch: str | None = None
    is_git: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "drifted": self.drifted,
            "commit_changed": self.commit_changed,
            "diff_changed": self.diff_changed,
            "expected_commit": self.expected_commit,
            "current_commit": self.current_commit,
            "expected_diff_hash": self.expected_diff_hash,
            "current_diff_hash": self.current_diff_hash,
            "current_branch": self.current_branch,
            "is_git": self.is_git,
            "error": self.error,
        }


@dataclass
class LogTailResult:
    """Result of fetching the tail of a remote or local log file."""

    log_path: str
    lines: int
    content: str = ""
    error: str | None = None


CommandExecutor = Callable[[str, str, int, int], CommandOutput]


def build_ssh_args(
    server: str,
    *,
    connect_timeout: int = 5,
    control_path: str = DEFAULT_CONTROL_PATH,
) -> list[str]:
    """Build non-interactive SSH arguments with ControlMaster multiplexing."""
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=60",
        "-o",
        f"ControlPath={control_path}",
        server,
    ]


def run_shell_on_host(
    server: str,
    script: str,
    timeout: int = 20,
    connect_timeout: int = 5,
) -> CommandOutput:
    """Execute a bash script on 'local' or a remote SSH host via stdin."""
    if server == "local":
        cmd = ["bash", "-c", script]
        stdin_data = None
    else:
        cmd = [*build_ssh_args(server, connect_timeout=connect_timeout), "bash -s"]
        stdin_data = script

    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandOutput(
            returncode=255,
            stdout="",
            stderr="SSH command timed out",
            unreachable=True,
            error="ssh_unreachable",
        )
    except FileNotFoundError as exc:
        return CommandOutput(
            returncode=127,
            stdout="",
            stderr=str(exc),
            unreachable=True,
            error="ssh_not_found",
        )

    # OpenSSH returns exit code 255 on connection/transport errors
    if server != "local" and proc.returncode == 255:
        return CommandOutput(
            returncode=255,
            stdout=proc.stdout,
            stderr=proc.stderr,
            unreachable=True,
            error="ssh_unreachable",
        )

    return CommandOutput(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        unreachable=False,
        error=None,
    )


def _extract_section(lines: list[str], start_tag: str, end_tag: str) -> str | None:
    try:
        start_idx = lines.index(start_tag)
        end_idx = lines.index(end_tag, start_idx + 1)
    except ValueError:
        return None
    return "\n".join(lines[start_idx + 1 : end_idx])


def _epoch_to_iso(epoch_str: str) -> str | None:
    text = epoch_str.strip()
    if not text or not text.isdigit():
        return None
    dt = datetime.fromtimestamp(int(text), tz=timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_probe_output(raw: str) -> ProbeResult:
    """Parse structured probe output produced by build_common_probe_snippet."""
    lines = raw.splitlines()
    alive = False
    log_mtime: str | None = None
    slurm_state: str | None = None
    slurm_exit_code: str | None = None

    for line in lines:
        if line.startswith("ALIVE:"):
            alive = line.split(":", 1)[1].strip() == "1"
        elif line.startswith("LOG_MTIME:"):
            log_mtime = _epoch_to_iso(line.split(":", 1)[1])
        elif line.startswith("SLURM_SACCT:"):
            sacct_raw = line.split(":", 1)[1].strip()
            if "|" in sacct_raw:
                s_state, s_code = sacct_raw.split("|", 1)
                slurm_state = s_state.strip() or None
                slurm_exit_code = s_code.strip() or None
            elif sacct_raw:
                slurm_state = sacct_raw

    meta: dict[str, Any] | None = None
    meta_text = _extract_section(lines, "META_BEGIN", "META_END")
    if meta_text and meta_text.strip():
        try:
            meta = json.loads(meta_text)
        except json.JSONDecodeError:
            meta = None

    exit_info: dict[str, Any] | None = None
    exit_text = _extract_section(lines, "EXIT_BEGIN", "EXIT_END")
    if exit_text and exit_text.strip():
        try:
            exit_info = json.loads(exit_text)
        except json.JSONDecodeError:
            exit_info = None

    tail_text = _extract_section(lines, "TAIL_BEGIN", "TAIL_END") or ""
    patch_text = _extract_section(lines, "PATCH_BEGIN", "PATCH_END")
    dirty_patch = patch_text if (patch_text and patch_text.strip()) else None

    return ProbeResult(
        alive=alive,
        meta=meta,
        exit_info=exit_info,
        log_mtime=log_mtime,
        log_tail=tail_text,
        dirty_patch=dirty_patch,
        sync_error=None,
        slurm_state=slurm_state,
        slurm_exit_code=slurm_exit_code,
    )


def _probe_server_batch(
    server: str,
    attempts: list[dict[str, Any]],
    *,
    connect_timeout: int,
    executor: CommandExecutor,
) -> dict[tuple[str, str, int], ProbeResult]:
    """Batch-probe all attempts belonging to a single server in one SSH round-trip."""
    script_parts: list[str] = []
    for idx, att in enumerate(attempts):
        runner = get_runner(str(att.get("runner") or "ssh-tmux"))
        include_patch = not bool(att.get("patch_path"))
        snippet = runner.build_probe_cmd(att, include_patch=include_patch)
        script_parts.append(f'echo "===EXPLOOM_ATTEMPT_START:{idx}==="')
        script_parts.append(snippet)
        script_parts.append(f'echo "===EXPLOOM_ATTEMPT_END:{idx}==="')

    batch_script = "\n".join(script_parts) + "\n"
    out = executor(server, batch_script, max(20, connect_timeout * 4), connect_timeout)

    results: dict[tuple[str, str, int], ProbeResult] = {}
    if out.unreachable:
        err = out.error or "ssh_unreachable"
        for att in attempts:
            key = (att["project"], att["exp_id"], int(att["attempt_no"]))
            results[key] = ProbeResult(alive=False, sync_error=err)
        return results

    lines = out.stdout.splitlines()
    for idx, att in enumerate(attempts):
        key = (att["project"], att["exp_id"], int(att["attempt_no"]))
        block = _extract_section(
            lines,
            f"===EXPLOOM_ATTEMPT_START:{idx}===",
            f"===EXPLOOM_ATTEMPT_END:{idx}===",
        )
        if block is None:
            results[key] = ProbeResult(alive=False, sync_error="probe_parse_error")
        else:
            results[key] = parse_probe_output(block)
    return results


def probe_attempts_concurrent(
    attempts: list[dict[str, Any]],
    *,
    connect_timeout: int = 5,
    max_workers: int = 8,
    executor: CommandExecutor = run_shell_on_host,
) -> dict[tuple[str, str, int], ProbeResult]:
    """Concurrently batch-probe attempts grouped by server."""
    if not attempts:
        return {}

    by_server: dict[str, list[dict[str, Any]]] = {}
    for att in attempts:
        by_server.setdefault(str(att["server"]), []).append(att)

    all_results: dict[tuple[str, str, int], ProbeResult] = {}
    worker_count = max(1, min(max_workers, len(by_server)))

    if worker_count == 1:
        for server, batch in by_server.items():
            all_results.update(
                _probe_server_batch(
                    server, batch, connect_timeout=connect_timeout, executor=executor
                )
            )
        return all_results

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(
                _probe_server_batch,
                server,
                batch,
                connect_timeout=connect_timeout,
                executor=executor,
            ): server
            for server, batch in by_server.items()
        }
        for fut in as_completed(futures):
            all_results.update(fut.result())

    return all_results


def check_code_drift(
    *,
    server: str,
    workdir: str,
    expected_commit: str | None,
    expected_diff_hash: str | None,
    connect_timeout: int = 5,
    executor: CommandExecutor = run_shell_on_host,
) -> DriftResult:
    """Inspect current (git_commit, diff_hash) on server:workdir and compare with expected."""
    workdir_q = shlex.quote(workdir)
    script = f"""
_exploom_sha256() {{
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{{print $1}}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{{print $1}}'
  else
    printf ''
  fi
}}
if ! cd {workdir_q} 2>/dev/null; then
  echo "WORKDIR_MISSING:1"
  exit 0
fi
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "IS_GIT:1"
  echo "COMMIT:$(git rev-parse HEAD 2>/dev/null || true)"
  echo "BRANCH:$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  echo "DIFF_HASH:$(git diff HEAD 2>/dev/null | _exploom_sha256)"
else
  echo "IS_GIT:0"
fi
"""
    out = executor(server, script, max(15, connect_timeout * 3), connect_timeout)
    if out.unreachable:
        return DriftResult(
            drifted=False,
            expected_commit=expected_commit,
            expected_diff_hash=expected_diff_hash,
            error=out.error or "ssh_unreachable",
        )

    kv: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            kv[k.strip()] = v.strip()

    if kv.get("WORKDIR_MISSING") == "1":
        return DriftResult(
            drifted=True,
            expected_commit=expected_commit,
            expected_diff_hash=expected_diff_hash,
            is_git=False,
            error=f"Workdir not found on {server}: {workdir}",
        )

    is_git = kv.get("IS_GIT") == "1"
    current_commit = kv.get("COMMIT") or None
    current_branch = kv.get("BRANCH") or None
    current_diff = kv.get("DIFF_HASH") or None

    # If the original attempt had no git fingerprint, we cannot detect drift
    if expected_commit is None and expected_diff_hash is None:
        return DriftResult(
            drifted=False,
            expected_commit=None,
            current_commit=current_commit,
            expected_diff_hash=None,
            current_diff_hash=current_diff,
            current_branch=current_branch,
            is_git=is_git,
        )

    commit_changed = expected_commit != current_commit
    diff_changed = (
        expected_diff_hash is not None and expected_diff_hash != current_diff
    )
    return DriftResult(
        drifted=commit_changed or diff_changed,
        commit_changed=commit_changed,
        diff_changed=diff_changed,
        expected_commit=expected_commit,
        current_commit=current_commit,
        expected_diff_hash=expected_diff_hash,
        current_diff_hash=current_diff,
        current_branch=current_branch,
        is_git=is_git,
    )


def fetch_log_tail(
    *,
    server: str,
    log_path: str,
    lines: int = 50,
    connect_timeout: int = 5,
    executor: CommandExecutor = run_shell_on_host,
) -> LogTailResult:
    """Fetch the last N lines (clamped to 1..200) of a remote or local log file."""
    clamped = max(1, min(int(lines), MAX_LOG_TAIL_LINES))
    log_expr = _expand_meta_dir_expr(log_path)
    script = f"""
LOG_PATH={log_expr}
if [ ! -f "$LOG_PATH" ]; then
  echo "__EXPLOOM_LOG_MISSING__"
  exit 0
fi
tail -n {clamped} "$LOG_PATH"
"""
    out = executor(server, script, max(15, connect_timeout * 3), connect_timeout)
    if out.unreachable:
        return LogTailResult(
            log_path=log_path,
            lines=clamped,
            content="",
            error=out.error or "ssh_unreachable",
        )
    if out.stdout.strip() == "__EXPLOOM_LOG_MISSING__":
        return LogTailResult(
            log_path=log_path,
            lines=clamped,
            content="",
            error=f"Log file not found on {server}: {log_path}",
        )
    return LogTailResult(
        log_path=log_path,
        lines=clamped,
        content=out.stdout,
        error=None,
    )


def execute_launch_cmd(
    *,
    server: str,
    wrapped_cmd: str,
    connect_timeout: int = 5,
    executor: CommandExecutor = run_shell_on_host,
) -> CommandOutput:
    """Execute a wrapped launch command on server (for human CLI `exploom rerun` only)."""
    return executor(server, wrapped_cmd, max(20, connect_timeout * 4), connect_timeout)
