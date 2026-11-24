"""Structural validation for ZCode / Claude Code Plugin manifests, SKILL.md, commands, and hooks."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_plugin_manifests_valid() -> None:
    for rel in (".zcode-plugin/plugin.json", ".claude-plugin/plugin.json"):
        path = REPO_ROOT / rel
        assert path.is_file(), f"Missing {rel}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["name"] == "exploom"
        assert data["version"]
        assert data["skills"] == "./skills/"
        assert data["commands"] == "./commands/"
        assert data["hooks"] == "./hooks/hooks.json"
        assert data["mcpServers"] == "./.mcp.json"


def test_marketplace_manifests_valid() -> None:
    for rel in (".zcode-plugin/marketplace.json", ".claude-plugin/marketplace.json"):
        path = REPO_ROOT / rel
        assert path.is_file(), f"Missing {rel}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["name"] == "exploom"
        assert isinstance(data.get("plugins"), list) and len(data["plugins"]) == 1
        plugin_entry = data["plugins"][0]
        assert plugin_entry["name"] == "exploom"
        assert plugin_entry["source"] == "./"


def test_mcp_json_valid() -> None:
    path = REPO_ROOT / ".mcp.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "mcpServers" in data
    assert "exploom" in data["mcpServers"]
    server_cfg = data["mcpServers"]["exploom"]
    assert "command" in server_cfg
    assert "exploom-mcp" in " ".join([server_cfg["command"], *server_cfg.get("args", [])])


def test_hooks_json_valid() -> None:
    path = REPO_ROOT / "hooks" / "hooks.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    hooks = data["hooks"]
    assert "SessionStart" in hooks
    assert "PostToolUse" in hooks
    assert hooks["PostToolUse"][0]["matcher"] == "Bash"
    assert "session_start.py" in hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "post_bash_check.py" in hooks["PostToolUse"][0]["hooks"][0]["command"]


def test_skill_md_within_60_lines() -> None:
    path = REPO_ROOT / "skills" / "exploom" / "SKILL.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) <= 60, f"SKILL.md has {len(lines)} lines (must be <= 60)"
    assert lines[0].strip() == "---"
    assert "name: exploom" in text
    for tool in ("exp_plan", "exp_attempt", "exp_finding", "exp_query", "exp_ops"):
        assert tool in text


def test_slash_commands_present() -> None:
    expected = {
        "commands/exp.md": "exp_query",
        "commands/exp-resume.md": "exp_query",
        "commands/exp-sync.md": "exp_ops",
        "commands/exp-rerun.md": "prepare_rerun",
        "commands/exp-collect.md": "record",
    }
    for rel, token in expected.items():
        path = REPO_ROOT / rel
        assert path.is_file(), f"Missing {rel}"
        content = path.read_text(encoding="utf-8")
        assert content.startswith("---")
        assert token in content
