"""Tests for 4-level hybrid project resolution chain."""

from __future__ import annotations

from pathlib import Path

from exploom.db import Database
from exploom.project import PROJECT_DOTFILE, bind_project, resolve_project


def test_level1_explicit_project(tmp_path: Path) -> None:
    db = Database(tmp_path / "registry.db")
    assert resolve_project(db, explicit_project="My Custom Proj!", cwd=tmp_path) == "My-Custom-Proj"


def test_level2_db_binding_and_nested_cwd(tmp_path: Path) -> None:
    db = Database(tmp_path / "registry.db")
    workspace = tmp_path / "empty_ctrl_dir"
    nested = workspace / "sub" / "deep"
    nested.mkdir(parents=True)

    res = bind_project(db, project="remote-llm", cwd=workspace)
    assert res["project"] == "remote-llm"
    assert resolve_project(db, cwd=workspace) == "remote-llm"
    assert resolve_project(db, cwd=nested) == "remote-llm"


def test_level2_dotfile_fallback(tmp_path: Path) -> None:
    db = Database(tmp_path / "registry.db")
    workspace = tmp_path / "work"
    sub = workspace / "scripts"
    sub.mkdir(parents=True)
    (workspace / PROJECT_DOTFILE).write_text("# comment\n  vision_v2  \n", encoding="utf-8")

    assert resolve_project(db, cwd=workspace) == "vision_v2"
    assert resolve_project(db, cwd=sub) == "vision_v2"


def test_level3_git_repo_name(tmp_path: Path) -> None:
    db = Database(tmp_path / "registry.db")
    repo = tmp_path / "awesome-repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "models"
    sub.mkdir(parents=True)

    assert resolve_project(db, cwd=sub) == "awesome-repo"


def test_level4_directory_fallback_and_collision_hash(tmp_path: Path) -> None:
    db = Database(tmp_path / "registry.db")
    dir_a = tmp_path / "a" / "shared"
    dir_b = tmp_path / "b" / "shared"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    # First directory gets bare name and binds it
    assert resolve_project(db, cwd=dir_a) == "shared"
    bind_project(db, project="shared", cwd=dir_a)

    # Second directory with same basename gets disambiguated with short hash
    resolved_b = resolve_project(db, cwd=dir_b)
    assert resolved_b.startswith("shared-")
    assert len(resolved_b) == len("shared-") + 6


def test_bind_project_writes_dotfile(tmp_path: Path) -> None:
    db = Database(tmp_path / "registry.db")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    res = bind_project(db, project="bound-proj", cwd=workspace, write_dotfile=True)
    dotfile = workspace / PROJECT_DOTFILE
    assert dotfile.is_file()
    assert dotfile.read_text(encoding="utf-8").strip() == "bound-proj"
    assert res["dotfile"] == str(dotfile)
