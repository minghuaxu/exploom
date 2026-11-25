"""Exploom MCP Server exposing 5 low-ambiguity tools over stdio.

Security boundary:
- MCP tools ONLY perform local SQLite/file operations and read-only SSH probes.
- Remote launch and rerun commands are generated (`wrapped_cmd` / `ssh_cmd`) and
  returned to the Coding Agent to execute via its native Bash tool (preserving
  tool approval prompts and the user's real SSH agent / jump-host environment).
"""

from __future__ import annotations

from typing import Any, Literal

try:
    from mcp.server.mcpserver import MCPServer as _BaseMCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - mcp 1.x fallback
    from mcp.server.fastmcp import FastMCP as _BaseMCPServer  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

from exploom.service import ExploomService

MAX_LOG_LINES = 200
MAX_NOTE_CHARS = 12_000

SERVER_INSTRUCTIONS = (
    "Exploom is the local single-control-plane Experiment Lineage & Ops server. "
    "Use its 5 tools to plan experiments (exp_plan), register/update execution attempts "
    "(exp_attempt), record analysis findings (exp_finding), query local state (exp_query), "
    "and run read-only SSH ops (exp_ops). "
    "Never run remote training directly without exp_attempt(action='start'); always execute "
    "the returned ssh_cmd via the host agent's native Bash tool."
)


def _format_node_line(node: dict[str, Any]) -> str:
    status = node["status"]
    if node.get("is_stale"):
        status_str = f"{status} ({node.get('stale_label') or 'stale'})"
    else:
        status_str = status
    la = node.get("latest_attempt")
    sync_err = la.get("sync_error") if la else None
    if sync_err:
        status_str += f" [{sync_err}]"

    parts = [f"{node['id']} {node['title']} [{status_str}]"]
    if node.get("based_on_finding"):
        parts.append(f"(from {node['based_on_finding']})")
    if node.get("tags"):
        parts.append(" ".join(f"#{t}" for t in node["tags"]))
    if la:
        loc = la["server"]
        if la.get("resource"):
            loc += f"/{la['resource']}"
        parts.append(f"#{la['attempt_no']}@{loc}")
    metrics = node.get("metrics") or {}
    if metrics:
        parts.append(" ".join(f"{k}={v}" for k, v in metrics.items()))
    return " ".join(parts)


def _render_tree_lines(
    nodes: list[dict[str, Any]],
    prefix: str = "",
) -> list[str]:
    lines: list[str] = []
    total = len(nodes)
    for idx, node in enumerate(nodes):
        is_last = idx == total - 1
        connector = "└── " if is_last else "├── "
        child_prefix = prefix + ("    " if is_last else "│   ")
        lines.append(f"{prefix}{connector}{_format_node_line(node)}")
        for finding in node.get("findings") or []:
            lines.append(
                f"{child_prefix}↳ {finding['id']} [{finding['kind']}]: {finding['summary']}"
            )
        children = node.get("children") or []
        if children:
            lines.extend(_render_tree_lines(children, prefix=child_prefix))
    return lines


def format_compact_tree(proj_tree: dict[str, Any]) -> str:
    """Format a project's experiment lineage tree as compact, token-efficient plain text."""
    synced = proj_tree.get("last_synced_at") or "never"
    header = f"{proj_tree['project']} (last_sync: {synced})"
    roots = proj_tree.get("roots") or []
    if not roots:
        return f"{header}\n└── (no experiments)"
    return "\n".join([header, *_render_tree_lines(roots)])


def _truncate_note(note: str, max_chars: int = MAX_NOTE_CHARS) -> str:
    if len(note) <= max_chars:
        return note
    keep = max_chars - 80
    return note[:keep].rstrip() + "\n\n[... truncated; read note_path directly for full content ...]"


