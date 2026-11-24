"""Markdown experiment notes and patch asset management."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from exploom.config import DEFAULT_MAX_PATCH_BYTES


def note_file_path(notes_dir: Path, project: str, exp_id: str) -> Path:
    return notes_dir / project / f"{exp_id}.md"


def patch_file_path(patches_dir: Path, project: str, exp_id: str, attempt_no: int) -> Path:
    return patches_dir / project / f"{exp_id}_{attempt_no}.patch"


def ensure_note(
    notes_dir: Path,
    project: str,
    exp_id: str,
    title: str,
    hypothesis: str = "",
    parent_id: str | None = None,
    based_on_finding: str | None = None,
    tags: list[str] | None = None,
) -> Path:
    """Create an experiment Markdown note if it does not already exist."""
    path = note_file_path(notes_dir, project, exp_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path

    meta_lines = [
        f"- **Project**: `{project}`",
        f"- **Experiment**: `{exp_id}`",
    ]
    if parent_id:
        meta_lines.append(f"- **Parent**: `{parent_id}`")
    if based_on_finding:
        meta_lines.append(f"- **Based On Finding**: `{based_on_finding}`")
    if tags:
        meta_lines.append(f"- **Tags**: {', '.join(f'`{t}`' for t in tags)}")

    content = (
        f"# {exp_id}: {title}\n\n"
        + "\n".join(meta_lines)
        + "\n\n## Hypothesis\n\n"
        + (hypothesis.strip() if hypothesis.strip() else "_TBD_")
        + "\n\n## Findings & Notes\n\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def read_note(note_path: Path | str) -> str:
    path = Path(note_path)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def append_note(
    note_path: Path | str,
    text: str,
    *,
    heading: str | None = None,
) -> Path:
    """Append a timestamped block to an experiment Markdown note."""
    path = Path(note_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    parts: list[str] = []
    if path.exists() and path.stat().st_size > 0:
        existing = path.read_text(encoding="utf-8")
        if not existing.endswith("\n"):
            parts.append("\n")
        if not existing.endswith("\n\n"):
            parts.append("\n")

    if heading:
        parts.append(f"### {heading} ({ts})\n\n")
    else:
        parts.append(f"<!-- {ts} -->\n")

    parts.append(text.rstrip() + "\n")
    with path.open("a", encoding="utf-8") as f:
        f.write("".join(parts))
    return path


def append_finding_to_note(
    note_path: Path | str,
    *,
    finding_id: str,
    kind: str,
    summary: str,
    attempt_no: int | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    note_append: str | None = None,
) -> Path:
    """Append a structured finding entry to the experiment's Markdown note."""
    attempt_part = f" (Attempt #{attempt_no})" if attempt_no is not None else ""
    heading = f"[{kind}] {finding_id}{attempt_part}"
    lines = [summary.strip()]
    if artifacts:
        lines.append("")
        lines.append("**Artifacts:**")
        for item in artifacts:
            server = item.get("server", "")
            art_path = item.get("path", "")
            prefix = f"`{server}`: " if server else ""
            lines.append(f"- {prefix}`{art_path}`")
    if note_append and note_append.strip():
        lines.append("")
        lines.append(note_append.strip())
    return append_note(note_path, "\n".join(lines), heading=heading)


def save_patch(
    patches_dir: Path,
    project: str,
    exp_id: str,
    attempt_no: int,
    patch_content: str | bytes,
    max_bytes: int = DEFAULT_MAX_PATCH_BYTES,
) -> Path:
    """Save a git diff patch file, truncating safely at max_bytes."""
    path = patch_file_path(patches_dir, project, exp_id, attempt_no)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = patch_content.encode("utf-8") if isinstance(patch_content, str) else patch_content
    if len(raw) > max_bytes:
        notice = b"\n\n# [exploom] Patch truncated at 200KB limit\n"
        budget = max(0, max_bytes - len(notice))
        raw = raw[:budget].decode("utf-8", errors="ignore").encode("utf-8") + notice
    path.write_bytes(raw)
    return path


def summarize_patch_files(patch_path: Path | str | None) -> list[str]:
    """Extract the list of changed file paths from a unified diff patch."""
    if not patch_path:
        return []
    path = Path(patch_path)
    if not path.is_file():
        return []

    files: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        candidate: str | None = None
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                candidate = parts[3][2:]
        elif line.startswith("+++ b/"):
            candidate = line[6:].strip()
        if candidate and candidate != "/dev/null" and candidate not in seen:
            seen.add(candidate)
            files.append(candidate)
    return files
