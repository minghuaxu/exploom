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
