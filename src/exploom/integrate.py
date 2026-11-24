"""Idempotent AGENTS.md / CLAUDE.md integration helper for Exploom."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

START_MARKER = "<!-- exploom:start -->"
END_MARKER = "<!-- exploom:end -->"

_BLOCK_RE = re.compile(
    re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
    flags=re.DOTALL,
)

SNIPPETS: dict[str, str] = {
    "en": f"""{START_MARKER}
## Exploom Experiment Tracking & Subagent Handoff

- Before launching a remote/local training or eval run, or dispatching a subagent for an experiment, call `exp_plan` to allocate an ID and start the subagent prompt with `[EXP_ID=E0xx]`.
- Any subagent receiving `[EXP_ID=E0xx]` must first call `exp_query(view="context", exp_id="E0xx")` (or `exploom show E0xx --json` when MCP is unavailable) to restore context, launch runs via `exp_attempt(action="start")` (single SSH host per attempt; keep `metrics` compact scalar KPIs), and persist conclusions via `exp_finding`.
- Subagents should report back to the main agent in at most 3 compact lines: `E0xx#<attempt> | status & key metrics/error | finding ID`.
{END_MARKER}""",
    "zh": f"""{START_MARKER}
## Exploom 实验血缘与子智能体接力

- 提交远端/本地训练或评估，或派发子智能体处理实验前，先调 `exp_plan` 分配 ID，并在子智能体任务提示词开头写入 `[EXP_ID=E0xx]`。
- 收到 `[EXP_ID=E0xx]` 的子智能体先调 `exp_query(view="context", exp_id="E0xx")`（无 MCP 时用 `exploom show E0xx --json`）还原上下文，通过 `exp_attempt(action="start")` 启动任务（单条 attempt 只写单个真实 SSH host，`metrics` 仅保留简短核心数值），并用 `exp_finding` 落盘结论。
- 子智能体仅向主智能体回报 3 行精简摘要：`E0xx#<attempt> | 状态与核心指标/报错 | finding ID`。
{END_MARKER}""",
}


def resolve_integration_target(
    *,
    target: Path | str | None = None,
    zcode: bool = False,
    claude: bool = False,
    cwd: Path | None = None,
    home: Path | None = None,
) -> Path:
    selected = sum([bool(target), bool(zcode), bool(claude)])
    if selected > 1:
        raise ValueError("Specify at most one of --target, --zcode, or --claude")
    home_dir = home or Path.home()
    if target:
        return Path(target).expanduser().resolve()
    if zcode:
        return (home_dir / ".zcode" / "AGENTS.md").resolve()
    if claude:
        return (home_dir / ".claude" / "CLAUDE.md").resolve()
    return ((cwd or Path.cwd()) / "AGENTS.md").resolve()


def apply_integration(
    target_path: Path,
    *,
    lang: str = "en",
    remove: bool = False,
) -> dict[str, Any]:
    """Idempotently add, update, or remove the Exploom block in target_path."""
    if lang not in SNIPPETS:
        raise ValueError(f"Unsupported lang '{lang}': expected one of {tuple(SNIPPETS)}")

    if remove:
        if not target_path.exists():
            return {"target": str(target_path), "action": "noop", "changed": False}
        original = target_path.read_text(encoding="utf-8")
        if not _BLOCK_RE.search(original):
            return {"target": str(target_path), "action": "noop", "changed": False}
        cleaned = _BLOCK_RE.sub("", original)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        final_text = (cleaned + "\n") if cleaned else ""
        target_path.write_text(final_text, encoding="utf-8")
        return {"target": str(target_path), "action": "removed", "changed": True}

    snippet = SNIPPETS[lang]
    if not target_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(snippet + "\n", encoding="utf-8")
        return {"target": str(target_path), "action": "created", "lang": lang, "changed": True}

    original = target_path.read_text(encoding="utf-8")
    if _BLOCK_RE.search(original):
        updated = _BLOCK_RE.sub(lambda _: snippet, original, count=1)
        if updated == original:
            return {"target": str(target_path), "action": "unchanged", "lang": lang, "changed": False}
        target_path.write_text(updated, encoding="utf-8")
        return {"target": str(target_path), "action": "updated", "lang": lang, "changed": True}

    prefix = original.rstrip()
    joined = (prefix + "\n\n" + snippet + "\n") if prefix else (snippet + "\n")
    target_path.write_text(joined, encoding="utf-8")
    return {"target": str(target_path), "action": "appended", "lang": lang, "changed": True}
