"""Tests for SSH batch probing, sync, log tailing, drift detection, and CLI ops."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from exploom.cli import app
from exploom.service import ExploomService
from exploom.ssh import CommandOutput, probe_attempts_concurrent

cli_runner = CliRunner()


def _init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@exploom.dev"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Exploom Test"], cwd=repo_dir, check=True)
    (repo_dir / "train.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "train.py"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=repo_dir, check=True, capture_output=True)


def test_real_local_sync_classify_tail_and_drift_rerun(tmp_path: Path) -> None:
    """End-to-end M2 test executing real wrapper scripts locally in a Git repo."""
    home = tmp_path / "exploom_home"
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    svc = ExploomService(home_dir=home)
    svc.plan_experiment(title="oom_test", project="p1")
    started = svc.start_attempt(
        exp_id="E001",
        server="local",
        workdir=str(repo),
        run_cmd='echo "step 10"; echo "torch.cuda.OutOfMemoryError: CUDA out of memory"; exit 1',
        resume_cmd='echo "resumed ok"; exit 0',
        runner="custom",
        project="p1",
    )

    # Run the wrapped command locally (simulating ZCode Bash execution)
    subprocess.run(["bash", "-c", started["wrapped_cmd"]], check=False)

    # Sync should detect exit_code=1 + OOM log -> failed_oom, and capture git_commit & diff_hash
    sync_res = svc.sync_attempts(project="p1")
    assert sync_res["synced_count"] == 1
    assert len(sync_res["changed"]) == 1
    assert sync_res["changed"][0]["new_status"] == "failed_oom"

    att1 = svc.db.get_attempt("p1", "E001", 1)
    assert att1 is not None
    assert att1["status"] == "failed_oom"
    assert att1["git_commit"] is not None
    assert att1["diff_hash"] is not None

    # tail_log should return the captured log
    log_res = svc.tail_log("E001", lines=10, project="p1")
    assert log_res["error"] is None
    assert "CUDA out of memory" in log_res["content"]

    # prepare_rerun with clean repo -> unblocked and selects resume_cmd
    prep_ok = svc.prepare_rerun("E001", use_resume=True, project="p1")
    assert prep_ok["blocked"] is False
    assert prep_ok["used_resume_cmd"] is True
    assert prep_ok["attempt"]["attempt_no"] == 2

    # Execute attempt #2 and sync -> done
    subprocess.run(["bash", "-c", prep_ok["attempt"]["wrapped_cmd"]], check=False)
    sync2 = svc.sync_attempts(project="p1")
    assert sync2["changed"][0]["new_status"] == "done"

    # Now modify train.py to trigger code drift!
    (repo / "train.py").write_text("print('v2_modified')\n", encoding="utf-8")

    prep_blocked = svc.prepare_rerun("E001", use_resume=True, force_on_drift=False, project="p1")
    assert prep_blocked["blocked"] is True
    assert prep_blocked["reason"] == "code_drift"
    assert prep_blocked["drift"]["diff_changed"] is True
    assert prep_blocked["attempt"] is None

    # Forcing overrides drift block and creates attempt #3
    prep_forced = svc.prepare_rerun("E001", use_resume=True, force_on_drift=True, project="p1")
    assert prep_forced["blocked"] is False
    assert prep_forced["attempt"]["attempt_no"] == 3


def test_concurrent_ssh_batch_probe_and_unreachable_guardrail(tmp_path: Path) -> None:
    """Verify per-server batching (1 SSH call per server) and ssh_unreachable status preservation."""
    calls_by_server: dict[str, int] = {}

    def fake_executor(server: str, script: str, timeout: int, connect_timeout: int) -> CommandOutput:
        calls_by_server[server] = calls_by_server.get(server, 0) + 1
        if server == "gpu-down":
            return CommandOutput(
                returncode=255,
                stdout="",
                stderr="ssh: connect to host gpu-down: Operation timed out",
                unreachable=True,
                error="ssh_unreachable",
            )
        # gpu-1 has 2 attempts batched into 1 SSH call
        assert "===EXPLOOM_ATTEMPT_START:0===" in script
        assert "===EXPLOOM_ATTEMPT_START:1===" in script
        stdout = "\n".join(
            [
                "===EXPLOOM_ATTEMPT_START:0===",
                "ALIVE:0",
                "LOG_MTIME:1763712000",
                "META_BEGIN",
                '{"git_commit": "abc123", "git_branch": "main", "diff_hash": "h1"}',
                "META_END",
                "EXIT_BEGIN",
                '{"exit_code": 0, "ended_at": "2026-11-21T12:00:00Z"}',
                "EXIT_END",
                "TAIL_BEGIN",
                "training complete",
                "TAIL_END",
                "PATCH_BEGIN",
                "diff --git a/a.py b/a.py",
                "PATCH_END",
                "===EXPLOOM_ATTEMPT_END:0===",
                "===EXPLOOM_ATTEMPT_START:1===",
                "ALIVE:0",
                "LOG_MTIME:1763712000",
                "META_BEGIN",
                '{"git_commit": "abc123", "git_branch": "main", "diff_hash": "h1"}',
                "META_END",
                "EXIT_BEGIN",
                '{"exit_code": 1, "ended_at": "2026-11-21T12:01:00Z"}',
                "EXIT_END",
                "TAIL_BEGIN",
                "OSError: [Errno 28] No space left on device",
                "TAIL_END",
                "PATCH_BEGIN",
                "PATCH_END",
                "===EXPLOOM_ATTEMPT_END:1===",
            ]
        )
        return CommandOutput(returncode=0, stdout=stdout, stderr="")

    svc = ExploomService(home_dir=tmp_path / "home", executor=fake_executor)
    svc.plan_experiment(title="e1", project="p1")
    svc.plan_experiment(title="e2", project="p1")
    svc.plan_experiment(title="e3", project="p1")
    svc.start_attempt(exp_id="E001", server="gpu-1", workdir="/w1", run_cmd="cmd1", project="p1")
    svc.start_attempt(exp_id="E002", server="gpu-1", workdir="/w2", run_cmd="cmd2", project="p1")
    svc.start_attempt(exp_id="E003", server="gpu-down", workdir="/w3", run_cmd="cmd3", project="p1")

    res = svc.sync_attempts(project="p1")
    # Only 1 SSH call per server (2 attempts on gpu-1 batched together!)
    assert calls_by_server == {"gpu-1": 1, "gpu-down": 1}
    assert res["synced_count"] == 3
    assert len(res["changed"]) == 2
    assert len(res["errors"]) == 1
    assert res["errors"][0]["sync_error"] == "ssh_unreachable"

    # Verify E001 done + patch saved, E002 failed_disk, E003 STILL running with sync_error
    a1 = svc.db.get_attempt("p1", "E001", 1)
    a2 = svc.db.get_attempt("p1", "E002", 1)
    a3 = svc.db.get_attempt("p1", "E003", 1)
    assert a1 is not None and a1["status"] == "done" and a1["patch_path"] is not None
    assert a2 is not None and a2["status"] == "failed_disk"
    assert a3 is not None and a3["status"] == "running" and a3["sync_error"] == "ssh_unreachable"


def test_cli_sync_log_rerun_local(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "exploom_home"
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setenv("EXPLOOM_HOME", str(home))
    monkeypatch.chdir(repo)

    r = cli_runner.invoke(app, ["init", "cli-m2", "--json"])
    assert r.exit_code == 0

    r = cli_runner.invoke(app, ["plan", "--title", "local_job", "--json"])
    assert r.exit_code == 0

    r = cli_runner.invoke(
        app,
        [
            "attempt",
            "start",
            "E001",
            "--server",
            "local",
            "--workdir",
            str(repo),
            "--cmd",
            'echo "hello from exploom"; exit 0',
            "--runner",
            "custom",
            "--json",
        ],
    )
    assert r.exit_code == 0
    att = json.loads(r.output)
    subprocess.run(["bash", "-c", att["wrapped_cmd"]], check=True)

    r = cli_runner.invoke(app, ["sync", "--json"])
    assert r.exit_code == 0, r.output
    sync_data = json.loads(r.output)
    assert sync_data["changed"][0]["new_status"] == "done"

    r = cli_runner.invoke(app, ["log", "E001", "-n", "20", "--json"])
    assert r.exit_code == 0, r.output
    assert "hello from exploom" in json.loads(r.output)["content"]

    r = cli_runner.invoke(app, ["rerun", "E001", "--dry-run", "--json"])
    assert r.exit_code == 0, r.output
    rerun_data = json.loads(r.output)
    assert rerun_data["blocked"] is False
    assert rerun_data["attempt"]["attempt_no"] == 2
