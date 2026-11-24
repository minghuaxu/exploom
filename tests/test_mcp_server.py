"""Tests for Exploom's 5-tool FastMCP / MCPServer interface and security boundaries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from exploom.mcp_server import MAX_LOG_LINES, create_mcp_server
from exploom.service import ExploomService
from exploom.ssh import CommandOutput


def _call_tool_sync(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool synchronously and parse its JSON result."""
    res = asyncio.run(server.call_tool(name, arguments))
    # mcp 2.x returns CallToolResult with structuredContent or content[0].text
    if getattr(res, "isError", False):
        text = res.content[0].text if res.content else "unknown tool error"
        raise RuntimeError(text)
    structured = getattr(res, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    if res.content:
        return json.loads(res.content[0].text)
    raise AssertionError(f"Unexpected tool result: {res!r}")


def test_mcp_exposes_exact_five_tools(tmp_path: Path) -> None:
    svc = ExploomService(home_dir=tmp_path / ".exploom")
    server = create_mcp_server(service=svc)
    tools = asyncio.run(server.list_tools())
    names = [t.name for t in tools]
    assert names == ["exp_plan", "exp_attempt", "exp_finding", "exp_query", "exp_ops"]


def test_mcp_full_multi_agent_and_side_workflow(tmp_path: Path) -> None:
    """End-to-end M3 closed loop:
    main agent checks resources -> plans E001 -> subagent restores context ->
    starts attempt (without MCP executing SSH!) -> syncs via mock read-only SSH ->
    logs finding -> /side branches E002 -> new session restores E002 context.
    """
    calls: list[tuple[str, str]] = []

    def mock_executor(
        server: str, script: str, timeout: int, connect_timeout: int
    ) -> CommandOutput:
        calls.append((server, script))
        # Handle probe batch
        if "===EXPLOOM_ATTEMPT_START:" in script:
            return CommandOutput(
                returncode=0,
                stdout=(
                    "===EXPLOOM_ATTEMPT_START:0===\n"
                    "ALIVE:0\n"
                    "LOG_MTIME:1763690000\n"
                    "META_BEGIN\n"
                    '{"git_commit":"abc1234","git_branch":"main","diff_hash":"hash1"}\n'
                    "META_END\n"
                    "EXIT_BEGIN\n"
                    '{"exit_code":137,"ended_at":"2026-11-21T04:00:00Z"}\n'
                    "EXIT_END\n"
                    "TAIL_BEGIN\n"
                    "torch.cuda.OutOfMemoryError: CUDA out of memory.\n"
                    "TAIL_END\n"
                    "PATCH_BEGIN\n"
                    "diff --git a/train.py b/train.py\n"
                    "--- a/train.py\n"
                    "+++ b/train.py\n"
                    "@@ -1 +1 @@\n"
                    "-bs=64\n"
                    "+bs=128\n"
                    "PATCH_END\n"
                    "===EXPLOOM_ATTEMPT_END:0===\n"
                ),
                stderr="",
            )
        # Handle tail_log
        if "tail -n" in script:
            return CommandOutput(returncode=0, stdout="line 1\nCUDA out of memory\n", stderr="")
        # Handle drift check
        if "DIFF_HASH:" in script:
            return CommandOutput(
                returncode=0,
                stdout="IS_GIT:1\nCOMMIT:abc1234\nBRANCH:main\nDIFF_HASH:hash1\n",
                stderr="",
            )
        raise AssertionError(f"Unexpected SSH call: {server} -> {script[:120]}")

    svc = ExploomService(home_dir=tmp_path / ".exploom", executor=mock_executor)
    server = create_mcp_server(service=svc)

    # 1. Main agent checks resources (empty)
    res_before = _call_tool_sync(server, "exp_query", {"view": "resources"})
    assert res_before["count"] == 0

    # 2. Main agent plans E001
    e1 = _call_tool_sync(
        server,
        "exp_plan",
        {
            "action": "create",
            "project": "demo",
            "title": "sft_baseline",
            "hypothesis": "Baseline Qwen2.5-7B SFT",
            "tags": ["sft"],
        },
    )
    assert e1["exp_id"] == "E001"
    assert e1["status"] == "planned"

    # 3. Subagent receives [EXP_ID=E001] and restores context
    ctx1 = _call_tool_sync(
        server, "exp_query", {"view": "context", "project": "demo", "exp_id": "E001"}
    )
    assert ctx1["experiment"]["title"] == "sft_baseline"
    assert ctx1["ancestors"] == []

    # 4. Subagent registers attempt via exp_attempt(start) -> MUST NOT execute SSH!
    att = _call_tool_sync(
        server,
        "exp_attempt",
        {
            "action": "start",
            "project": "demo",
            "exp_id": "E001",
            "server": "gpu-1",
            "workdir": "/data/demo",
            "resource": "gpu:0,1",
            "run_cmd": "python train.py --bs 128",
            "resume_cmd": "python train.py --bs 128 --resume",
        },
    )
    assert att["attempt_no"] == 1
    assert "ssh gpu-1" in att["ssh_cmd"]
    assert "exploom-wrapper" in att["wrapped_cmd"]
    assert len(calls) == 0  # Security boundary: start_attempt NEVER runs SSH

    # 5. Resources view shows gpu-1/gpu:0,1 occupied
    res_during = _call_tool_sync(server, "exp_query", {"view": "resources"})
    assert res_during["count"] == 1
    assert res_during["resources"][0]["server"] == "gpu-1"
    assert res_during["resources"][0]["resource"] == "gpu:0,1"

    # 6. Sync detects OOM failure and saves dirty patch
    synced = _call_tool_sync(server, "exp_ops", {"action": "sync", "project": "demo"})
    assert synced["synced_count"] == 1
    assert synced["changed"][0]["new_status"] == "failed_oom"

    # 7. Tail log clamps lines > MAX_LOG_LINES
    log_res = _call_tool_sync(
        server,
        "exp_ops",
        {"action": "tail_log", "project": "demo", "exp_id": "E001", "lines": 999},
    )
    assert log_res["lines"] == MAX_LOG_LINES
    assert "CUDA out of memory" in log_res["content"]

    # 8. Subagent records diagnosis finding
    finding = _call_tool_sync(
        server,
        "exp_finding",
        {
            "project": "demo",
            "exp_id": "E001",
            "attempt_no": 1,
            "kind": "diagnosis",
            "summary": "Batch size 128 OOMs at step 40; drop to 64 with grad_accum=2",
            "artifacts": [{"server": "gpu-1", "path": "scripts/mem_profile.py"}],
        },
    )
    assert finding["finding_id"] == "E001-F1"

    # 9. /side branches E002 based on E001-F1
    e2 = _call_tool_sync(
        server,
        "exp_plan",
        {
            "action": "create",
            "project": "demo",
            "parent_id": "E001",
            "based_on_finding": "E001-F1",
            "title": "sft_bs64_ga2",
            "tags": ["/side", "sft"],
        },
    )
    assert e2["exp_id"] == "E002"
    assert e2["parent_id"] == "E001"

    # 10. Tree view renders compact text with findings
    tree = _call_tool_sync(
        server,
        "exp_query",
        {"view": "tree", "project": "demo", "include_findings": True},
    )
    assert "E001 sft_baseline [failed_oom]" in tree["tree_text"]
    assert "E001-F1 [diagnosis]" in tree["tree_text"]
    assert "E002 sft_bs64_ga2 [planned]" in tree["tree_text"]
    assert "#/side" in tree["tree_text"]

    # 11. prepare_rerun checks drift (read-only SSH) and returns ssh_cmd without launching
    calls_before_rerun = len(calls)
    prep = _call_tool_sync(
        server,
        "exp_ops",
        {"action": "prepare_rerun", "project": "demo", "exp_id": "E001"},
    )
    assert prep["blocked"] is False
    assert prep["used_resume_cmd"] is True
    assert prep["attempt"]["attempt_no"] == 2
    assert "ssh gpu-1" in prep["attempt"]["ssh_cmd"]
    # Only the 1 read-only drift check ran, NOT a launch command
    assert len(calls) == calls_before_rerun + 1

    # 12. New session restores E002 context with ancestor chain
    ctx2 = _call_tool_sync(
        server, "exp_query", {"view": "context", "project": "demo", "exp_id": "E002"}
    )
    assert len(ctx2["ancestors"]) == 1
    assert ctx2["ancestors"][0]["id"] == "E001"

    # Also verify E001 context shows changed_files from saved patch
    ctx1_after = _call_tool_sync(
        server, "exp_query", {"view": "context", "project": "demo", "exp_id": "E001"}
    )
    assert ctx1_after["changed_files"] == ["train.py"]


def test_mcp_prepare_rerun_blocks_on_drift(tmp_path: Path) -> None:
    def drift_executor(
        server: str, script: str, timeout: int, connect_timeout: int
    ) -> CommandOutput:
        return CommandOutput(
            returncode=0,
            stdout="IS_GIT:1\nCOMMIT:new_commit_999\nBRANCH:main\nDIFF_HASH:hash_new\n",
            stderr="",
        )

    svc = ExploomService(home_dir=tmp_path / ".exploom", executor=drift_executor)
    svc.plan_experiment(action="create", project="p1", title="base")
    svc.start_attempt(
        exp_id="E001",
        server="gpu-1",
        workdir="/work",
        run_cmd="python run.py",
        git_commit="old_commit_111",
        diff_hash="hash_old",
        project="p1",
    )
    server = create_mcp_server(service=svc)

    blocked = _call_tool_sync(
        server,
        "exp_ops",
        {"action": "prepare_rerun", "project": "p1", "exp_id": "E001"},
    )
    assert blocked["blocked"] is True
    assert blocked["reason"] == "code_drift"
    assert blocked["attempt"] is None

    forced = _call_tool_sync(
        server,
        "exp_ops",
        {
            "action": "prepare_rerun",
            "project": "p1",
            "exp_id": "E001",
            "force_on_drift": True,
        },
    )
    assert forced["blocked"] is False
    assert forced["attempt"]["attempt_no"] == 2


def test_mcp_validation_errors(tmp_path: Path) -> None:
    svc = ExploomService(home_dir=tmp_path / ".exploom")
    server = create_mcp_server(service=svc)

    with pytest.raises(Exception, match="exp_id is required"):
        _call_tool_sync(server, "exp_query", {"view": "context"})

    with pytest.raises(Exception, match="server is required"):
        _call_tool_sync(
            server,
            "exp_attempt",
            {"action": "start", "exp_id": "E001", "workdir": "/tmp", "run_cmd": "echo hi"},
        )

    with pytest.raises(Exception, match="exp_id is required"):
        _call_tool_sync(server, "exp_ops", {"action": "tail_log"})


def test_mcp_record_attempt_backfill(tmp_path: Path) -> None:
    svc = ExploomService(home_dir=tmp_path / ".exploom")
    server = create_mcp_server(service=svc)

    created = _call_tool_sync(
        server,
        "exp_plan",
        {
            "action": "create",
            "project": "p1",
            "title": "historical_sft",
            "hypothesis": "lr=1e-4",
            "tags": ["backfilled"],
        },
    )
    exp_id = created["exp_id"]

    recorded = _call_tool_sync(
        server,
        "exp_attempt",
        {
            "action": "record",
            "project": "p1",
            "exp_id": exp_id,
            "server": "gpu-2",
            "workdir": "/work/nanote",
            "resource": "gpu:0,1",
            "runner": "ssh-tmux",
            "runner_ref": "train_nanote_v1",
            "run_cmd": "python train.py --lr 1e-4",
            "log_path": "/work/nanote/train.log",
            "status": "done",
            "exit_code": 0,
            "metrics": {"val_loss": 0.42},
        },
    )
    assert recorded["attempt_no"] == 1
    assert recorded["status"] == "done"
    assert recorded["runner"] == "ssh-tmux"
    assert recorded["runner_ref"] == "train_nanote_v1"
    assert "ssh_cmd" not in recorded
    assert "wrapped_cmd" not in recorded

    ctx = _call_tool_sync(
        server,
        "exp_query",
        {"view": "context", "project": "p1", "exp_id": exp_id},
    )
    assert ctx["experiment"]["status"] == "done"
    assert ctx["experiment"]["metrics"]["val_loss"] == 0.42
    assert ctx["recent_attempts"][0]["ended_at"] is not None


def test_mcp_upward_reparent_and_cycle_guard(tmp_path: Path) -> None:
    svc = ExploomService(home_dir=tmp_path / ".exploom")
    server = create_mcp_server(service=svc)

    # Session 1 (newer) collected v2 -> v2_fix first
    e1 = _call_tool_sync(server, "exp_plan", {"action": "create", "project": "p1", "title": "sft_v2"})
    e2 = _call_tool_sync(
        server,
        "exp_plan",
        {"action": "create", "project": "p1", "title": "sft_v2_fix", "parent_id": e1["exp_id"]},
    )

    # Session 2 (older) collects v1 baseline later and stitches E001 under E003
    e3 = _call_tool_sync(server, "exp_plan", {"action": "create", "project": "p1", "title": "sft_v1"})
    updated = _call_tool_sync(
        server,
        "exp_plan",
        {"action": "update", "project": "p1", "exp_id": e1["exp_id"], "parent_id": e3["exp_id"]},
    )
    assert updated["parent_id"] == e3["exp_id"]

    tree = _call_tool_sync(server, "exp_query", {"view": "tree", "project": "p1"})
    lines = tree["tree_text"].splitlines()
    # E003 is now the sole root, E001 is its child, E002 is E001's child
    assert "E003 sft_v1" in lines[1]
    assert "E001 sft_v2" in lines[2]
    assert "E002 sft_v2_fix" in lines[3]

    # Cycle guard rejects making E003 a child of its descendant E002
    with pytest.raises(Exception, match="create a cycle"):
        _call_tool_sync(
            server,
            "exp_plan",
            {"action": "update", "project": "p1", "exp_id": e3["exp_id"], "parent_id": e2["exp_id"]},
        )


def test_mcp_based_on_finding_normalizes_verbose_label(tmp_path: Path) -> None:
    svc = ExploomService(home_dir=tmp_path / ".exploom")
    server = create_mcp_server(service=svc)

    e1 = _call_tool_sync(server, "exp_plan", {"action": "create", "project": "p1", "title": "base"})
    f1 = _call_tool_sync(
        server,
        "exp_finding",
        {"project": "p1", "exp_id": e1["exp_id"], "kind": "analysis", "summary": "high variance"},
    )
    e2 = _call_tool_sync(
        server,
        "exp_plan",
        {
            "action": "create",
            "project": "p1",
            "title": "multi_seed",
            "parent_id": e1["exp_id"],
            "based_on_finding": f"{f1['finding_id']}: high variance in first random draw",
        },
    )
    ctx = _call_tool_sync(server, "exp_query", {"view": "context", "project": "p1", "exp_id": e2["exp_id"]})
    assert ctx["experiment"]["based_on_finding"] == f1["finding_id"]
