"""End-to-end CLI tests using Typer CliRunner."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from exploom.cli import app

runner = CliRunner()


def test_cli_end_to_end_json_and_rich(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "exploom_home"
    ws = tmp_path / "project_ws"
    ws.mkdir()
    monkeypatch.setenv("EXPLOOM_HOME", str(home))
    monkeypatch.chdir(ws)

    # 1. init
    r = runner.invoke(app, ["init", "demo-proj", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["project"] == "demo-proj"

    # 2. plan root & child
    r = runner.invoke(
        app,
        ["plan", "--title", "baseline", "--hypothesis", "Baseline run", "--tag", "sft", "--json"],
    )
    assert r.exit_code == 0, r.output
    e1 = json.loads(r.output)
    assert e1["exp_id"] == "E001"

    r = runner.invoke(
        app,
        ["plan", "--title", "fix_lr", "--parent", "E001", "--metric", "lr=2e-5", "--json"],
    )
    assert r.exit_code == 0, r.output
    e2 = json.loads(r.output)
    assert e2["exp_id"] == "E002"

    # 3. attempt start & update
    r = runner.invoke(
        app,
        [
            "attempt",
            "start",
            "E001",
            "--server",
            "gpu-a100",
            "--workdir",
            "/work/demo",
            "--cmd",
            "python train.py",
            "--resource",
            "gpu:0",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    att = json.loads(r.output)
    assert att["attempt_no"] == 1
    assert att["status"] == "running"

    # ps should show E001#1 running
    r = runner.invoke(app, ["ps", "--json"])
    assert r.exit_code == 0, r.output
    ps_rows = json.loads(r.output)
    assert len(ps_rows) == 1
    assert ps_rows[0]["exp_id"] == "E001"

    # Rich ps output also succeeds
    r = runner.invoke(app, ["ps"])
    assert r.exit_code == 0, r.output
    assert "gpu-a100" in r.output

    # Update E001#1 to done with metrics
    r = runner.invoke(
        app,
        ["attempt", "update", "E001", "--exit-code", "0", "--metric", "acc=0.91", "--json"],
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["status"] == "done"

    # 4. finding
    r = runner.invoke(
        app,
        [
            "finding",
            "E001",
            "--kind",
            "analysis",
            "--summary",
            "Eval error concentrated on long inputs",
            "--artifact",
            "gpu-a100:scripts/err.py",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["finding_id"] == "E001-F1"

    # 5. tree (human + json)
    r = runner.invoke(app, ["tree", "-a"])
    assert r.exit_code == 0, r.output
    assert "E001" in r.output
    assert "E001-F1" in r.output
    assert "E002" in r.output

    r = runner.invoke(app, ["tree", "-a", "--json"])
    assert r.exit_code == 0, r.output
    tree_data = json.loads(r.output)
    assert tree_data["project"] == "demo-proj"
    assert tree_data["roots"][0]["id"] == "E001"

    # 6. ls
    r = runner.invoke(app, ["ls", "--server", "gpu-a100", "--json"])
    assert r.exit_code == 0, r.output
    ls_data = json.loads(r.output)
    assert [c["id"] for c in ls_data] == ["E001"]

    # 7. show (human + json)
    r = runner.invoke(app, ["show", "E001"])
    assert r.exit_code == 0, r.output
    assert "baseline" in r.output

    r = runner.invoke(app, ["show", "E001", "--json"])
    assert r.exit_code == 0, r.output
    ctx = json.loads(r.output)
    assert ctx["experiment"]["status"] == "done"
    assert len(ctx["findings"]) == 1
    assert ctx["recent_attempts"][0]["started_at"] is not None
    assert ctx["recent_attempts"][0]["ended_at"] is not None
    assert ctx["recent_attempts"][0]["duration_seconds"] is not None


def test_cli_timeline_times_and_sort(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "exploom_home"
    ws = tmp_path / "project_ws"
    ws.mkdir()
    monkeypatch.setenv("EXPLOOM_HOME", str(home))
    monkeypatch.chdir(ws)

    assert runner.invoke(app, ["init", "timeline-proj", "--json"]).exit_code == 0
    assert runner.invoke(app, ["plan", "--title", "later_run", "--json"]).exit_code == 0
    assert runner.invoke(app, ["plan", "--title", "earlier_run", "--json"]).exit_code == 0
    assert runner.invoke(app, ["plan", "--title", "planned_only", "--json"]).exit_code == 0

    assert (
        runner.invoke(
            app,
            ["attempt", "start", "E001", "--server", "local", "--workdir", ".", "--cmd", "echo 1"],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "attempt",
                "update",
                "E001",
                "--status",
                "done",
                "--started-at",
                "2026-11-20T10:00:00Z",
                "--ended-at",
                "2026-11-20T12:30:00Z",
                "--json",
            ],
        ).exit_code
        == 0
    )

    assert (
        runner.invoke(
            app,
            ["attempt", "start", "E002", "--server", "local", "--workdir", ".", "--cmd", "echo 2"],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            [
                "attempt",
                "update",
                "E002",
                "--status",
                "done",
                "--started-at",
                "2026-11-19T08:00:00Z",
                "--ended-at",
                "2026-11-19T09:15:00Z",
                "--json",
            ],
        ).exit_code
        == 0
    )

    r = runner.invoke(app, ["ls", "--sort", "started", "--json"])
    assert r.exit_code == 0, r.output
    cards = json.loads(r.output)
    assert [c["id"] for c in cards] == ["E002", "E001", "E003"]
    assert cards[0]["duration_seconds"] == 4500
    assert cards[0]["duration_label"] == "1h15m"
    assert cards[1]["duration_seconds"] == 9000
    assert cards[1]["duration_label"] == "2h30m"
    assert cards[2]["started_at"] is None
    assert cards[2]["ended_at"] is None

    r_rich = runner.invoke(app, ["ls", "--sort", "ended"])
    assert r_rich.exit_code == 0, r_rich.output
    assert "Timeline" in r_rich.output
    assert "1h15m" in r_rich.output
    assert "2h30m" in r_rich.output

    r_show = runner.invoke(app, ["show", "E001"])
    assert r_show.exit_code == 0, r_show.output
    assert "2h30m" in r_show.output
