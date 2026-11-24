"""Tests for Phase 2 runners (ssh-screen, ssh-slurm, ssh-docker) and example config."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from exploom.config import load_config
from exploom.runners import SUPPORTED_RUNNERS, AttemptSpec, get_runner
from exploom.service import ExploomService
from exploom.ssh import CommandOutput, parse_probe_output

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_all_seven_runners_registered() -> None:
    for name in (
        "ssh-tmux",
        "ssh-nohup",
        "ssh-screen",
        "ssh-slurm",
        "ssh-docker",
        "local",
        "custom",
    ):
        assert name in SUPPORTED_RUNNERS
        assert get_runner(name).name == name


def test_screen_runner_wrap_and_probe() -> None:
    runner = get_runner("ssh-screen")
    spec = AttemptSpec(
        project="proj",
        exp_id="E001",
        attempt_no=1,
        server="gpu-1",
        workdir="/data/proj",
        run_cmd="python train.py",
        meta_dir="$HOME/.cache/exploom/proj/E001_1",
        log_path="$HOME/.cache/exploom/proj/E001_1/run.log",
        runner="ssh-screen",
        runner_ref="exp_proj_E001_1",
    )
    wrapped = runner.build_wrapped_cmd(spec)
    assert "exploom-wrapper" in wrapped
    assert "screen -dmS exp_proj_E001_1" in wrapped

    probe = runner.build_probe_cmd(
        {
            "meta_dir": spec.meta_dir,
            "log_path": spec.log_path,
            "runner_ref": spec.runner_ref,
        }
    )
    assert "screen -ls" in probe
    assert "exp_proj_E001_1" in probe


def test_slurm_runner_wrap_probe_and_sacct_classification(tmp_path: Path) -> None:
    runner = get_runner("ssh-slurm")
    spec = AttemptSpec(
        project="hpc",
        exp_id="E001",
        attempt_no=1,
        server="cluster",
        workdir="/scratch/hpc",
        run_cmd="python train.py",
        meta_dir="/scratch/u/.exploom_meta/hpc/E001_1",
        log_path="/scratch/u/.exploom_meta/hpc/E001_1/run.log",
        runner="ssh-slurm",
        runner_ref="exp_hpc_E001_1",
    )
    wrapped = runner.build_wrapped_cmd(spec)
    assert "sbatch --parsable" in wrapped
    assert "slurm_job_id" in wrapped

    probe_cmd = runner.build_probe_cmd(
        {
            "meta_dir": spec.meta_dir,
            "log_path": spec.log_path,
            "runner_ref": spec.runner_ref,
        }
    )
    assert "squeue -h -j" in probe_cmd
    assert "sacct -n -P -j" in probe_cmd
    assert "SLURM_SACCT:" in probe_cmd

    # Direct parse_probe_output check
    parsed = parse_probe_output(
        "ALIVE:0\nLOG_MTIME:\nMETA_BEGIN\nMETA_END\nEXIT_BEGIN\nEXIT_END\n"
        "TAIL_BEGIN\nTAIL_END\nSLURM_SACCT:OUT_OF_MEMORY|0:125\n"
    )
    assert parsed.slurm_state == "OUT_OF_MEMORY"
    assert parsed.slurm_exit_code == "0:125"

    # End-to-end sync classification across Slurm terminal states (even without exit.json)
    cases = [
        ("OUT_OF_MEMORY|0:125", "failed_oom"),
        ("TIMEOUT|0:15", "failed_quota"),
        ("PREEMPTED|0:0", "failed_quota"),
        ("CANCELLED by 1000|0:0", "aborted"),
        ("COMPLETED|0:0", "done"),
    ]
    for idx, (sacct_val, expected_status) in enumerate(cases, start=1):
        home_dir = tmp_path / f"slurm_case_{idx}"

        def make_exec(val: str):
            def _exec(
                server: str, script: str, timeout: int, connect_timeout: int
            ) -> CommandOutput:
                return CommandOutput(
                    returncode=0,
                    stdout=(
                        "===EXPLOOM_ATTEMPT_START:0===\n"
                        "ALIVE:0\n"
                        "LOG_MTIME:1763690000\n"
                        "META_BEGIN\n{}\nMETA_END\n"
                        "EXIT_BEGIN\nEXIT_END\n"
                        "TAIL_BEGIN\nTAIL_END\n"
                        "PATCH_BEGIN\nPATCH_END\n"
                        f"SLURM_SACCT:{val}\n"
                        "===EXPLOOM_ATTEMPT_END:0===\n"
                    ),
                    stderr="",
                )

            return _exec

        svc = ExploomService(home_dir=home_dir, executor=make_exec(sacct_val))
        svc.plan_experiment(action="create", project="hpc", title=f"job_{idx}")
        svc.start_attempt(
            exp_id="E001",
            server="cluster",
            workdir="/scratch/hpc",
            run_cmd="python train.py",
            runner="ssh-slurm",
            project="hpc",
        )
        res = svc.sync_attempts(project="hpc")
        assert res["changed"][0]["new_status"] == expected_status, (
            f"Expected {expected_status} for sacct={sacct_val}, got {res['changed']}"
        )


def test_docker_runner_shared_meta_dir_and_container_resolution(tmp_path: Path) -> None:
    home_dir = tmp_path / ".exploom"
    home_dir.mkdir()
    (home_dir / "config.toml").write_text(
        """
