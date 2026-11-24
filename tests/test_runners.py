"""Tests for Phase 1 runners (ssh-tmux, ssh-nohup, local, custom) and wrapper execution."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from exploom.runners import AttemptSpec, SUPPORTED_RUNNERS, WRAPPER_MARKER, get_runner


def _init_git_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@exploom.dev"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Exploom Test"], cwd=repo_dir, check=True)
    (repo_dir / "train.py").write_text("lr = 1e-4\n", encoding="utf-8")
    subprocess.run(["git", "add", "train.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    ).stdout.strip()
    return commit


def test_runner_registry_and_commands() -> None:
    assert set(SUPPORTED_RUNNERS) == {
        "ssh-tmux",
        "ssh-nohup",
        "ssh-screen",
        "ssh-slurm",
        "ssh-docker",
        "local",
        "custom",
    }
    with pytest.raises(ValueError, match="Unsupported runner"):
        get_runner("nonexistent")

    spec = AttemptSpec(
        project="demo",
        exp_id="E001",
        attempt_no=1,
        server="gpu-1",
        workdir="/work/proj",
        run_cmd="python train.py --prompt \"hello 'world'\"",
        meta_dir="$HOME/.cache/exploom/demo/E001_1",
        log_path="$HOME/.cache/exploom/demo/E001_1/run.log",
        runner="ssh-tmux",
        runner_ref="exp_demo_E001_1",
    )

    tmux_cmd = get_runner("ssh-tmux").build_wrapped_cmd(spec)
    assert WRAPPER_MARKER in tmux_cmd
    assert "tmux new-session -d -s exp_demo_E001_1" in tmux_cmd

    nohup_cmd = get_runner("ssh-nohup").build_wrapped_cmd(spec)
    assert WRAPPER_MARKER in nohup_cmd
    assert "nohup bash" in nohup_cmd

    custom_cmd = get_runner("custom").build_wrapped_cmd(spec)
    assert WRAPPER_MARKER in custom_cmd
    assert 'bash "$META_DIR/run.sh"' in custom_cmd


def test_wrapper_zero_git_pollution_and_metadata_capture(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit = _init_git_repo(repo)
    # Introduce an uncommitted change so dirty.patch and diff_hash are captured
    (repo / "train.py").write_text("lr = 2e-5\n", encoding="utf-8")

    meta_dir = tmp_path / "meta" / "E001_1"
    log_path = meta_dir / "run.log"
    spec = AttemptSpec(
        project="demo",
        exp_id="E001",
        attempt_no=1,
        server="local",
        workdir=str(repo),
        run_cmd='echo "training step 1"; echo "done"',
        meta_dir=str(meta_dir),
        log_path=str(log_path),
        runner="custom",
        runner_ref="exp_demo_E001_1",
    )
    wrapped = get_runner("custom").build_wrapped_cmd(spec)
    proc = subprocess.run(["bash", "-c", wrapped], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr

    # Zero Git pollution: only our intentional train.py modification exists, no untracked files
    git_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert git_status == "M train.py"

    # Metadata, exit.json, dirty.patch, run.log are all in meta_dir
    meta = json.loads((meta_dir / "meta.json").read_text(encoding="utf-8"))
    exit_info = json.loads((meta_dir / "exit.json").read_text(encoding="utf-8"))
    dirty_patch = (meta_dir / "dirty.patch").read_text(encoding="utf-8")
    log_text = log_path.read_text(encoding="utf-8")

    assert meta["git_commit"] == commit
    assert meta["diff_hash"] is not None and len(meta["diff_hash"]) == 64
    assert exit_info["exit_code"] == 0
    assert "+lr = 2e-5" in dirty_patch
    assert "training step 1" in log_text


def test_wrapper_non_git_dir_and_local_background_runner(tmp_path: Path) -> None:
    workdir = tmp_path / "plain_dir"
    workdir.mkdir()
    meta_dir = tmp_path / "meta" / "E002_1"
    log_path = meta_dir / "run.log"

    spec = AttemptSpec(
        project="demo",
        exp_id="E002",
        attempt_no=1,
        server="local",
        workdir=str(workdir),
        run_cmd='echo "CUDA out of memory" >&2; exit 1',
        meta_dir=str(meta_dir),
        log_path=str(log_path),
        runner="local",
        runner_ref="exp_demo_E002_1",
    )
    wrapped = get_runner("local").build_wrapped_cmd(spec)
    proc = subprocess.run(["bash", "-c", wrapped], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr

    # Wait briefly for background process to write exit.json
    for _ in range(30):
        if (meta_dir / "exit.json").is_file():
            break
        time.sleep(0.05)

    meta = json.loads((meta_dir / "meta.json").read_text(encoding="utf-8"))
    exit_info = json.loads((meta_dir / "exit.json").read_text(encoding="utf-8"))
    assert meta["git_commit"] is None
    assert meta["diff_hash"] is None
    assert exit_info["exit_code"] == 1
    assert "CUDA out of memory" in log_path.read_text(encoding="utf-8")
