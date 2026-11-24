"""Core business service layer shared by CLI and MCP."""

from __future__ import annotations

import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

from exploom.classify import (
    ATTEMPT_STATUSES,
    EXPERIMENT_OVERRIDE_STATUSES,
    FAILED_STATUSES,
    FINDING_KINDS,
    check_stale,
    classify_result,
    format_duration_minutes,
    truncate_summary,
)
from exploom.config import ExploomConfig, load_config
from exploom.db import Database, utc_now_iso
from exploom.notes import (
    append_finding_to_note,
    ensure_note,
    note_file_path,
    read_note,
    save_patch,
    summarize_patch_files,
)
from exploom.project import bind_project, resolve_project
from exploom.runners import AttemptSpec, get_runner
from exploom.ssh import (
    CommandExecutor,
    check_code_drift,
    execute_launch_cmd,
    fetch_log_tail,
    probe_attempts_concurrent,
    run_shell_on_host,
)


def _parse_gpu_indices(resource: str | None) -> set[str] | None:
    """Extract GPU index tokens from strings like 'gpu:0,1' or 'cuda:2'."""
    if not resource:
        return None
    text = resource.strip().lower()
    for prefix in ("gpu:", "cuda:"):
        if text.startswith(prefix):
            rest = text[len(prefix) :]
            tokens = {tok.strip() for tok in rest.split(",") if tok.strip()}
            return tokens or None
    return None


def _resources_conflict(res_a: str | None, res_b: str | None) -> bool:
    if not res_a or not res_b:
        return False
    if res_a.strip().lower() == res_b.strip().lower():
        return True
    gpus_a = _parse_gpu_indices(res_a)
    gpus_b = _parse_gpu_indices(res_b)
    if gpus_a and gpus_b:
        return bool(gpus_a & gpus_b)
    return False


def _parse_slurm_exit_code(raw: str | None) -> int | None:
    """Parse Slurm ExitCode 'exit:signal' (e.g. '0:125' -> 125 or '1:0' -> 1)."""
    if not raw:
        return None
    text = raw.strip()
    if ":" in text:
        rc_str, sig_str = text.split(":", 1)
        rc = int(rc_str) if rc_str.isdigit() else 0
        sig = int(sig_str) if sig_str.isdigit() else 0
        if rc != 0:
            return rc
        if sig != 0:
            return 128 + sig if sig < 128 else sig
        return 0
    return int(text) if text.isdigit() else None


def _resolve_docker_container_name(
    resource: str | None, server: str, server_containers: dict[str, str]
) -> str | None:
    if resource:
        res = resource.strip()
        for prefix in ("docker:", "container:"):
            if res.lower().startswith(prefix):
                name = res[len(prefix) :].strip()
                if name:
                    return name
    return server_containers.get(server)


