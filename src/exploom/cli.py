"""Exploom CLI (Typer + Rich) with --json support on all commands."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from typing import Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from exploom.classify import parse_iso_timestamp
from exploom.integrate import apply_integration, resolve_integration_target
from exploom.service import ExploomService

app = typer.Typer(
    name="exploom",
    help="Local single-control-plane Experiment Lineage & Ops for AI Coding Agents.",
    no_args_is_help=True,
)
attempt_app = typer.Typer(help="Start or update experiment execution attempts.", no_args_is_help=True)
app.add_typer(attempt_app, name="attempt")

console = Console()
err_console = Console(stderr=True)

STATUS_STYLES: dict[str, str] = {
    "planned": "dim white",
    "running": "bold cyan",
    "done": "bold green",
    "failed_oom": "bold red",
    "failed_disk": "bold red",
    "failed_quota": "bold yellow",
    "failed_net": "bold yellow",
    "failed_code": "bold red",
    "aborted": "magenta",
    "archived": "dim",
    "superseded": "dim strike",
}


def _get_service() -> ExploomService:
    return ExploomService()


def _emit_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _fail(message: str, as_json: bool = False) -> None:
    if as_json:
        _emit_json({"error": message})
    else:
        err_console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code=1)


def _parse_metrics(items: list[str] | None) -> dict[str, Any] | None:
    if not items:
        return None
    result: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid metric '{item}': expected key=value")
        k, v = item.split("=", 1)
        key = k.strip()
        val_str = v.strip()
        if not key:
            raise ValueError(f"Invalid metric '{item}': empty key")
        # Try int -> float -> bool -> str
        val: Any = val_str
        try:
            val = int(val_str)
        except ValueError:
            try:
                val = float(val_str)
            except ValueError:
                if val_str.lower() == "true":
                    val = True
                elif val_str.lower() == "false":
                    val = False
        result[key] = val
    return result


def _parse_artifacts(items: list[str] | None) -> list[dict[str, str]] | None:
    if not items:
        return None
    artifacts: list[dict[str, str]] = []
    for item in items:
        if ":" in item and not item.startswith("/"):
            server, path = item.split(":", 1)
            artifacts.append({"server": server.strip(), "path": path.strip()})
        else:
            artifacts.append({"path": item.strip()})
    return artifacts


def _format_status_badge(
    status: str, is_stale: bool = False, stale_label: str | None = None, sync_error: str | None = None
) -> str:
    if is_stale:
        label = f"{status} ({stale_label or 'stale'})"
        badge = f"[bold yellow]{label}[/bold yellow]"
    else:
        style = STATUS_STYLES.get(status, "white")
        badge = f"[{style}]{status}[/{style}]"
    if sync_error:
        badge += f" [dim yellow][{sync_error}][/dim yellow]"
    return badge


def _format_metrics_inline(metrics: dict[str, Any]) -> str:
    if not metrics:
        return ""
    return " ".join(f"{k}={v}" for k, v in metrics.items())


def _format_short_ts(value: str | None) -> str:
    """Format an ISO8601 UTC timestamp in local time as 'YYYY-MM-DD HH:MM'."""
    if not value:
        return "-"
    dt = parse_iso_timestamp(value)
    if dt is None:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _format_timeline_span(
    started_at: str | None,
    ended_at: str | None,
    duration_label: str | None = None,
    *,
    is_running: bool = False,
) -> str:
    """Format a compact local-time span like '11-20 18:00→20:30 (2h30m)'."""
    if not started_at:
        return "-"
    start_dt = parse_iso_timestamp(started_at)
    if start_dt is None:
        return started_at
    local_start = start_dt.astimezone()
    s_str = local_start.strftime("%m-%d %H:%M")

    if ended_at:
        end_dt = parse_iso_timestamp(ended_at)
        if end_dt is not None:
            local_end = end_dt.astimezone()
            if local_end.date() == local_start.date():
                e_str = local_end.strftime("%H:%M")
            else:
                e_str = local_end.strftime("%m-%d %H:%M")
            span = f"{s_str}→{e_str}"
        else:
            span = f"{s_str}→{ended_at}"
    elif is_running:
        span = f"{s_str}→…"
    else:
        span = s_str

    if duration_label:
        span += f" ({duration_label})"
    return span


# ----------------------------------------------------------------------
# init
# ----------------------------------------------------------------------


@app.command("init")
def cmd_init(
    project: Optional[str] = typer.Argument(None, help="Project slug to bind to current directory."),
    dotfile: bool = typer.Option(False, "--dotfile", help="Also write .exploom-project file."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Bind the current working directory to a project."""
    try:
        svc = _get_service()
        res = svc.init_project(project=project, write_dotfile=dotfile)
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(res)
        return
    console.print(
        f"[bold green]Initialized project[/bold green] [cyan]{res['project']}[/cyan] -> {res['local_root']}"
    )
    if res.get("dotfile"):
        console.print(f"Wrote dotfile: {res['dotfile']}")


