"""Tests for ExploomService business logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from exploom.service import ExploomService


def test_full_experiment_lineage_lifecycle(tmp_path: Path) -> None:
    svc = ExploomService(home_dir=tmp_path / "exploom_home")
    ws = tmp_path / "workspace"
    ws.mkdir()

    # 1. Init project
    init_res = svc.init_project("llm-ft", cwd=ws)
    assert init_res["project"] == "llm-ft"

    # 2. Plan root experiment
    e1 = svc.plan_experiment(
        title="data_clean_v1",
        hypothesis="Deduplicate noisy samples",
        tags=["data"],
        cwd=ws,
    )
    assert e1["exp_id"] == "E001"
    assert e1["status"] == "planned"
    assert Path(e1["note_path"]).is_file()

    # 3. Start attempt & detect conflicts
    a1 = svc.start_attempt(
        exp_id="E001",
        server="gpu-1",
        workdir="/data/proj",
        run_cmd="python clean.py",
        resource="gpu:0,1",
        cwd=ws,
    )
    assert a1["attempt_no"] == 1
    assert a1["conflict_warnings"] == []
    assert "ssh gpu-1" in a1["ssh_cmd"]

    # 4. Plan child experiment and start conflicting attempt on gpu:1,2
    e2 = svc.plan_experiment(
        title="sft_lora_v1",
        parent_id="E001",
        tags=["sft", "/side"],
        cwd=ws,
    )
    assert e2["exp_id"] == "E002"

    a2 = svc.start_attempt(
        exp_id="E002",
        server="gpu-1",
        workdir="/data/proj",
        run_cmd="python train.py",
        resume_cmd="python train.py --resume",
        resource="gpu:1,2",
        cwd=ws,
    )
    # Both GPU resource overlap (gpu:1) and workdir overlap should warn
    assert len(a2["conflict_warnings"]) == 2

    # 5. Guardrail: sync_error must NOT overwrite status
    sync_err = svc.update_attempt(
        exp_id="E002",
        sync_error="ssh_unreachable",
        cwd=ws,
    )
    assert sync_err["status"] == "running"
    assert sync_err["sync_error"] == "ssh_unreachable"

    # 6. Auto-classify OOM failure on E002#1 and attach patch
    fail_res = svc.update_attempt(
        exp_id="E002",
        exit_code=137,
        log_tail="CUDA out of memory at step 400",
        patch_content="diff --git a/train.py b/train.py\n--- a/train.py\n+++ b/train.py\n",
        cwd=ws,
    )
    assert fail_res["status"] == "failed_oom"

    # 7. Complete E001#1 with metrics
    done_res = svc.update_attempt(
        exp_id="E001",
        exit_code=0,
        metrics={"rows": 50000, "dedup_ratio": 0.12},
        cwd=ws,
    )
    assert done_res["status"] == "done"

    # 8. Add finding on E002 and branch E003 from that finding
    f1 = svc.add_finding(
        exp_id="E002",
        attempt_no=1,
        kind="diagnosis",
        summary="Batch size 32 OOMs on 24GB GPU",
        artifacts=[{"server": "gpu-1", "path": "logs/oom.log"}],
        cwd=ws,
    )
    assert f1["finding_id"] == "E002-F1"

    e3 = svc.plan_experiment(
        title="sft_lora_bs16",
        parent_id="E002",
        based_on_finding="E002-F1",
        cwd=ws,
    )
    assert e3["exp_id"] == "E003"

    # 9. Verify tree structure
    tree = svc.get_tree(include_findings=True, cwd=ws)
    assert tree["project"] == "llm-ft"
    assert len(tree["roots"]) == 1
    root = tree["roots"][0]
    assert root["id"] == "E001"
    assert root["status"] == "done"
    assert root["metrics"]["rows"] == 50000
    assert root["children"][0]["id"] == "E002"
    assert root["children"][0]["findings"][0]["id"] == "E002-F1"
    assert root["children"][0]["children"][0]["id"] == "E003"
    assert root["children"][0]["children"][0]["based_on_finding"] == "E002-F1"

    # 10. Verify context for E003 (ancestors E001 -> E002) and E002 (patch changed files)
    ctx3 = svc.get_context("E003", cwd=ws)
    assert [a["id"] for a in ctx3["ancestors"]] == ["E001", "E002"]

    ctx2 = svc.get_context("E002", cwd=ws)
    assert ctx2["changed_files"] == ["train.py"]
    assert len(ctx2["findings"]) == 1
    assert "Batch size 32 OOMs" in ctx2["note"]

    # 11. Verify filters in list_experiments
    failed = svc.list_experiments(failed_only=True, cwd=ws)
    assert [c["id"] for c in failed] == ["E002"]

    side_exps = svc.list_experiments(tag="/side", cwd=ws)
    assert [c["id"] for c in side_exps] == ["E002"]


def test_status_override_and_stale_detection(tmp_path: Path) -> None:
    svc = ExploomService(home_dir=tmp_path / "home")
    svc.plan_experiment(title="long_run", project="p1")
    svc.start_attempt(
        exp_id="E001",
        server="gpu-2",
        workdir="/w",
        run_cmd="python run.py",
        project="p1",
    )

    now = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
    old_mtime = (now - timedelta(minutes=120)).isoformat()
    svc.update_attempt(exp_id="E001", log_mtime=old_mtime, project="p1")

    resources = svc.get_resources(project="p1", all_projects=False, now=now)
    assert len(resources) == 1
    assert resources[0]["is_stale"] is True
    assert resources[0]["stale_label"] == "stale 2h"

    # Override status to archived
    upd = svc.plan_experiment(
        action="update", exp_id="E001", status_override="archived", project="p1"
    )
    assert upd["status"] == "archived"

    # Clear override -> returns to derived 'running'
    cleared = svc.plan_experiment(
        action="update", exp_id="E001", status_override="", project="p1"
    )
    assert cleared["status"] == "running"


def test_validation_errors(tmp_path: Path) -> None:
    svc = ExploomService(home_dir=tmp_path / "home")
    with pytest.raises(ValueError, match="Parent experiment"):
        svc.plan_experiment(title="orphan", parent_id="E999", project="p1")

    svc.plan_experiment(title="root", project="p1")
    with pytest.raises(ValueError, match="Invalid finding kind"):
        svc.add_finding(exp_id="E001", kind="invalid", summary="x", project="p1")
