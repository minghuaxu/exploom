"""Tests for failure classification engine, stale detection, and config loading."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from exploom.classify import check_stale, classify_result, format_duration_minutes
from exploom.config import FailureRule, load_config


def test_classify_success() -> None:
    status, summary = classify_result(0, "all good")
    assert status == "done"
    assert summary is None


def test_classify_builtin_oom_exit_code_and_log() -> None:
    status, summary = classify_result(137, "step 120 loss=1.42")
    assert status == "failed_oom"
    assert summary is not None

    status2, summary2 = classify_result(1, "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB")
    assert status2 == "failed_oom"
    assert "CUDA out of memory" in (summary2 or "")


def test_classify_builtin_disk_quota_net_code() -> None:
    assert classify_result(1, "OSError: [Errno 28] No space left on device")[0] == "failed_disk"
    assert classify_result(1, "Error 429: RateLimit exceeded")[0] == "failed_quota"
    assert classify_result(1, "slurmstepd: error: *** JOB 123 PREEMPTED ***")[0] == "failed_quota"
    assert classify_result(1, "State: TIMEOUT (exit code 0:15)")[0] == "failed_quota"
    assert classify_result(1, "RuntimeError: NCCL communicator timeout after 1800s")[0] == "failed_net"
    assert classify_result(1, "ValueError: Loss is NaN at step 50")[0] == "failed_code"
    assert classify_result(2, "unknown error")[0] == "failed_code"


def test_classify_custom_rule_precedence() -> None:
    custom = [FailureRule(status="failed_quota", pattern=r"MY_CUSTOM_BILLING_LIMIT")]
    status, summary = classify_result(
        1,
        "Traceback (most recent call last):\nMY_CUSTOM_BILLING_LIMIT reached",
        custom_rules=custom,
    )
    assert status == "failed_quota"
    assert "MY_CUSTOM_BILLING_LIMIT" in (summary or "")


def test_error_summary_truncation() -> None:
    long_line = "RuntimeError: " + ("x" * 500)
    status, summary = classify_result(1, long_line)
    assert status == "failed_code"
    assert summary is not None
    assert len(summary) <= 300
    assert summary.endswith("...")


def test_check_stale() -> None:
    now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
    fresh_mtime = (now - timedelta(minutes=10)).isoformat()
    stale_mtime = (now - timedelta(minutes=125)).isoformat()

    assert check_stale("running", fresh_mtime, threshold_minutes=45, now=now) == (False, 10)
    assert check_stale("running", stale_mtime, threshold_minutes=45, now=now) == (True, 125)
    assert check_stale("done", stale_mtime, threshold_minutes=45, now=now) == (False, None)
    assert format_duration_minutes(30) == "30m"
    assert format_duration_minutes(125) == "2h"
    assert format_duration_minutes(60 * 26) == "1d"


def test_load_config_toml(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        """
stale_threshold_minutes = 60
ssh_connect_timeout = 8
default_runner = "ssh-nohup"

[[failure_rules]]
status = "failed_quota"
pattern = "CLOUD_CREDITS_EXHAUSTED"

[servers.docker-gpu]
meta_dir = "/data/.exploom_meta"
""",
        encoding="utf-8",
    )
    cfg = load_config(home_dir=tmp_path)
    assert cfg.stale_threshold_minutes == 60
    assert cfg.ssh_connect_timeout == 8
    assert cfg.default_runner == "ssh-nohup"
    assert len(cfg.custom_failure_rules) == 1
    assert cfg.custom_failure_rules[0].status == "failed_quota"
    assert cfg.get_meta_dir("docker-gpu", "proj", "E001", 2) == "/data/.exploom_meta/proj/E001_2"
