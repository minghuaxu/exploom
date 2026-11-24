"""4-level hybrid project resolution chain (supports local code and code-free workspaces)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from exploom.db import Database

PROJECT_DOTFILE = ".exploom-project"


def sanitize_slug(name: str) -> str:
    """Normalize a project name into a filesystem- and shell-safe slug."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-.")
    return cleaned or "default"


def _short_path_hash(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:6]


def _find_dotfile_project(cwd: Path) -> str | None:
    for directory in (cwd, *cwd.parents):
        dotfile = directory / PROJECT_DOTFILE
        if dotfile.is_file():
            for line in dotfile.read_text(encoding="utf-8").splitlines():
                val = line.strip()
                if val and not val.startswith("#"):
                    return sanitize_slug(val)
    return None


def _find_git_root_name(cwd: Path) -> str | None:
    for directory in (cwd, *cwd.parents):
        git_marker = directory / ".git"
        if git_marker.is_dir() or git_marker.is_file():
            return sanitize_slug(directory.name)
    return None


def _find_db_bound_project(db: Database, cwd: Path) -> str | None:
    resolved_cwd = cwd.resolve()
    best_match: tuple[int, str] | None = None
    for proj in db.list_projects():
        raw_root = proj.get("local_root")
        if not raw_root:
            continue
        root_path = Path(raw_root).expanduser().resolve()
        try:
            resolved_cwd.relative_to(root_path)
        except ValueError:
            continue
        depth = len(root_path.parts)
        if best_match is None or depth > best_match[0]:
            best_match = (depth, str(proj["slug"]))
    return best_match[1] if best_match else None


def resolve_project(
    db: Database,
    explicit_project: str | None = None,
    cwd: Path | str | None = None,
) -> str:
    """Resolve the active project slug using the 4-level hybrid chain:

    1. Explicit argument
    2. Local binding (SQLite projects.local_root or .exploom-project file)
    3. Git repository root directory name
    4. Current directory name (with short path hash on slug collision)
    """
    if explicit_project and explicit_project.strip():
        return sanitize_slug(explicit_project)

    resolved_cwd = Path(cwd).expanduser().resolve() if cwd is not None else Path.cwd().resolve()

    # Level 2a: SQLite local_root binding
    bound = _find_db_bound_project(db, resolved_cwd)
    if bound:
        return bound

    # Level 2b: .exploom-project file
    dotfile_proj = _find_dotfile_project(resolved_cwd)
    if dotfile_proj:
        return dotfile_proj

    # Level 3: Git repo root name
    git_proj = _find_git_root_name(resolved_cwd)
    if git_proj:
        return git_proj

    # Level 4: Directory name fallback (disambiguate on collision)
    base_slug = sanitize_slug(resolved_cwd.name or "default")
    existing = db.get_project(base_slug)
    if existing and existing.get("local_root"):
        existing_root = Path(existing["local_root"]).expanduser().resolve()
        if existing_root != resolved_cwd:
            return f"{base_slug}-{_short_path_hash(resolved_cwd)}"
    return base_slug


def bind_project(
    db: Database,
    project: str | None = None,
    cwd: Path | str | None = None,
    *,
    write_dotfile: bool = False,
) -> dict[str, object]:
    """Bind the current directory to a project slug in SQLite (and optionally .exploom-project)."""
    resolved_cwd = Path(cwd).expanduser().resolve() if cwd is not None else Path.cwd().resolve()
    slug = (
        sanitize_slug(project)
        if project and project.strip()
        else resolve_project(db, explicit_project=None, cwd=resolved_cwd)
    )
    proj_row = db.ensure_project(slug, local_root=str(resolved_cwd))
    dotfile_path: str | None = None
    if write_dotfile:
        df = resolved_cwd / PROJECT_DOTFILE
        df.write_text(f"{slug}\n", encoding="utf-8")
        dotfile_path = str(df)
    return {
        "project": proj_row["slug"],
        "local_root": proj_row["local_root"],
        "dotfile": dotfile_path,
        "created_at": proj_row["created_at"],
    }
