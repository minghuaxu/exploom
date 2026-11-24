"""Tests for pure-stdlib SessionStart and PostToolUse(Bash) hooks."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from exploom.service import ExploomService

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_START_SCRIPT = REPO_ROOT / "hooks" / "scripts" / "session_start.py"
POST_BASH_SCRIPT = REPO_ROOT / "hooks" / "scripts" / "post_bash_check.py"


def _imported_top_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def test_hooks_use_only_python_stdlib() -> None:
    stdlib = sys.stdlib_module_names
    for script in (SESSION_START_SCRIPT, POST_BASH_SCRIPT):
        imported = _imported_top_modules(script)
        non_stdlib = imported - stdlib
        assert non_stdlib == set(), f"{script.name} imported non-stdlib modules: {non_stdlib}"


def test_session_start_silent_when_db_missing(tmp_path: Path) -> None:
    home_dir = tmp_path / "empty_exploom"
    env = {**os.environ, "EXPLOOM_HOME": str(home_dir)}
    proc = subprocess.run(
        [sys.executable, str(SESSION_START_SCRIPT)],
        input=json.dumps({"cwd": str(tmp_path)}),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not (home_dir / "registry.db").exists()


def test_session_start_injects_compact_card_and_is_fast(tmp_path: Path) -> None:
    home_dir = tmp_path / ".exploom"
    work_dir = tmp_path / "myproj"
    work_dir.mkdir()

    svc = ExploomService(home_dir=home_dir)
    svc.init_project("myproj", cwd=work_dir)

    # E001: running + stale (2 hours silent)
    svc.plan_experiment(action="create", project="myproj", title="sft_long_run")
    svc.start_attempt(
        exp_id="E001",
        server="gpu-1",
        workdir="/data/myproj",
        resource="gpu:0,1",
        run_cmd="python train.py",
        project="myproj",
    )
    two_hours_ago = (
        (datetime.now(timezone.utc) - timedelta(hours=2))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    svc.update_attempt(
        exp_id="E001",
        attempt_no=1,
        log_mtime=two_hours_ago,
        project="myproj",
    )

    # E002: failed_oom (resumable)
    svc.plan_experiment(action="create", project="myproj", title="sft_big_bs")
    svc.start_attempt(
        exp_id="E002",
        server="gpu-2",
        workdir="/data/myproj",
        run_cmd="python train.py --bs 256",
        project="myproj",
    )
    svc.update_attempt(exp_id="E002", attempt_no=1, status="failed_oom", project="myproj")

    # E003: done
    svc.plan_experiment(action="create", project="myproj", title="data_prep")
    svc.start_attempt(
        exp_id="E003",
        server="local",
        workdir=str(work_dir),
        run_cmd="python prep.py",
        project="myproj",
    )
    svc.update_attempt(exp_id="E003", attempt_no=1, status="done", project="myproj")

    # E004: planned (so we have 4 exps, verifying Recent caps at 3)
    svc.plan_experiment(action="create", project="myproj", title="next_idea")

    # Direct function speed check (< 30ms)
    spec = importlib.util.spec_from_file_location("session_start_mod", SESSION_START_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    t0 = time.perf_counter()
    card_direct = mod.build_status_card(home_dir, work_dir)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert card_direct is not None
    assert elapsed_ms < 30.0, f"SessionStart query took {elapsed_ms:.1f}ms (expected <30ms)"

    # End-to-end subprocess check
    env = {**os.environ, "EXPLOOM_HOME": str(home_dir)}
    proc = subprocess.run(
        [sys.executable, str(SESSION_START_SCRIPT)],
        input=json.dumps({"cwd": str(work_dir)}),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "[Exploom] project=myproj" in ctx
    assert "Running: E001#1@gpu-1/gpu:0,1 (stale 2h)" in ctx
    assert "Resumable: E002#1 [failed_oom]" in ctx
    assert "Recent:" in ctx
    # Compact <= ~120 tokens
    assert len(ctx) < 500


def _run_post_bash(command: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(POST_BASH_SCRIPT)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0
    return proc.stdout.strip()


def test_post_bash_check_triggers_on_untracked_remote_bg_jobs() -> None:
    trigger_cmds = [
        'ssh gpu-1 "tmux new -d -s train python train.py"',
        'ssh gpu-1 "nohup python train.py > run.log 2>&1 &"',
        'ssh cluster "sbatch scripts/train.sbatch"',
        'ssh gpu-2 "screen -dmS exp1 python train.py"',
        'ssh gpu-1 "cd /work && python train.py &"',
    ]
    for cmd in trigger_cmds:
        out = _run_post_bash(cmd)
        assert out != "", f"Expected reminder for: {cmd}"
        data = json.loads(out)
        assert data["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        assert "exp_attempt" in data["hookSpecificOutput"]["additionalContext"]


def test_post_bash_check_silent_on_daily_and_wrapped_commands(tmp_path: Path) -> None:
    svc = ExploomService(home_dir=tmp_path / ".exploom")
    svc.plan_experiment(action="create", project="demo", title="wrapped_run")
    att = svc.start_attempt(
        exp_id="E001",
        server="gpu-1",
        workdir="/data/demo",
        run_cmd="python train.py",
        project="demo",
    )

    silent_cmds = [
        "ssh gpu-1 nvidia-smi",
        'ssh gpu-1 "ls -la /data && cat train.log"',
        'ssh gpu-1 "python check_env.py 2>&1"',
        'ssh gpu-1 "tmux ls"',
        'ssh gpu-1 "tmux has-session -t exp_demo_E001_1"',
        'ssh gpu-1 "screen -ls"',
        "nohup python local_train.py > run.log 2>&1 &",
        att["ssh_cmd"],  # Exploom-wrapped ssh command MUST be silent!
    ]
    for cmd in silent_cmds:
        out = _run_post_bash(cmd)
        assert out == "", f"Expected silence for: {cmd}, got: {out}"