# ----------------------------------------------------------------------
# plan
# ----------------------------------------------------------------------


@app.command("plan")
def cmd_plan(
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Short experiment title."),
    exp_id: Optional[str] = typer.Option(None, "--exp-id", "-e", help="Update existing experiment ID."),
    parent: Optional[str] = typer.Option(None, "--parent", "-p", help="Parent experiment ID."),
    finding: Optional[str] = typer.Option(None, "--finding", "-f", help="Triggering finding ID (e.g. E011-F2)."),
    hypothesis: Optional[str] = typer.Option(None, "--hypothesis", "-H", help="Hypothesis description."),
    tag: Optional[list[str]] = typer.Option(None, "--tag", help="Tag (repeatable)."),
    metric: Optional[list[str]] = typer.Option(None, "--metric", "-m", help="Metric key=value (repeatable)."),
    status_override: Optional[str] = typer.Option(
        None, "--status-override", help="Override status: archived | superseded | '' (clear)."
    ),
    dashboard: Optional[str] = typer.Option(None, "--dashboard", help="Optional WandB/TensorBoard URL."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Create a new experiment node or update an existing one."""
    try:
        svc = _get_service()
        action = "update" if exp_id else "create"
        metrics_dict = _parse_metrics(metric)
        res = svc.plan_experiment(
            action=action,
            title=title,
            exp_id=exp_id,
            parent_id=parent,
            based_on_finding=finding,
            hypothesis=hypothesis,
            tags=tag,
            metrics=metrics_dict,
            status_override=status_override,
            dashboard_url=dashboard,
            project=project,
        )
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(res)
        return
    verb = "Updated" if exp_id else "Planned"
    parent_info = f" (parent: {res['parent_id']})" if res.get("parent_id") else ""
    console.print(
        f"[bold green]{verb}[/bold green] [cyan]{res['project']}/{res['exp_id']}[/cyan]{parent_info} "
        f"[{STATUS_STYLES.get(res['status'], 'white')}]{res['status']}[/]"
    )
    console.print(f"Note: {res['note_path']}")


# ----------------------------------------------------------------------
# attempt start / update
# ----------------------------------------------------------------------


@attempt_app.command("start")
def cmd_attempt_start(
    exp_id: str = typer.Argument(..., help="Experiment ID (e.g. E001)."),
    server: str = typer.Option("local", "--server", "-s", help="SSH host alias or 'local'."),
    workdir: str = typer.Option(".", "--workdir", "-w", help="Remote or local working directory."),
    cmd: str = typer.Option(..., "--cmd", "-c", help="Full launch command."),
    resume_cmd: Optional[str] = typer.Option(None, "--resume-cmd", help="Checkpoint resume command."),
    resource: Optional[str] = typer.Option(None, "--resource", "-r", help="Resource spec (e.g. gpu:0,1)."),
    runner: Optional[str] = typer.Option(
        None,
        "--runner",
        help="Runner type (ssh-tmux, ssh-nohup, ssh-screen, ssh-slurm, ssh-docker, local, custom).",
    ),
    log_path: Optional[str] = typer.Option(None, "--log-path", help="Remote/local main log path."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Register a new execution attempt and generate its wrapped command."""
    try:
        svc = _get_service()
        res = svc.start_attempt(
            exp_id=exp_id,
            server=server,
            workdir=workdir,
            run_cmd=cmd,
            resume_cmd=resume_cmd,
            resource=resource,
            runner=runner,
            log_path=log_path,
            project=project,
        )
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(res)
        return

    console.print(
        f"[bold cyan]Started[/bold cyan] {res['project']}/{res['exp_id']}#{res['attempt_no']} "
        f"runner=[bold]{res['runner']}[/bold] ref=[dim]{res['runner_ref']}[/dim]"
    )
    for warn in res["conflict_warnings"]:
        console.print(f"[bold yellow]Warning:[/bold yellow] {warn}")
    console.print(f"SSH/Exec cmd:\n  {res['ssh_cmd']}")


@attempt_app.command("update")
def cmd_attempt_update(
    exp_id: str = typer.Argument(..., help="Experiment ID (e.g. E001)."),
    attempt_no: Optional[int] = typer.Option(None, "--attempt", "-n", help="Attempt number (default: latest)."),
    server: Optional[str] = typer.Option(None, "--server", "-s", help="Update SSH host alias or 'local'."),
    workdir: Optional[str] = typer.Option(None, "--workdir", "-w", help="Update working directory."),
    resource: Optional[str] = typer.Option(None, "--resource", "-r", help="Update resource spec."),
    runner: Optional[str] = typer.Option(None, "--runner", help="Update runner type."),
    status: Optional[str] = typer.Option(None, "--status", help="Explicit attempt status."),
    exit_code: Optional[int] = typer.Option(None, "--exit-code", help="Process exit code (auto-classifies if status omitted)."),
    error: Optional[str] = typer.Option(None, "--error", help="Short error summary."),
    metric: Optional[list[str]] = typer.Option(None, "--metric", "-m", help="Metric key=value (repeatable)."),
    started_at: Optional[str] = typer.Option(None, "--started-at", help="Override start time (ISO8601)."),
    ended_at: Optional[str] = typer.Option(None, "--ended-at", help="Override end time (ISO8601)."),
    log_mtime: Optional[str] = typer.Option(None, "--log-mtime", help="Log mtime ISO8601 timestamp."),
    sync_error: Optional[str] = typer.Option(None, "--sync-error", help="Sync error tag (preserves status)."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Update an existing attempt's status, exit code, metrics, or error summary."""
    try:
        svc = _get_service()
        metrics_dict = _parse_metrics(metric)
        res = svc.update_attempt(
            exp_id=exp_id,
            attempt_no=attempt_no,
            server=server,
            workdir=workdir,
            resource=resource,
            runner=runner,
            status=status,
            exit_code=exit_code,
            error_summary=error,
            metrics=metrics_dict,
            started_at=started_at,
            ended_at=ended_at,
            log_mtime=log_mtime,
            sync_error=sync_error,
            project=project,
        )
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(res)
        return
    console.print(
        f"Updated {res['project']}/{res['exp_id']}#{res['attempt_no']} -> "
        f"{_format_status_badge(res['status'], sync_error=res.get('sync_error'))}"
    )


# ----------------------------------------------------------------------
# finding
# ----------------------------------------------------------------------


@app.command("finding")
def cmd_finding(
    exp_id: str = typer.Argument(..., help="Experiment ID (e.g. E001)."),
    kind: str = typer.Option("analysis", "--kind", "-k", help="analysis | diagnosis | decision | artifact"),
    summary: str = typer.Option(..., "--summary", "-s", help="One-line finding summary."),
    attempt_no: Optional[int] = typer.Option(None, "--attempt", "-n", help="Optional attempt number."),
    artifact: Optional[list[str]] = typer.Option(
        None, "--artifact", "-a", help="Artifact path or server:path (repeatable)."
    ),
    note: Optional[str] = typer.Option(None, "--note", help="Extra markdown note text to append."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Attach a finding, diagnosis, decision, or analysis artifact to an experiment."""
    try:
        svc = _get_service()
        res = svc.add_finding(
            exp_id=exp_id,
            kind=kind,
            summary=summary,
            attempt_no=attempt_no,
            artifacts=_parse_artifacts(artifact),
            note_append=note,
            project=project,
        )
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(res)
        return
    console.print(
        f"[bold green]Recorded[/bold green] [cyan]{res['finding_id']}[/cyan] ({res['kind']}) on {res['exp_id']}"
    )


# ----------------------------------------------------------------------
# tree
# ----------------------------------------------------------------------


def _render_tree_node(parent_branch: Tree, node: dict[str, Any]) -> None:
    badge = _format_status_badge(
        node["status"],
        is_stale=node["is_stale"],
        stale_label=node.get("stale_label"),
        sync_error=(node["latest_attempt"] or {}).get("sync_error"),
    )
    parts = [f"[bold]{node['id']}[/bold]", node["title"], badge]
    if node.get("based_on_finding"):
        parts.append(f"[dim](from {node['based_on_finding']})[/dim]")
    if node.get("tags"):
        parts.append(" ".join(f"[blue]#{t}[/blue]" for t in node["tags"]))
    if node.get("latest_attempt"):
        la = node["latest_attempt"]
        loc = la["server"]
        if la.get("resource"):
            loc += f"/{la['resource']}"
        parts.append(f"[dim]#{la['attempt_no']}@{loc}[/dim]")
    metrics_str = _format_metrics_inline(node.get("metrics") or {})
    if metrics_str:
        parts.append(f"[green]{metrics_str}[/green]")

    branch = parent_branch.add(" ".join(parts))

    for f in node.get("findings") or []:
        branch.add(f"[dim magenta]↳ {f['id']} [{f['kind']}]:[/dim magenta] {f['summary']}")

    for child in node.get("children") or []:
        _render_tree_node(branch, child)


@app.command("tree")
def cmd_tree(
    all_findings: bool = typer.Option(False, "-a", "--all-findings", help="Include findings under each experiment."),
    all_projects: bool = typer.Option(False, "--all", help="Show trees across all projects."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Display the experiment lineage tree."""
    try:
        svc = _get_service()
        data = svc.get_tree(project=project, include_findings=all_findings, all_projects=all_projects)
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(data)
        return

    tree_list = data["projects"] if all_projects else [data]
    if not tree_list:
        console.print("[dim]No projects found.[/dim]")
        return

    for proj_tree in tree_list:
        synced = proj_tree.get("last_synced_at") or "never"
        root = Tree(f"[bold cyan]{proj_tree['project']}[/bold cyan] [dim](last_sync: {synced})[/dim]")
        roots = proj_tree.get("roots") or []
        if not roots:
            root.add("[dim]No experiments yet.[/dim]")
        else:
            for node in roots:
                _render_tree_node(root, node)
        console.print(root)


# ----------------------------------------------------------------------
# ls
# ----------------------------------------------------------------------


@app.command("ls")
def cmd_ls(
    failed: bool = typer.Option(False, "--failed", help="Show only failed experiments."),
    running: bool = typer.Option(False, "--running", help="Show only running experiments."),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by exact status."),
    server: Optional[str] = typer.Option(None, "--server", help="Filter by server."),
    tag: Optional[str] = typer.Option(None, "--tag", help="Filter by tag."),
    sort_by: str = typer.Option("created", "--sort", help="Sort order: created | started | ended."),
    all_projects: bool = typer.Option(False, "--all", help="List across all projects."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """List experiments with optional status, server, or tag filters."""
    try:
        svc = _get_service()
        cards = svc.list_experiments(
            project=project,
            status_filter=status,
            failed_only=failed,
            running_only=running,
            server=server,
            tag=tag,
            sort_by=sort_by,
            all_projects=all_projects,
        )
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(cards)
        return

    table = Table(show_header=True, header_style="bold")
    if all_projects:
        table.add_column("Project", style="cyan")
    table.add_column("ID", style="bold", no_wrap=True)
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Server", no_wrap=True)
    table.add_column("Timeline", style="dim")
    table.add_column("Metrics", style="green")
    table.add_column("Tags", style="blue")

    for card in cards:
        row: list[str] = []
        if all_projects:
            row.append(card["project"])
        exp_label = card["id"]
        if card.get("attempt_no"):
            exp_label += f"#{card['attempt_no']}"
        row.extend(
            [
                exp_label,
                card["title"],
                _format_status_badge(
                    card["status"],
                    is_stale=card["is_stale"],
                    stale_label=card.get("stale_label"),
                    sync_error=card.get("sync_error"),
                ),
                card.get("server") or "-",
                _format_timeline_span(
                    card.get("started_at"),
                    card.get("ended_at"),
                    card.get("duration_label"),
                    is_running=(card["status"] == "running"),
                ),
                _format_metrics_inline(card.get("metrics") or {}) or "-",
                ", ".join(card.get("tags") or []) or "-",
            ]
        )
        table.add_row(*row)
    console.print(table)


# ----------------------------------------------------------------------
# show / edit
# ----------------------------------------------------------------------


@app.command("show")
def cmd_show(
    exp_id: str = typer.Argument(..., help="Experiment ID (e.g. E001)."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Show full context for an experiment (ancestors, attempts, findings, note)."""
    try:
        svc = _get_service()
        ctx = svc.get_context(exp_id, project=project)
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(ctx)
        return

    exp = ctx["experiment"]
    header = (
        f"[bold]{exp['id']}[/bold]: {exp['title']}  "
        f"{_format_status_badge(exp['status'], is_stale=exp['is_stale'])}"
    )
    meta_lines = [
        f"Project: [cyan]{ctx['project']}[/cyan] | Created: {_format_short_ts(exp.get('created_at'))} | Note: {exp['note_path']}"
    ]
    if ctx["ancestors"]:
        chain = " -> ".join(f"{a['id']} ({a['title']})" for a in ctx["ancestors"])
        meta_lines.append(f"Lineage: {chain} -> [bold]{exp['id']}[/bold]")
    if exp.get("based_on_finding"):
        meta_lines.append(f"Based on finding: {exp['based_on_finding']}")
    if exp.get("hypothesis"):
        meta_lines.append(f"Hypothesis: {exp['hypothesis']}")
    if exp.get("metrics"):
        meta_lines.append(f"Metrics: [green]{_format_metrics_inline(exp['metrics'])}[/green]")
    if ctx["changed_files"]:
        meta_lines.append(f"Patch files: {', '.join(ctx['changed_files'])}")

    console.print(Panel("\n".join(meta_lines), title=header, expand=False))

    if ctx["recent_attempts"]:
        att_table = Table(title=f"Recent Attempts ({len(ctx['recent_attempts'])}/{ctx['total_attempts']})")
        att_table.add_column("#", no_wrap=True)
        att_table.add_column("Server", no_wrap=True)
        att_table.add_column("Workdir")
        att_table.add_column("Resource")
        att_table.add_column("Status")
        att_table.add_column("Timeline", style="dim")
        att_table.add_column("Error")
        for att in ctx["recent_attempts"]:
            att_table.add_row(
                str(att["attempt_no"]),
                att["server"],
                att["workdir"],
                att.get("resource") or "-",
                _format_status_badge(att["status"], sync_error=att.get("sync_error")),
                _format_timeline_span(
                    att.get("started_at"),
                    att.get("ended_at"),
                    att.get("duration_label"),
                    is_running=(att["status"] == "running"),
                ),
                att.get("error_summary") or "-",
            )
        console.print(att_table)

    if ctx["findings"]:
        f_table = Table(title="Findings")
        f_table.add_column("ID", style="magenta")
        f_table.add_column("Kind")
        f_table.add_column("Summary")
        for f in ctx["findings"]:
            f_table.add_row(f["id"], f["kind"], f["summary"])
        console.print(f_table)


@app.command("edit")
def cmd_edit(
    exp_id: str = typer.Argument(..., help="Experiment ID (e.g. E001)."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
) -> None:
    """Open the experiment's Markdown note in $EDITOR."""
    try:
        svc = _get_service()
        ctx = svc.get_context(exp_id, project=project)
    except Exception as exc:
        _fail(str(exc), as_json=False)
        return

    note_path = ctx["experiment"]["note_path"]
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    cmd = [*shlex.split(editor), note_path]
    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        _fail(f"Editor '{editor}' not found. Set $EDITOR or open {note_path} directly.")


# ----------------------------------------------------------------------
# ps
# ----------------------------------------------------------------------


@app.command("ps")
def cmd_ps(
    current: bool = typer.Option(False, "--current", help="Only show running attempts for current project."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Show currently running experiments and server/resource occupation matrix."""
    try:
        svc = _get_service()
        all_projects = not current and project is None
        rows = svc.get_resources(project=project, all_projects=all_projects)
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(rows)
        return

    if not rows:
        console.print("[dim]No running experiments.[/dim]")
        return

    table = Table(title="Running Experiments & Resources", show_header=True, header_style="bold")
    table.add_column("Server", style="cyan", no_wrap=True)
    table.add_column("Resource", style="yellow")
    table.add_column("Exp", style="bold")
    table.add_column("Title")
    table.add_column("Workdir")
    table.add_column("Runner")
    table.add_column("Started", style="dim")
    table.add_column("State")

    for r in rows:
        state = _format_status_badge(
            "running",
            is_stale=r["is_stale"],
            stale_label=r.get("stale_label"),
            sync_error=r.get("sync_error"),
        )
        table.add_row(
            r["server"],
            r.get("resource") or "-",
            f"{r['project']}/{r['exp_id']}#{r['attempt_no']}",
            r["title"],
            r["workdir"],
            r["runner"],
            _format_timeline_span(
                r.get("started_at"),
                None,
                r.get("duration_label"),
                is_running=False,
            ),
            state,
        )
    console.print(table)


# ----------------------------------------------------------------------
# sync / log / rerun (M2 SSH Ops)
# ----------------------------------------------------------------------


@app.command("sync")
def cmd_sync(
    exp_id: Optional[str] = typer.Argument(None, help="Optional experiment ID to sync."),
    all_projects: bool = typer.Option(False, "--all", help="Sync running attempts across all projects."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Concurrently probe running experiments via read-only SSH and update status."""
    try:
        svc = _get_service()
        res = svc.sync_attempts(exp_id=exp_id, project=project, all_projects=all_projects)
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(res)
        return

    console.print(
        f"[bold cyan]Synced[/bold cyan] {res['synced_count']} attempt(s) at {res['last_synced_at']}"
    )
    for item in res["changed"]:
        console.print(
            f"  [bold]{item['project']}/{item['exp_id']}#{item['attempt_no']}[/bold] "
            f"({item['server']}): {item['old_status']} -> "
            f"{_format_status_badge(item['new_status'])}"
            + (f" | {item['error_summary']}" if item.get("error_summary") else "")
        )
    for item in res["stale"]:
        console.print(
            f"  [bold yellow]STALE[/bold yellow] {item['project']}/{item['exp_id']}#{item['attempt_no']} "
            f"({item['server']}): {item['stale_label']}"
        )
    for item in res["errors"]:
        console.print(
            f"  [bold red]SYNC ERROR[/bold red] {item['project']}/{item['exp_id']}#{item['attempt_no']} "
            f"({item['server']}): {item['sync_error']} (status preserved)"
        )


@app.command("log")
def cmd_log(
    exp_id: str = typer.Argument(..., help="Experiment ID (e.g. E001)."),
    lines: int = typer.Option(80, "-n", "--lines", help="Number of tail lines (1..200)."),
    attempt_no: Optional[int] = typer.Option(None, "--attempt", help="Attempt number (default: latest)."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Fetch the tail of an experiment's remote or local main log."""
    try:
        svc = _get_service()
        res = svc.tail_log(exp_id, attempt_no=attempt_no, lines=lines, project=project)
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(res)
        return

    if res.get("error"):
        _fail(res["error"], as_json=False)
        return

    console.print(
        f"[dim]--- {res['project']}/{res['exp_id']}#{res['attempt_no']} "
        f"({res['server']}:{res['log_path']}, last {res['lines']} lines) ---[/dim]"
    )
    typer.echo(res["content"], nl=False)


@app.command("rerun")
def cmd_rerun(
    exp_id: str = typer.Argument(..., help="Experiment ID (e.g. E001)."),
    fresh: bool = typer.Option(False, "--fresh", help="Use run_cmd instead of resume_cmd."),
    force: bool = typer.Option(False, "--force", help="Proceed even if code drift is detected."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only run drift check and print command without launching."),
    project: Optional[str] = typer.Option(None, "--project", "-P", help="Explicit project slug."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Check code drift and relaunch an experiment attempt."""
    try:
        svc = _get_service()
        if dry_run:
            res = svc.prepare_rerun(
                exp_id,
                use_resume=not fresh,
                force_on_drift=force,
                project=project,
            )
            res["launched"] = False
        else:
            res = svc.execute_rerun(
                exp_id,
                use_resume=not fresh,
                force_on_drift=force,
                project=project,
            )
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(res)
        if res.get("blocked") or res.get("launch_error"):
            raise typer.Exit(code=1)
        return

    if res.get("blocked"):
        drift = res.get("drift") or {}
        err_console.print(
            f"[bold yellow]Rerun blocked ({res['reason']}):[/bold yellow] "
            f"expected commit={drift.get('expected_commit')}, current={drift.get('current_commit')}, "
            f"diff_changed={drift.get('diff_changed')}. Pass --force to override."
        )
        raise typer.Exit(code=1)

    att = res["attempt"]
    if res.get("launch_error"):
        _fail(f"Failed to launch {att['exp_id']}#{att['attempt_no']}: {res['launch_error']}")
        return

    action_label = "Prepared (dry-run)" if dry_run else "Relaunched"
    console.print(
        f"[bold green]{action_label}[/bold green] {att['project']}/{att['exp_id']}#{att['attempt_no']} "
        f"on [cyan]{att['server']}[/cyan]"
    )
    if dry_run:
        console.print(f"SSH/Exec cmd:\n  {att['ssh_cmd']}")


@app.command("integrate")
def cmd_integrate(
    target: Optional[str] = typer.Option(
        None,
        "--target",
        "-t",
        help="Explicit AGENTS.md / CLAUDE.md path (defaults to ./AGENTS.md).",
    ),
    zcode: bool = typer.Option(False, "--zcode", help="Target global ~/.zcode/AGENTS.md."),
    claude: bool = typer.Option(False, "--claude", help="Target global ~/.claude/CLAUDE.md."),
    lang: str = typer.Option("en", "--lang", help="Snippet language: en or zh."),
    remove: bool = typer.Option(False, "--remove", help="Remove the managed Exploom block cleanly."),
    as_json: bool = typer.Option(False, "--json", help="Output JSON."),
) -> None:
    """Idempotently add, update, or remove Exploom subagent handoff rules in AGENTS.md / CLAUDE.md."""
    try:
        target_path = resolve_integration_target(target=target, zcode=zcode, claude=claude)
        res = apply_integration(target_path, lang=lang, remove=remove)
    except Exception as exc:
        _fail(str(exc), as_json=as_json)
        return

    if as_json:
        _emit_json(res)
        return

    action = res["action"]
    if action in ("created", "appended", "updated"):
        console.print(f"[bold green]{action.capitalize()}[/bold green] Exploom block in [cyan]{res['target']}[/cyan]")
    elif action == "removed":
        console.print(f"[bold yellow]Removed[/bold yellow] Exploom block from [cyan]{res['target']}[/cyan]")
    else:
        console.print(f"[dim]No changes needed[/dim] ([cyan]{res['target']}[/cyan])")


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
