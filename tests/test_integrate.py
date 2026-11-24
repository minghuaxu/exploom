"""Tests for idempotent AGENTS.md / CLAUDE.md integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from exploom.integrate import END_MARKER, START_MARKER, apply_integration, resolve_integration_target


def test_integrate_create_update_and_remove(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "AGENTS.md"

    # Create
    r1 = apply_integration(target, lang="en")
    assert r1["action"] == "created"
    assert r1["changed"] is True
    text1 = target.read_text(encoding="utf-8")
    assert START_MARKER in text1 and END_MARKER in text1
    assert "Exploom Experiment Tracking" in text1

    # Idempotent re-run
    r2 = apply_integration(target, lang="en")
    assert r2["action"] == "unchanged"
    assert r2["changed"] is False

    # Switch language updates in place
    r3 = apply_integration(target, lang="zh")
    assert r3["action"] == "updated"
    assert r3["changed"] is True
    text3 = target.read_text(encoding="utf-8")
    assert "Exploom 实验血缘与子智能体接力" in text3
    assert text3.count(START_MARKER) == 1

    # Remove cleanly
    r4 = apply_integration(target, remove=True)
    assert r4["action"] == "removed"
    assert r4["changed"] is True
    assert target.read_text(encoding="utf-8") == ""

    # Removing again is a noop
    r5 = apply_integration(target, remove=True)
    assert r5["action"] == "noop"
    assert r5["changed"] is False


def test_integrate_preserves_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "AGENTS.md"
    original = "# Existing Rules\n\n- Keep me intact.\n"
    target.write_text(original, encoding="utf-8")

    r_add = apply_integration(target, lang="zh")
    assert r_add["action"] == "appended"
    after_add = target.read_text(encoding="utf-8")
    assert after_add.startswith("# Existing Rules\n\n- Keep me intact.\n\n")
    assert START_MARKER in after_add

    r_rm = apply_integration(target, remove=True)
    assert r_rm["action"] == "removed"
    assert target.read_text(encoding="utf-8") == original


def test_resolve_integration_target(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "proj"

    assert resolve_integration_target(cwd=cwd, home=home) == (cwd / "AGENTS.md").resolve()
    assert resolve_integration_target(zcode=True, cwd=cwd, home=home) == (
        home / ".zcode" / "AGENTS.md"
    ).resolve()
    assert resolve_integration_target(claude=True, cwd=cwd, home=home) == (
        home / ".claude" / "CLAUDE.md"
    ).resolve()

    with pytest.raises(ValueError, match="at most one"):
        resolve_integration_target(zcode=True, claude=True, cwd=cwd, home=home)