[servers.gpu-docker-1]
meta_dir = "/data/.exploom_meta"
docker_container = "trainer_box"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    svc = ExploomService(home_dir=home_dir)
    svc.plan_experiment(action="create", project="vision", title="vit_sft")
    att = svc.start_attempt(
        exp_id="E001",
        server="gpu-docker-1",
        workdir="/workspace/vision",
        run_cmd="python train.py",
        runner="ssh-docker",
        project="vision",
    )
    assert att["meta_dir"] == "/data/.exploom_meta/vision/E001_1"
    assert att["runner_ref"] == "trainer_box:exp_vision_E001_1"
    assert "docker exec -d trainer_box" in att["wrapped_cmd"]
    assert "/data/.exploom_meta/vision/E001_1" in att["wrapped_cmd"]

    # Probe checks docker inspect + docker exec kill -0 + host shared meta_dir
    docker_runner = get_runner("ssh-docker")
    probe = docker_runner.build_probe_cmd(
        {
            "meta_dir": att["meta_dir"],
            "log_path": att["log_path"],
            "runner_ref": att["runner_ref"],
            "resource": None,
        }
    )
    assert "docker inspect -f '{{.State.Running}}' trainer_box" in probe
    assert "docker exec trainer_box kill -0" in probe
    assert "/data/.exploom_meta/vision/E001_1" in probe

    # Resource override ('docker:other_box') works without server config
    att2 = svc.start_attempt(
        exp_id="E001",
        server="gpu-other",
        workdir="/workspace/vision",
        resource="docker:other_box",
        run_cmd="python train.py",
        runner="ssh-docker",
        project="vision",
    )
    assert att2["runner_ref"] == "other_box:exp_vision_E001_2"
    assert "docker exec -d other_box" in att2["wrapped_cmd"]


def test_example_config_toml_loads_cleanly(tmp_path: Path) -> None:
    example = REPO_ROOT / "examples" / "config.toml"
    assert example.is_file()
    home_dir = tmp_path / ".exploom"
    home_dir.mkdir()
    shutil.copy2(example, home_dir / "config.toml")

    cfg = load_config(home_dir=home_dir)
    assert cfg.stale_threshold_minutes == 45
    assert cfg.ssh_connect_timeout == 5
    assert cfg.default_runner == "ssh-tmux"
    assert len(cfg.custom_failure_rules) == 2
    assert cfg.server_meta_dirs["gpu-docker-1"] == "/data/.exploom_meta"
    assert cfg.server_docker_containers["gpu-docker-1"] == "trainer_box"