class ExploomService:
    """High-level orchestration over SQLite, Markdown notes, patches, runners, and SSH."""

    def __init__(
        self,
        config: ExploomConfig | None = None,
        home_dir: Path | str | None = None,
        executor: CommandExecutor = run_shell_on_host,
    ) -> None:
        self.config = config or load_config(home_dir=home_dir)
        self.config.ensure_dirs()
        self.db = Database(self.config.db_path)
        self.executor = executor

    # ------------------------------------------------------------------
    # Project resolution & init
    # ------------------------------------------------------------------

    def resolve_project(
        self, project: str | None = None, cwd: Path | str | None = None
    ) -> str:
        return resolve_project(self.db, explicit_project=project, cwd=cwd)

    def init_project(
        self,
        project: str | None = None,
        cwd: Path | str | None = None,
        *,
        write_dotfile: bool = False,
    ) -> dict[str, Any]:
        return bind_project(self.db, project=project, cwd=cwd, write_dotfile=write_dotfile)

    # ------------------------------------------------------------------
    # Status derivation
    # ------------------------------------------------------------------

    def derive_status(
        self,
        exp: dict[str, Any],
        latest_attempt: dict[str, Any] | None,
        *,
        now: datetime | None = None,
    ) -> tuple[str, bool, int | None]:
        """Derive effective experiment status and stale info.

        Returns (status, is_stale, stale_minutes).
        """
        override = exp.get("status_override")
        if override:
            return str(override), False, None
        if latest_attempt is None:
            return "planned", False, None
        att_status = str(latest_attempt["status"])
        is_stale, silent_min = check_stale(
            att_status,
            latest_attempt.get("log_mtime"),
            threshold_minutes=self.config.stale_threshold_minutes,
            now=now,
        )
        return att_status, is_stale, silent_min

    # ------------------------------------------------------------------
    # Experiment planning (exp_plan)
    # ------------------------------------------------------------------

    def plan_experiment(
        self,
        *,
        action: str = "create",
        title: str | None = None,
        exp_id: str | None = None,
        parent_id: str | None = None,
        based_on_finding: str | None = None,
        hypothesis: str | None = None,
        tags: list[str] | None = None,
        metrics: dict[str, Any] | None = None,
        status_override: str | None = None,
        clear_status_override: bool = False,
        dashboard_url: str | None = None,
        project: str | None = None,
        cwd: Path | str | None = None,
    ) -> dict[str, Any]:
        if action not in ("create", "update"):
            raise ValueError(f"Invalid action '{action}': expected 'create' or 'update'")

        if status_override is not None and status_override != "":
            if status_override not in EXPERIMENT_OVERRIDE_STATUSES:
                raise ValueError(
                    f"Invalid status_override '{status_override}': "
                    f"expected one of {EXPERIMENT_OVERRIDE_STATUSES}"
                )

        proj = self.resolve_project(project=project, cwd=cwd)

        if parent_id:
            if action == "update" and exp_id and parent_id == exp_id:
                raise ValueError("Experiment cannot be its own parent")
            if self.db.get_experiment(proj, parent_id) is None:
                raise ValueError(f"Parent experiment '{parent_id}' not found in project '{proj}'")
            if action == "update" and exp_id:
                cursor: str | None = parent_id
                seen: set[str] = {exp_id}
                while cursor:
                    if cursor in seen:
                        raise ValueError(
                            f"Setting parent_id='{parent_id}' on '{exp_id}' would create a cycle"
                        )
                    seen.add(cursor)
                    parent_row = self.db.get_experiment(proj, cursor)
                    cursor = parent_row["parent_id"] if parent_row else None

        if based_on_finding:
            normalized_finding = based_on_finding.strip()
            if self.db.get_finding(proj, normalized_finding) is None:
                m = re.match(r"^(E\d+-F\d+)(?:[\s:：—–\-].*)?$", normalized_finding)
                if m and self.db.get_finding(proj, m.group(1)) is not None:
                    normalized_finding = m.group(1)
                else:
                    raise ValueError(
                        f"Finding '{based_on_finding}' not found in project '{proj}'. "
                        "Pass a bare finding ID like 'E004-F1' created via exp_finding first."
                    )
            based_on_finding = normalized_finding

        if action == "create":
            if not title or not title.strip():
                raise ValueError("title is required when creating an experiment")

            def _note_factory(new_exp_id: str) -> str:
                return str(note_file_path(self.config.notes_dir, proj, new_exp_id))

            exp = self.db.create_experiment(
                project=proj,
                title=title.strip(),
                note_path_factory=_note_factory,
                parent_id=parent_id,
                based_on_finding=based_on_finding,
                hypothesis=(hypothesis or "").strip(),
                tags=tags,
                metrics=metrics,
                status_override=status_override or None,
                dashboard_url=dashboard_url,
            )
            ensure_note(
                self.config.notes_dir,
                proj,
                exp["id"],
                title=exp["title"],
                hypothesis=exp["hypothesis"],
                parent_id=exp["parent_id"],
                based_on_finding=exp["based_on_finding"],
                tags=exp["tags"],
            )
            derived_status, _, _ = self.derive_status(exp, None)
            return {
                "project": proj,
                "exp_id": exp["id"],
                "parent_id": exp["parent_id"],
                "note_path": exp["note_path"],
                "status": derived_status,
            }

        # action == "update"
        if not exp_id:
            raise ValueError("exp_id is required when action='update'")
        updated = self.db.update_experiment(
            project=proj,
            exp_id=exp_id,
            title=title.strip() if title is not None else None,
            parent_id=parent_id,
            based_on_finding=based_on_finding,
            hypothesis=hypothesis,
            tags=tags,
            metrics=metrics,
            status_override=status_override if status_override else None,
            clear_status_override=clear_status_override or (status_override == ""),
            dashboard_url=dashboard_url,
        )
        if updated is None:
            raise KeyError(f"Experiment '{exp_id}' not found in project '{proj}'")
        latest = self.db.get_latest_attempt(proj, exp_id)
        derived_status, _, _ = self.derive_status(updated, latest)
        return {
            "project": proj,
            "exp_id": updated["id"],
            "parent_id": updated["parent_id"],
            "note_path": updated["note_path"],
            "status": derived_status,
        }

    # ------------------------------------------------------------------
    # Attempt management (exp_attempt)
    # ------------------------------------------------------------------

    def check_resource_conflicts(
        self,
        *,
        server: str,
        workdir: str,
        resource: str | None = None,
    ) -> list[str]:
        """Return conflict warnings against currently running attempts on the same server."""
        warnings: list[str] = []
        running = self.db.list_attempts(status="running", server=server)
        for att in running:
            ref = f"{att['project']}/{att['exp_id']}#{att['attempt_no']}"
            if resource and _resources_conflict(resource, att.get("resource")):
                warnings.append(
                    f"Resource conflict on '{server}': {ref} is already running with resource '{att['resource']}'"
                )
            if workdir and att.get("workdir") == workdir:
                warnings.append(
                    f"Workdir conflict on '{server}': {ref} is already running in '{workdir}'"
                )
        return warnings

    def start_attempt(
        self,
        *,
        exp_id: str,
        server: str,
        workdir: str,
        run_cmd: str,
        resume_cmd: str | None = None,
        resource: str | None = None,
        runner: str | None = None,
        log_path: str | None = None,
        git_commit: str | None = None,
        git_branch: str | None = None,
        diff_hash: str | None = None,
        project: str | None = None,
        cwd: Path | str | None = None,
    ) -> dict[str, Any]:
        proj = self.resolve_project(project=project, cwd=cwd)
        if self.db.get_experiment(proj, exp_id) is None:
            raise KeyError(f"Experiment '{exp_id}' not found in project '{proj}'")

        resolved_runner = runner or ("local" if server == "local" else self.config.default_runner)
        conflicts = self.check_resource_conflicts(
            server=server, workdir=workdir, resource=resource
        )

        def _meta_factory(attempt_no: int) -> str:
            return self.config.get_meta_dir(server, proj, exp_id, attempt_no)

        def _ref_factory(attempt_no: int) -> str:
            base_ref = f"exp_{proj}_{exp_id}_{attempt_no}"
            if resolved_runner == "ssh-docker":
                container = _resolve_docker_container_name(
                    resource, server, self.config.server_docker_containers
                )
                if container:
                    return f"{container}:{base_ref}"
            return base_ref

        attempt = self.db.create_attempt(
            project=proj,
            exp_id=exp_id,
            server=server,
            workdir=workdir,
            run_cmd=run_cmd,
            resume_cmd=resume_cmd,
            resource=resource,
            runner=resolved_runner,
            log_path=log_path,
            meta_dir_factory=_meta_factory,
            runner_ref_factory=_ref_factory,
            status="running",
            git_commit=git_commit,
            git_branch=git_branch,
            diff_hash=diff_hash,
        )

        effective_log_path = attempt["log_path"] or f"{attempt['meta_dir']}/run.log"
        if attempt["log_path"] is None:
            attempt = (
                self.db.update_attempt(
                    project=proj,
                    exp_id=exp_id,
                    attempt_no=attempt["attempt_no"],
                    log_path=effective_log_path,
                )
                or attempt
            )

        runner_impl = get_runner(resolved_runner)
        spec = AttemptSpec(
            project=proj,
            exp_id=exp_id,
            attempt_no=int(attempt["attempt_no"]),
            server=server,
            workdir=workdir,
            run_cmd=run_cmd,
            meta_dir=attempt["meta_dir"],
            log_path=effective_log_path,
            runner=resolved_runner,
            runner_ref=attempt["runner_ref"],
            max_patch_bytes=self.config.max_patch_bytes,
        )
        wrapped_cmd = runner_impl.build_wrapped_cmd(spec)
        ssh_cmd = (
            wrapped_cmd
            if server == "local"
            else f"ssh {shlex.quote(server)} {shlex.quote(wrapped_cmd)}"
        )

        return {
            "project": proj,
            "exp_id": exp_id,
            "attempt_no": attempt["attempt_no"],
            "server": server,
            "runner": attempt["runner"],
            "runner_ref": attempt["runner_ref"],
            "meta_dir": attempt["meta_dir"],
            "log_path": effective_log_path,
            "wrapped_cmd": wrapped_cmd,
            "ssh_cmd": ssh_cmd,
            "conflict_warnings": conflicts,
            "status": attempt["status"],
        }

    def record_attempt(
        self,
        *,
        exp_id: str,
        server: str | None = None,
        workdir: str | None = None,
        run_cmd: str | None = None,
        resume_cmd: str | None = None,
        resource: str | None = None,
        runner: str | None = None,
        runner_ref: str | None = None,
        log_path: str | None = None,
        status: str | None = None,
        exit_code: int | None = None,
        error_summary: str | None = None,
        metrics: dict[str, Any] | None = None,
        git_commit: str | None = None,
        git_branch: str | None = None,
        diff_hash: str | None = None,
        project: str | None = None,
        cwd: Path | str | None = None,
    ) -> dict[str, Any]:
        """Backfill/record a historical or already-running attempt without generating ssh_cmd."""
        proj = self.resolve_project(project=project, cwd=cwd)
        if self.db.get_experiment(proj, exp_id) is None:
            raise KeyError(f"Experiment '{exp_id}' not found in project '{proj}'")

        resolved_status = status
        resolved_error = error_summary
        if resolved_status is None:
            if exit_code is not None:
                resolved_status, classified_err = classify_result(
                    exit_code,
                    custom_rules=self.config.custom_failure_rules,
                )
                if resolved_error is None:
                    resolved_error = classified_err
            else:
                resolved_status = "done"

        if resolved_status not in ATTEMPT_STATUSES:
            raise ValueError(
                f"Invalid attempt status '{resolved_status}': expected one of {ATTEMPT_STATUSES}"
            )

        if resolved_error is not None:
            resolved_error = truncate_summary(resolved_error)

        resolved_server = (server or "unknown").strip() or "unknown"
        resolved_workdir = (workdir or "").strip()
        resolved_run_cmd = (run_cmd or "").strip()
        if runner:
            resolved_runner = runner
        elif resolved_server == "local":
            resolved_runner = "local"
        else:
            resolved_runner = "custom"

        conflicts: list[str] = []
        if resolved_status == "running" and resolved_server != "unknown":
            conflicts = self.check_resource_conflicts(
                server=resolved_server,
                workdir=resolved_workdir,
                resource=resource,
            )

        def _meta_factory(attempt_no: int) -> str:
            return self.config.get_meta_dir(resolved_server, proj, exp_id, attempt_no)

        def _ref_factory(attempt_no: int) -> str:
            if runner_ref and runner_ref.strip():
                return runner_ref.strip()
            return f"backfill_{proj}_{exp_id}_{attempt_no}"

        attempt = self.db.create_attempt(
            project=proj,
            exp_id=exp_id,
            server=resolved_server,
            workdir=resolved_workdir,
            run_cmd=resolved_run_cmd,
            resume_cmd=resume_cmd,
            resource=resource,
            runner=resolved_runner,
            log_path=log_path,
            meta_dir_factory=_meta_factory,
            runner_ref_factory=_ref_factory,
            status=resolved_status,
            git_commit=git_commit,
            git_branch=git_branch,
            diff_hash=diff_hash,
        )

        updated = (
            self.db.update_attempt(
                project=proj,
                exp_id=exp_id,
                attempt_no=int(attempt["attempt_no"]),
                status=resolved_status,
                exit_code=exit_code,
                error_summary=resolved_error,
                metrics=metrics,
            )
            or attempt
        )

        return {
            "project": proj,
            "exp_id": exp_id,
            "attempt_no": updated["attempt_no"],
            "server": updated["server"],
            "runner": updated["runner"],
            "runner_ref": updated["runner_ref"],
            "log_path": updated["log_path"],
            "status": updated["status"],
            "exit_code": updated["exit_code"],
            "error_summary": updated["error_summary"],
            "conflict_warnings": conflicts,
        }

    def update_attempt(
        self,
        *,
        exp_id: str,
        attempt_no: int | None = None,
        status: str | None = None,
        exit_code: int | None = None,
        log_tail: str = "",
        error_summary: str | None = None,
        metrics: dict[str, Any] | None = None,
        runner_ref: str | None = None,
        log_mtime: str | None = None,
        last_synced_at: str | None = None,
        sync_error: str | None = None,
        clear_sync_error: bool = False,
        git_commit: str | None = None,
        git_branch: str | None = None,
        diff_hash: str | None = None,
        patch_content: str | bytes | None = None,
        project: str | None = None,
        cwd: Path | str | None = None,
    ) -> dict[str, Any]:
        proj = self.resolve_project(project=project, cwd=cwd)
        if attempt_no is None:
            latest = self.db.get_latest_attempt(proj, exp_id)
            if latest is None:
                raise KeyError(f"No attempts found for experiment '{exp_id}' in project '{proj}'")
            target_no = int(latest["attempt_no"])
        else:
            target_no = int(attempt_no)

        resolved_status = status
        resolved_error = error_summary
        if resolved_status is None and exit_code is not None:
            classified_status, classified_err = classify_result(
                exit_code,
                log_tail=log_tail,
                custom_rules=self.config.custom_failure_rules,
            )
            resolved_status = classified_status
            if resolved_error is None:
                resolved_error = classified_err

        if resolved_status is not None and resolved_status not in ATTEMPT_STATUSES:
            raise ValueError(
                f"Invalid attempt status '{resolved_status}': expected one of {ATTEMPT_STATUSES}"
            )

        if resolved_error is not None:
            resolved_error = truncate_summary(resolved_error)

        saved_patch_path: str | None = None
        if patch_content is not None:
            p = save_patch(
                self.config.patches_dir,
                proj,
                exp_id,
                target_no,
                patch_content,
                max_bytes=self.config.max_patch_bytes,
            )
            saved_patch_path = str(p)

        updated = self.db.update_attempt(
            project=proj,
            exp_id=exp_id,
            attempt_no=target_no,
            status=resolved_status,
            exit_code=exit_code,
            error_summary=resolved_error,
            metrics=metrics,
            runner_ref=runner_ref,
            log_mtime=log_mtime,
            last_synced_at=last_synced_at,
            sync_error=sync_error,
            clear_sync_error=clear_sync_error,
            git_commit=git_commit,
            git_branch=git_branch,
            diff_hash=diff_hash,
            patch_path=saved_patch_path,
        )
        if updated is None:
            raise KeyError(f"Attempt '{exp_id}#{target_no}' not found in project '{proj}'")

        return {
            "project": proj,
            "exp_id": exp_id,
            "attempt_no": updated["attempt_no"],
            "status": updated["status"],
            "exit_code": updated["exit_code"],
            "error_summary": updated["error_summary"],
            "sync_error": updated["sync_error"],
        }

    # ------------------------------------------------------------------
    # Findings (exp_finding)
    # ------------------------------------------------------------------

    def add_finding(
        self,
        *,
        exp_id: str,
        kind: str,
        summary: str,
        attempt_no: int | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        note_append: str | None = None,
        project: str | None = None,
        cwd: Path | str | None = None,
    ) -> dict[str, Any]:
        if kind not in FINDING_KINDS:
            raise ValueError(f"Invalid finding kind '{kind}': expected one of {FINDING_KINDS}")
        if not summary or not summary.strip():
            raise ValueError("summary is required for a finding")

        proj = self.resolve_project(project=project, cwd=cwd)
        exp = self.db.get_experiment(proj, exp_id)
        if exp is None:
            raise KeyError(f"Experiment '{exp_id}' not found in project '{proj}'")

        if attempt_no is not None:
            if self.db.get_attempt(proj, exp_id, attempt_no) is None:
                raise KeyError(f"Attempt '{exp_id}#{attempt_no}' not found in project '{proj}'")

        finding = self.db.create_finding(
            project=proj,
            exp_id=exp_id,
            kind=kind,
            summary=summary.strip(),
            attempt_no=attempt_no,
            artifacts=artifacts,
        )

        ensure_note(
            self.config.notes_dir,
            proj,
            exp_id,
            title=exp["title"],
            hypothesis=exp["hypothesis"],
            parent_id=exp["parent_id"],
            based_on_finding=exp["based_on_finding"],
            tags=exp["tags"],
        )
        append_finding_to_note(
            exp["note_path"],
            finding_id=finding["id"],
            kind=finding["kind"],
            summary=finding["summary"],
            attempt_no=finding["attempt_no"],
            artifacts=finding["artifacts"],
            note_append=note_append,
        )

        return {
            "project": proj,
            "finding_id": finding["id"],
            "exp_id": exp_id,
            "attempt_no": finding["attempt_no"],
            "kind": finding["kind"],
            "note_path": exp["note_path"],
        }

    # ------------------------------------------------------------------
    # Queries (exp_query: tree / list / context / resources)
    # ------------------------------------------------------------------

    def _latest_attempts_by_exp(
        self, project: str | None = None
    ) -> dict[tuple[str, str], dict[str, Any]]:
        latest_map: dict[tuple[str, str], dict[str, Any]] = {}
        for att in self.db.list_attempts(project=project):
            key = (att["project"], att["exp_id"])
            prev = latest_map.get(key)
            if prev is None or att["attempt_no"] > prev["attempt_no"]:
                latest_map[key] = att
        return latest_map

    def get_tree(
        self,
        *,
        project: str | None = None,
        include_findings: bool = False,
        all_projects: bool = False,
        cwd: Path | str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        target_projects: list[str]
        if all_projects:
            target_projects = [p["slug"] for p in self.db.list_projects()]
        else:
            target_projects = [self.resolve_project(project=project, cwd=cwd)]

        trees: list[dict[str, Any]] = []
        for proj in target_projects:
            proj_row = self.db.get_project(proj)
            exps = self.db.list_experiments(project=proj)
            latest_map = self._latest_attempts_by_exp(project=proj)
            findings_by_exp: dict[str, list[dict[str, Any]]] = {}
            if include_findings:
                for f in self.db.list_findings(project=proj):
                    findings_by_exp.setdefault(f["exp_id"], []).append(f)

            nodes: dict[str, dict[str, Any]] = {}
            for exp in exps:
                latest = latest_map.get((proj, exp["id"]))
                status, is_stale, silent_min = self.derive_status(exp, latest, now=now)
                stale_label = (
                    f"stale {format_duration_minutes(silent_min)}"
                    if is_stale and silent_min is not None
                    else None
                )
                node: dict[str, Any] = {
                    "id": exp["id"],
                    "title": exp["title"],
                    "parent_id": exp["parent_id"],
                    "based_on_finding": exp["based_on_finding"],
                    "status": status,
                    "status_override": exp["status_override"],
                    "is_stale": is_stale,
                    "stale_minutes": silent_min,
                    "stale_label": stale_label,
                    "tags": exp["tags"],
                    "metrics": exp["metrics"],
                    "latest_attempt": (
                        {
                            "attempt_no": latest["attempt_no"],
                            "server": latest["server"],
                            "resource": latest["resource"],
                            "status": latest["status"],
                            "error_summary": latest["error_summary"],
                            "sync_error": latest["sync_error"],
                        }
                        if latest
                        else None
                    ),
                    "children": [],
                }
                if include_findings:
                    node["findings"] = [
                        {
                            "id": f["id"],
                            "kind": f["kind"],
                            "summary": f["summary"],
                            "attempt_no": f["attempt_no"],
                        }
                        for f in findings_by_exp.get(exp["id"], [])
                    ]
                nodes[exp["id"]] = node

            roots: list[dict[str, Any]] = []
            for exp in exps:
                node = nodes[exp["id"]]
                parent_id = exp["parent_id"]
                if parent_id and parent_id in nodes and parent_id != exp["id"]:
                    nodes[parent_id]["children"].append(node)
                else:
                    roots.append(node)

            trees.append(
                {
                    "project": proj,
                    "last_synced_at": proj_row["last_synced_at"] if proj_row else None,
                    "roots": roots,
                }
            )

        if all_projects:
            return {"projects": trees}
        return trees[0] if trees else {"project": target_projects[0], "last_synced_at": None, "roots": []}

    def list_experiments(
        self,
        *,
        project: str | None = None,
        status_filter: str | None = None,
        failed_only: bool = False,
        running_only: bool = False,
        server: str | None = None,
        tag: str | None = None,
        all_projects: bool = False,
        cwd: Path | str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        proj = None if all_projects else self.resolve_project(project=project, cwd=cwd)
        exps = self.db.list_experiments(project=proj)
        latest_map = self._latest_attempts_by_exp(project=proj)

        cards: list[dict[str, Any]] = []
        for exp in exps:
            latest = latest_map.get((exp["project"], exp["id"]))
            status, is_stale, silent_min = self.derive_status(exp, latest, now=now)

            if status_filter and status != status_filter:
                continue
            if failed_only and status not in FAILED_STATUSES:
                continue
            if running_only and status != "running":
                continue
            if server and (latest is None or latest["server"] != server):
                continue
            if tag and tag not in exp["tags"]:
                continue

            cards.append(
                {
                    "project": exp["project"],
                    "id": exp["id"],
                    "parent_id": exp["parent_id"],
                    "title": exp["title"],
                    "status": status,
                    "is_stale": is_stale,
                    "stale_label": (
                        f"stale {format_duration_minutes(silent_min)}"
                        if is_stale and silent_min is not None
                        else None
                    ),
                    "tags": exp["tags"],
                    "metrics": exp["metrics"],
                    "server": latest["server"] if latest else None,
                    "attempt_no": latest["attempt_no"] if latest else None,
                    "error_summary": latest["error_summary"] if latest else None,
                    "sync_error": latest["sync_error"] if latest else None,
                    "updated_at": exp["updated_at"],
                }
            )
        return cards

    def get_context(
        self,
        exp_id: str,
        *,
        project: str | None = None,
        cwd: Path | str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        proj = self.resolve_project(project=project, cwd=cwd)
        exp = self.db.get_experiment(proj, exp_id)
        if exp is None:
            raise KeyError(f"Experiment '{exp_id}' not found in project '{proj}'")

        all_attempts = self.db.list_attempts(project=proj, exp_id=exp_id)
        latest = all_attempts[-1] if all_attempts else None
        status, is_stale, silent_min = self.derive_status(exp, latest, now=now)

        # Walk ancestor chain (with cycle guard)
        ancestors: list[dict[str, Any]] = []
        visited = {exp_id}
        curr_parent = exp.get("parent_id")
        while curr_parent and curr_parent not in visited:
            visited.add(curr_parent)
            p_exp = self.db.get_experiment(proj, curr_parent)
            if p_exp is None:
                break
            p_latest = self.db.get_latest_attempt(proj, curr_parent)
            p_status, _, _ = self.derive_status(p_exp, p_latest, now=now)
            ancestors.append(
                {
                    "id": p_exp["id"],
                    "title": p_exp["title"],
                    "status": p_status,
                    "metrics": p_exp["metrics"],
                }
            )
            curr_parent = p_exp.get("parent_id")
        ancestors.reverse()

        recent_attempts = all_attempts[-2:]
        findings = self.db.list_findings(project=proj, exp_id=exp_id)
        note_content = read_note(exp["note_path"])

        changed_files: list[str] = []
        for att in reversed(all_attempts):
            if att.get("patch_path"):
                changed_files = summarize_patch_files(att["patch_path"])
                break

        return {
            "project": proj,
            "experiment": {
                **exp,
                "status": status,
                "is_stale": is_stale,
                "stale_minutes": silent_min,
            },
            "ancestors": ancestors,
            "total_attempts": len(all_attempts),
            "recent_attempts": recent_attempts,
            "findings": findings,
            "note": note_content,
            "changed_files": changed_files,
        }

    def get_resources(
        self,
        *,
        project: str | None = None,
        all_projects: bool = True,
        cwd: Path | str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        proj = None if all_projects else self.resolve_project(project=project, cwd=cwd)
        running = self.db.list_attempts(project=proj, status="running")
        exp_cache: dict[tuple[str, str], dict[str, Any] | None] = {}

        rows: list[dict[str, Any]] = []
        for att in running:
            key = (att["project"], att["exp_id"])
            if key not in exp_cache:
                exp_cache[key] = self.db.get_experiment(att["project"], att["exp_id"])
            exp = exp_cache[key]
            is_stale, silent_min = check_stale(
                att["status"],
                att.get("log_mtime"),
                threshold_minutes=self.config.stale_threshold_minutes,
                now=now,
            )
            rows.append(
                {
                    "project": att["project"],
                    "exp_id": att["exp_id"],
                    "attempt_no": att["attempt_no"],
                    "title": exp["title"] if exp else "",
                    "server": att["server"],
                    "workdir": att["workdir"],
                    "resource": att["resource"],
                    "runner": att["runner"],
                    "runner_ref": att["runner_ref"],
                    "started_at": att["started_at"],
                    "is_stale": is_stale,
                    "stale_minutes": silent_min,
                    "stale_label": (
                        f"stale {format_duration_minutes(silent_min)}"
                        if is_stale and silent_min is not None
                        else None
                    ),
                    "sync_error": att["sync_error"],
                }
            )
        rows.sort(key=lambda r: (r["server"], r["resource"] or "", r["project"], r["exp_id"]))
        return rows

    # ------------------------------------------------------------------
    # Ops (exp_ops: sync / tail_log / prepare_rerun / execute_rerun)
    # ------------------------------------------------------------------

    def sync_attempts(
        self,
        *,
        exp_id: str | None = None,
        project: str | None = None,
        all_projects: bool = False,
        cwd: Path | str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Concurrently probe running attempts (or a specific experiment's latest attempt)."""
        proj = None if all_projects else self.resolve_project(project=project, cwd=cwd)
        if exp_id is not None:
            target_proj = proj or self.resolve_project(project=project, cwd=cwd)
            latest = self.db.get_latest_attempt(target_proj, exp_id)
            if latest is None:
                raise KeyError(
                    f"No attempts found for experiment '{exp_id}' in project '{target_proj}'"
                )
            targets = [latest]
        else:
            targets = self.db.list_attempts(project=proj, status="running")

        synced_at = utc_now_iso()
        if not targets:
            if proj:
                if self.db.get_project(proj) is not None:
                    self.db.update_project_synced_at(proj, synced_at)
            return {
                "synced_count": 0,
                "changed": [],
                "stale": [],
                "errors": [],
                "last_synced_at": synced_at,
            }

        probes = probe_attempts_concurrent(
            targets,
            connect_timeout=self.config.ssh_connect_timeout,
            executor=self.executor,
        )

        changed: list[dict[str, Any]] = []
        stale_list: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        touched_projects: set[str] = set()

        for att in targets:
            p_slug = att["project"]
            eid = att["exp_id"]
            ano = int(att["attempt_no"])
            touched_projects.add(p_slug)
            probe = probes.get((p_slug, eid, ano))
            if probe is None:
                continue

            # Guardrail #1: SSH unreachable must NOT overwrite existing status
            if probe.sync_error:
                self.db.update_attempt(
                    project=p_slug,
                    exp_id=eid,
                    attempt_no=ano,
                    last_synced_at=synced_at,
                    sync_error=probe.sync_error,
                )
                errors.append(
                    {
                        "project": p_slug,
                        "exp_id": eid,
                        "attempt_no": ano,
                        "server": att["server"],
                        "sync_error": probe.sync_error,
                    }
                )
                continue

            meta = probe.meta or {}
            patch_path: str | None = None
            if probe.dirty_patch and not att.get("patch_path"):
                saved = save_patch(
                    self.config.patches_dir,
                    p_slug,
                    eid,
                    ano,
                    probe.dirty_patch,
                    max_bytes=self.config.max_patch_bytes,
                )
                patch_path = str(saved)

            old_status = att["status"]
            new_status = old_status
            exit_code = att.get("exit_code")
            error_summary = att.get("error_summary")
            ended_at = att.get("ended_at")

            slurm_state = (probe.slurm_state or "").strip()
            slurm_state_upper = slurm_state.upper()
            slurm_terminal = bool(
                slurm_state_upper
                and not slurm_state_upper.startswith(
                    ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "STAGE_OUT", "SUSPENDED")
                )
            )

            if probe.exit_info is not None:
                raw_rc = probe.exit_info.get("exit_code")
                exit_code = int(raw_rc) if raw_rc is not None else 1
                ended_at = probe.exit_info.get("ended_at") or synced_at
                combined_tail = (
                    f"{slurm_state_upper}\n{probe.log_tail}" if slurm_terminal else probe.log_tail
                )
                if slurm_state_upper.startswith("CANCELLED"):
                    new_status = "aborted"
                    error_summary = truncate_summary(f"Slurm job {slurm_state}")
                else:
                    new_status, error_summary = classify_result(
                        exit_code,
                        log_tail=combined_tail,
                        custom_rules=self.config.custom_failure_rules,
                    )
            elif slurm_terminal:
                ended_at = synced_at
                parsed_rc = _parse_slurm_exit_code(probe.slurm_exit_code)
                if slurm_state_upper.startswith("COMPLETED"):
                    exit_code = 0
                    new_status = "done"
                    error_summary = None
                elif slurm_state_upper.startswith("CANCELLED"):
                    exit_code = parsed_rc if parsed_rc is not None else 143
                    new_status = "aborted"
                    error_summary = truncate_summary(f"Slurm job {slurm_state}")
                else:
                    if parsed_rc is not None and parsed_rc != 0:
                        exit_code = parsed_rc
                    else:
                        exit_code = 137 if "OUT_OF_MEMORY" in slurm_state_upper else 1
                    combined_tail = f"Slurm state: {slurm_state_upper}\n{probe.log_tail}"
                    new_status, error_summary = classify_result(
                        exit_code,
                        log_tail=combined_tail,
                        custom_rules=self.config.custom_failure_rules,
                    )
            elif not probe.alive:
                # Process vanished without exit.json (e.g. external kill / host reboot)
                ended_at = synced_at
                new_status, classified_err = classify_result(
                    1,
                    log_tail=probe.log_tail,
                    custom_rules=self.config.custom_failure_rules,
                )
                error_summary = (
                    classified_err or "Process terminated unexpectedly without exit.json"
                )

            updated = self.db.update_attempt(
                project=p_slug,
                exp_id=eid,
                attempt_no=ano,
                status=new_status,
                exit_code=exit_code,
                error_summary=error_summary,
                log_mtime=probe.log_mtime,
                ended_at=ended_at,
                last_synced_at=synced_at,
                clear_sync_error=True,
                git_commit=meta.get("git_commit") or att.get("git_commit"),
                git_branch=meta.get("git_branch") or att.get("git_branch"),
                diff_hash=meta.get("diff_hash") or att.get("diff_hash"),
                patch_path=patch_path,
            )

            if new_status != old_status:
                changed.append(
                    {
                        "project": p_slug,
                        "exp_id": eid,
                        "attempt_no": ano,
                        "server": att["server"],
                        "old_status": old_status,
                        "new_status": new_status,
                        "exit_code": exit_code,
                        "error_summary": error_summary,
                    }
                )
            elif updated and updated["status"] == "running":
                is_stale, silent_min = check_stale(
                    "running",
                    updated.get("log_mtime"),
                    threshold_minutes=self.config.stale_threshold_minutes,
                    now=now,
                )
                if is_stale and silent_min is not None:
                    stale_list.append(
                        {
                            "project": p_slug,
                            "exp_id": eid,
                            "attempt_no": ano,
                            "server": att["server"],
                            "silent_minutes": silent_min,
                            "stale_label": f"stale {format_duration_minutes(silent_min)}",
                        }
                    )

        for p_slug in touched_projects:
            self.db.update_project_synced_at(p_slug, synced_at)

        return {
            "synced_count": len(targets),
            "changed": changed,
            "stale": stale_list,
            "errors": errors,
            "last_synced_at": synced_at,
        }

    def tail_log(
        self,
        exp_id: str,
        *,
        attempt_no: int | None = None,
        lines: int = 50,
        project: str | None = None,
        cwd: Path | str | None = None,
    ) -> dict[str, Any]:
        """Fetch the last N lines of an experiment attempt's log via read-only SSH/local."""
        proj = self.resolve_project(project=project, cwd=cwd)
        if attempt_no is None:
            att = self.db.get_latest_attempt(proj, exp_id)
        else:
            att = self.db.get_attempt(proj, exp_id, attempt_no)
        if att is None:
            raise KeyError(f"Attempt not found for experiment '{exp_id}' in project '{proj}'")

        log_path = att.get("log_path") or f"{att['meta_dir']}/run.log"
        res = fetch_log_tail(
            server=att["server"],
            log_path=log_path,
            lines=lines,
            connect_timeout=self.config.ssh_connect_timeout,
            executor=self.executor,
        )
        return {
            "project": proj,
            "exp_id": exp_id,
            "attempt_no": att["attempt_no"],
            "server": att["server"],
            "log_path": res.log_path,
            "lines": res.lines,
            "content": res.content,
            "error": res.error,
        }

    def prepare_rerun(
        self,
        exp_id: str,
        *,
        use_resume: bool = True,
        force_on_drift: bool = False,
        project: str | None = None,
        cwd: Path | str | None = None,
    ) -> dict[str, Any]:
        """Check code drift on server:workdir and prepare a new attempt for rerun.

        Never executes the launch command itself (preserves MCP approval boundary).
        """
        proj = self.resolve_project(project=project, cwd=cwd)
        prev = self.db.get_latest_attempt(proj, exp_id)
        if prev is None:
            raise KeyError(f"No previous attempts found for '{exp_id}' in project '{proj}'")

        drift = check_code_drift(
            server=prev["server"],
            workdir=prev["workdir"],
            expected_commit=prev.get("git_commit"),
            expected_diff_hash=prev.get("diff_hash"),
            connect_timeout=self.config.ssh_connect_timeout,
            executor=self.executor,
        )

        if drift.error and not force_on_drift:
            return {
                "project": proj,
                "exp_id": exp_id,
                "blocked": True,
                "reason": drift.error,
                "drift": drift.to_dict(),
                "attempt": None,
            }

        if drift.drifted and not force_on_drift:
            return {
                "project": proj,
                "exp_id": exp_id,
                "blocked": True,
                "reason": "code_drift",
                "drift": drift.to_dict(),
                "attempt": None,
            }

        chosen_cmd = (prev.get("resume_cmd") or prev["run_cmd"]) if use_resume else prev["run_cmd"]
        new_attempt = self.start_attempt(
            exp_id=exp_id,
            server=prev["server"],
            workdir=prev["workdir"],
            run_cmd=chosen_cmd,
            resume_cmd=prev.get("resume_cmd"),
            resource=prev.get("resource"),
            runner=prev.get("runner"),
            git_commit=drift.current_commit,
            git_branch=drift.current_branch,
            diff_hash=drift.current_diff_hash,
            project=proj,
            cwd=cwd,
        )
        return {
            "project": proj,
            "exp_id": exp_id,
            "blocked": False,
            "reason": None,
            "drift": drift.to_dict(),
            "used_resume_cmd": bool(use_resume and prev.get("resume_cmd")),
            "attempt": new_attempt,
        }

    def execute_rerun(
        self,
        exp_id: str,
        *,
        use_resume: bool = True,
        force_on_drift: bool = False,
        project: str | None = None,
        cwd: Path | str | None = None,
    ) -> dict[str, Any]:
        """Human CLI helper: prepare_rerun and immediately launch via SSH/local if unblocked."""
        prep = self.prepare_rerun(
            exp_id,
            use_resume=use_resume,
            force_on_drift=force_on_drift,
            project=project,
            cwd=cwd,
        )
        if prep["blocked"] or not prep["attempt"]:
            return {**prep, "launched": False, "launch_error": None}

        att = prep["attempt"]
        out = execute_launch_cmd(
            server=att["server"] if "server" in att else self.db.get_attempt(prep["project"], exp_id, att["attempt_no"])["server"],
            wrapped_cmd=att["wrapped_cmd"],
            connect_timeout=self.config.ssh_connect_timeout,
            executor=self.executor,
        )
        if out.unreachable or out.returncode != 0:
            err_msg = out.error or out.stderr.strip() or f"Launch exited with code {out.returncode}"
            return {**prep, "launched": False, "launch_error": err_msg}
        return {**prep, "launched": True, "launch_error": None}