def create_mcp_server(service: ExploomService | None = None) -> _BaseMCPServer:
    """Create and configure the Exploom MCP server with its 5 core tools."""
    mcp = _BaseMCPServer(
        name="exploom",
        instructions=SERVER_INSTRUCTIONS,
    )

    def _get_service() -> ExploomService:
        return service if service is not None else ExploomService()

    def _run_tool(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except (ValueError, KeyError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(
        name="exp_plan",
        description=(
            "Create or update an experiment node in the lineage graph. "
            "Use action='create' before dispatching a subagent or starting a new experiment "
            "(supports parent_id like 'E001', based_on_finding as a bare finding ID like 'E004-F1' "
            "without summary text, tags such as '/side', and hypothesis). "
            "Keep `metrics` compact (3-6 short scalar KPIs like F1/loss/acc — put prose, verdicts, "
            "bug notes, hashes, or nested dicts in `exp_finding` instead). "
            "Use action='update' to update metrics, title, hypothesis, tags, dashboard_url, "
            "or status_override ('archived' | 'superseded' | '' to clear)."
        ),
    )
    def exp_plan(
        action: Literal["create", "update"] = "create",
        title: str | None = None,
        exp_id: str | None = None,
        parent_id: str | None = None,
        based_on_finding: str | None = None,
        hypothesis: str | None = None,
        tags: list[str] | None = None,
        metrics: dict[str, Any] | None = None,
        status_override: str | None = None,
        dashboard_url: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        svc = _get_service()
        return _run_tool(
            svc.plan_experiment,
            action=action,
            title=title,
            exp_id=exp_id,
            parent_id=parent_id,
            based_on_finding=based_on_finding,
            hypothesis=hypothesis,
            tags=tags,
            metrics=metrics,
            status_override=status_override,
            dashboard_url=dashboard_url,
            project=project,
        )

    @mcp.tool(
        name="exp_attempt",
        description=(
            "Register a new execution attempt (action='start'), backfill a historical or "
            "already-running attempt from memory without launching anything (action='record'), "
            "or update an existing attempt's status/metrics/error_summary (action='update'). "
            "`server` must be a single valid SSH host name (or 'local'/'unknown') — never "
            "concatenate multiple hosts; record separate attempts when a run spans multiple servers. "
            "Keep `metrics` compact (short scalar KPIs only). "
            "On action='start', checks server/GPU/workdir conflicts and returns wrapped_cmd "
            "and ssh_cmd WITHOUT executing them — execute ssh_cmd via the native Bash tool."
        ),
    )
    def exp_attempt(
        action: Literal["start", "record", "update"],
        exp_id: str,
        attempt_no: int | None = None,
        server: str | None = None,
        workdir: str | None = None,
        resource: str | None = None,
        run_cmd: str | None = None,
        resume_cmd: str | None = None,
        runner: str | None = None,
        runner_ref: str | None = None,
        log_path: str | None = None,
        status: str | None = None,
        exit_code: int | None = None,
        error_summary: str | None = None,
        metrics: dict[str, Any] | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        svc = _get_service()
        if action == "start":
            if not server or not server.strip():
                raise ToolError("server is required when action='start'")
            if not workdir or not workdir.strip():
                raise ToolError("workdir is required when action='start'")
            if not run_cmd or not run_cmd.strip():
                raise ToolError("run_cmd is required when action='start'")
            return _run_tool(
                svc.start_attempt,
                exp_id=exp_id,
                server=server.strip(),
                workdir=workdir.strip(),
                run_cmd=run_cmd,
                resume_cmd=resume_cmd,
                resource=resource,
                runner=runner,
                log_path=log_path,
                project=project,
            )
        if action == "record":
            return _run_tool(
                svc.record_attempt,
                exp_id=exp_id,
                server=server,
                workdir=workdir,
                run_cmd=run_cmd,
                resume_cmd=resume_cmd,
                resource=resource,
                runner=runner,
                runner_ref=runner_ref,
                log_path=log_path,
                status=status,
                exit_code=exit_code,
                error_summary=error_summary,
                metrics=metrics,
                started_at=started_at,
                ended_at=ended_at,
                project=project,
            )
        if action == "update":
            return _run_tool(
                svc.update_attempt,
                exp_id=exp_id,
                attempt_no=attempt_no,
                server=server,
                workdir=workdir,
                resource=resource,
                run_cmd=run_cmd,
                resume_cmd=resume_cmd,
                runner=runner,
                status=status,
                exit_code=exit_code,
                error_summary=error_summary,
                metrics=metrics,
                runner_ref=runner_ref,
                log_path=log_path,
                started_at=started_at,
                ended_at=ended_at,
                project=project,
            )
        raise ToolError(f"Invalid action '{action}': expected 'start', 'record', or 'update'")

    @mcp.tool(
        name="exp_finding",
        description=(
            "Attach an analysis finding, failure diagnosis, decision, or artifact reference "
            "(such as error analysis scripts or bad-case outputs) to an experiment, and "
            "automatically append it to the experiment's Markdown note."
        ),
    )
    def exp_finding(
        exp_id: str,
        summary: str,
        kind: Literal["analysis", "diagnosis", "decision", "artifact"] = "analysis",
        attempt_no: int | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        note_append: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        svc = _get_service()
        return _run_tool(
            svc.add_finding,
            exp_id=exp_id,
            kind=kind,
            summary=summary,
            attempt_no=attempt_no,
            artifacts=artifacts,
            note_append=note_append,
            project=project,
        )

    @mcp.tool(
        name="exp_query",
        description=(
            "Fast local read-only query over SQLite (zero SSH, controlled output size). "
            "views: 'tree' (compact lineage tree + last_synced_at), "
            "'list' (filtered experiment cards), "
            "'context' (full restore pack for exp_id: metadata, ancestors, last 2 attempts, "
            "findings, markdown note, patch file list), "
            "'resources' (currently occupied server/workdir/resource table across running jobs)."
        ),
    )
    def exp_query(
        view: Literal["tree", "list", "context", "resources"],
        exp_id: str | None = None,
        status_filter: str | None = None,
        server: str | None = None,
        tag: str | None = None,
        sort_by: Literal["created", "started", "ended"] = "created",
        include_findings: bool = False,
        all_projects: bool = False,
        project: str | None = None,
    ) -> dict[str, Any]:
        svc = _get_service()

        if view == "tree":
            raw = _run_tool(
                svc.get_tree,
                project=project,
                include_findings=include_findings,
                all_projects=all_projects,
            )
            if all_projects:
                return {
                    "projects": [
                        {
                            "project": p["project"],
                            "last_synced_at": p["last_synced_at"],
                            "tree_text": format_compact_tree(p),
                        }
                        for p in raw["projects"]
                    ]
                }
            return {
                "project": raw["project"],
                "last_synced_at": raw["last_synced_at"],
                "tree_text": format_compact_tree(raw),
            }

        if view == "list":
            try:
                cards = svc.list_experiments(
                    project=project,
                    status_filter=status_filter,
                    server=server,
                    tag=tag,
                    sort_by=sort_by,
                    all_projects=all_projects,
                )
            except (ValueError, KeyError) as exc:
                raise ToolError(str(exc)) from exc
            return {
                "count": len(cards),
                "experiments": cards,
            }

        if view == "context":
            if not exp_id:
                raise ToolError("exp_id is required when view='context'")
            ctx = _run_tool(svc.get_context, exp_id, project=project)
            ctx["note"] = _truncate_note(ctx.get("note") or "")
            return ctx

        if view == "resources":
            # Default to all projects when no explicit project is specified to prevent
            # cross-project GPU/workdir collisions on shared servers.
            scope_all = all_projects or (project is None)
            try:
                rows = svc.get_resources(project=project, all_projects=scope_all)
            except (ValueError, KeyError) as exc:
                raise ToolError(str(exc)) from exc
            return {
                "count": len(rows),
                "resources": rows,
            }

        raise ToolError(
            f"Invalid view '{view}': expected 'tree', 'list', 'context', or 'resources'"
        )

    @mcp.tool(
        name="exp_ops",
        description=(
            "The ONLY tool that performs read-only SSH operations. "
            "action='sync': concurrently probe running attempts, classify failures, detect stale logs. "
            "action='tail_log': fetch the last N lines (default 50, max 200) of an attempt's log. "
            "action='prepare_rerun': check remote code drift and return a new attempt's ssh_cmd "
            "for execution via the native Bash tool (never launches remotely itself)."
        ),
    )
    def exp_ops(
        action: Literal["sync", "tail_log", "prepare_rerun"],
        exp_id: str | None = None,
        attempt_no: int | None = None,
        use_resume: bool = True,
        force_on_drift: bool = False,
        lines: int = 50,
        all_projects: bool = False,
        project: str | None = None,
    ) -> dict[str, Any]:
        svc = _get_service()

        if action == "sync":
            return _run_tool(
                svc.sync_attempts,
                exp_id=exp_id,
                project=project,
                all_projects=all_projects,
            )

        if action == "tail_log":
            if not exp_id:
                raise ToolError("exp_id is required when action='tail_log'")
            clamped_lines = max(1, min(int(lines), MAX_LOG_LINES))
            return _run_tool(
                svc.tail_log,
                exp_id,
                attempt_no=attempt_no,
                lines=clamped_lines,
                project=project,
            )

        if action == "prepare_rerun":
            if not exp_id:
                raise ToolError("exp_id is required when action='prepare_rerun'")
            return _run_tool(
                svc.prepare_rerun,
                exp_id,
                use_resume=use_resume,
                force_on_drift=force_on_drift,
                project=project,
            )

        raise ToolError(
            f"Invalid action '{action}': expected 'sync', 'tail_log', or 'prepare_rerun'"
        )

    return mcp


def main() -> None:
    """Entry point for the `exploom-mcp` console script (stdio transport)."""
    server = create_mcp_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
