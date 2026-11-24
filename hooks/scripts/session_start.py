#!/usr/bin/env python3
"""Exploom SessionStart hook (pure Python stdlib, ~15ms, read-only SQLite, zero SSH).

Injects a compact <=120-token status card for the current project:
- Project name & last_sync freshness
- Currently running attempts (with stale log warnings)
- Resumable failed attempts (failed_quota, failed_net, failed_oom, failed_disk)
- 3 most recently active experiments
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

DEFAULT_STALE_MINUTES = 45
RESUMABLE_STATUSES = ("failed_quota", "failed_net", "failed_oom", "failed_disk")
PROJECT_DOTFILE = ".exploom-project"


def _sanitize_slug(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-.")
    return cleaned or "default"


def _short_path_hash(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:6]


def _read_stale_threshold(home_dir: Path) -> int:
    cfg_path = home_dir / "config.toml"
    if not cfg_path.is_file():
        return DEFAULT_STALE_MINUTES
    try:
        raw = cfg_path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"^\s*stale_threshold_minutes\s*=\s*(\d+)", raw, re.MULTILINE)
        if match:
            return max(1, int(match.group(1)))
    except OSError:
        pass
    return DEFAULT_STALE_MINUTES


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _format_duration(minutes: int) -> str:
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours, rem = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{rem}m" if rem else f"{hours}h"
    days, rem_h = divmod(hours, 24)
    return f"{days}d{rem_h}h" if rem_h else f"{days}d"


def _format_age(ts: str | None, now: datetime) -> str:
    dt = _parse_iso(ts)
    if dt is None:
        return "never"
    mins = max(0, int((now - dt).total_seconds() // 60))
    return f"{_format_duration(mins)} ago"


def _resolve_project_ro(conn: sqlite3.Connection, cwd: Path) -> str:
    resolved_cwd = cwd.resolve()

    # Level 2a: SQLite local_root binding (deepest ancestor)
    best: tuple[int, str] | None = None
    for row in conn.execute("SELECT slug, local_root FROM projects WHERE local_root IS NOT NULL"):
        slug, raw_root = row[0], row[1]
        if not raw_root:
            continue
        root_path = Path(raw_root).expanduser().resolve()
        try:
            resolved_cwd.relative_to(root_path)
        except ValueError:
            continue
        depth = len(root_path.parts)
        if best is None or depth > best[0]:
            best = (depth, str(slug))
    if best:
        return best[1]

    # Level 2b: .exploom-project dotfile
    for directory in (resolved_cwd, *resolved_cwd.parents):
        dotfile = directory / PROJECT_DOTFILE
        if dotfile.is_file():
            try:
                for line in dotfile.read_text(encoding="utf-8").splitlines():
                    val = line.strip()
                    if val and not val.startswith("#"):
                        return _sanitize_slug(val)
            except OSError:
                pass

    # Level 3: Git repo root name
    for directory in (resolved_cwd, *resolved_cwd.parents):
        git_marker = directory / ".git"
        if git_marker.is_dir() or git_marker.is_file():
            return _sanitize_slug(directory.name)

    # Level 4: Directory name fallback with collision disambiguation
    base_slug = _sanitize_slug(resolved_cwd.name or "default")
    row = conn.execute(
        "SELECT local_root FROM projects WHERE slug = ?", (base_slug,)
    ).fetchone()
    if row and row[0]:
        existing_root = Path(row[0]).expanduser().resolve()
        if existing_root != resolved_cwd:
            return f"{base_slug}-{_short_path_hash(resolved_cwd)}"
    return base_slug


def _read_stdin_cwd() -> Path:
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read().strip()
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("cwd"):
                    return Path(str(data["cwd"])).expanduser()
    except Exception:
        pass
    return Path.cwd()


def build_status_card(
    home_dir: Path,
    cwd: Path,
    *,
    now: datetime | None = None,
) -> str | None:
    db_path = home_dir / "registry.db"
    if not db_path.is_file():
        return None

    ref_now = now or datetime.now(timezone.utc)
    stale_threshold = _read_stale_threshold(home_dir)
    uri = f"file:{quote(str(db_path.resolve()))}?mode=ro"

    conn = sqlite3.connect(uri, uri=True, timeout=0.5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")

        proj = _resolve_project_ro(conn, cwd)
        proj_row = conn.execute(
            "SELECT slug, last_synced_at FROM projects WHERE slug = ?", (proj,)
        ).fetchone()
        if proj_row is None:
            return None

        exps = conn.execute(
            """
            SELECT id, title, status_override, updated_at
            FROM experiments
            WHERE project = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (proj,),
        ).fetchall()
        if not exps:
            return None

        latest_attempts: dict[str, sqlite3.Row] = {}
        for att in conn.execute(
            """
            SELECT exp_id, attempt_no, server, resource, status, log_mtime, sync_error
            FROM attempts
            WHERE project = ?
            ORDER BY attempt_no ASC
            """,
            (proj,),
        ):
            latest_attempts[att["exp_id"]] = att

        running_items: list[str] = []
        resumable_items: list[str] = []
        recent_items: list[str] = []

        for exp in exps:
            eid = exp["id"]
            att = latest_attempts.get(eid)
            override = exp["status_override"]
            if override:
                status = str(override)
            elif att is None:
                status = "planned"
            else:
                status = str(att["status"])

            stale_suffix = ""
            if not override and att is not None and status == "running":
                mtime_dt = _parse_iso(att["log_mtime"])
                if mtime_dt is not None:
                    silent_min = int((ref_now - mtime_dt).total_seconds() // 60)
                    if silent_min >= stale_threshold:
                        stale_suffix = f" (stale {_format_duration(silent_min)})"

            if status == "running" and att is not None:
                loc = att["server"]
                if att["resource"]:
                    loc += f"/{att['resource']}"
                sync_note = f" [{att['sync_error']}]" if att["sync_error"] else ""
                running_items.append(f"{eid}#{att['attempt_no']}@{loc}{stale_suffix}{sync_note}")
            elif status in RESUMABLE_STATUSES and att is not None:
                resumable_items.append(f"{eid}#{att['attempt_no']} [{status}]")

            if len(recent_items) < 3:
                title = str(exp["title"])[:28]
                recent_items.append(f"{eid} {title} [{status}{stale_suffix}]")

        sync_str = _format_age(proj_row["last_synced_at"], ref_now)
        lines = [f"[Exploom] project={proj} | last_sync={sync_str}"]
        if running_items:
            shown = ", ".join(running_items[:4])
            if len(running_items) > 4:
                shown += f" (+{len(running_items) - 4})"
            lines.append(f"Running: {shown}")
        if resumable_items:
            shown = ", ".join(resumable_items[:4])
            if len(resumable_items) > 4:
                shown += f" (+{len(resumable_items) - 4})"
            lines.append(f"Resumable: {shown}")
        if recent_items:
            lines.append("Recent: " + ", ".join(recent_items))
        return "\n".join(lines)
    finally:
        conn.close()


def main() -> int:
    try:
        env_home = os.environ.get("EXPLOOM_HOME")
        home_dir = Path(env_home).expanduser().resolve() if env_home else (Path.home() / ".exploom")
        cwd = _read_stdin_cwd()
        card = build_status_card(home_dir, cwd)
        if card:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": card,
                }
            }
            sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    except Exception:
        # Never block or fail session startup
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
