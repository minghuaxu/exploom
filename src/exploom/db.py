"""SQLite WAL storage, schema migrations, and atomic ID allocation."""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

SCHEMA_VERSION = 1

SCHEMA_V1_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    slug            TEXT PRIMARY KEY,
    local_root      TEXT,
    next_exp_seq    INTEGER NOT NULL DEFAULT 1,
    last_synced_at  TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    project          TEXT NOT NULL REFERENCES projects(slug),
    id               TEXT NOT NULL,
    parent_id        TEXT,
    based_on_finding TEXT,
    title            TEXT NOT NULL,
    hypothesis       TEXT NOT NULL DEFAULT '',
    tags_json        TEXT NOT NULL DEFAULT '[]',
    status_override  TEXT,
    metrics_json     TEXT NOT NULL DEFAULT '{}',
    dashboard_url    TEXT,
    note_path        TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (project, id)
);

CREATE TABLE IF NOT EXISTS attempts (
    project          TEXT NOT NULL,
    exp_id           TEXT NOT NULL,
    attempt_no       INTEGER NOT NULL,
    server           TEXT NOT NULL,
    workdir          TEXT NOT NULL,
    resource         TEXT,
    run_cmd          TEXT NOT NULL,
    resume_cmd       TEXT,
    runner           TEXT NOT NULL DEFAULT 'ssh-tmux',
    runner_ref       TEXT,
    log_path         TEXT,
    meta_dir         TEXT NOT NULL,
    git_commit       TEXT,
    git_branch       TEXT,
    diff_hash        TEXT,
    patch_path       TEXT,
    status           TEXT NOT NULL,
    exit_code        INTEGER,
    error_summary    TEXT,
    metrics_json     TEXT NOT NULL DEFAULT '{}',
    log_mtime        TEXT,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    last_synced_at   TEXT,
    sync_error       TEXT,
    PRIMARY KEY (project, exp_id, attempt_no),
    FOREIGN KEY (project, exp_id) REFERENCES experiments(project, id)
);

CREATE TABLE IF NOT EXISTS findings (
    project          TEXT NOT NULL,
    id               TEXT NOT NULL,
    exp_id           TEXT NOT NULL,
    attempt_no       INTEGER,
    kind             TEXT NOT NULL,
    summary          TEXT NOT NULL,
    artifacts_json   TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    PRIMARY KEY (project, id),
    FOREIGN KEY (project, exp_id) REFERENCES experiments(project, id)
);

CREATE INDEX IF NOT EXISTS idx_experiments_parent
    ON experiments(project, parent_id);
CREATE INDEX IF NOT EXISTS idx_attempts_status
    ON attempts(project, status);
CREATE INDEX IF NOT EXISTS idx_attempts_server_status
    ON attempts(server, status);
CREATE INDEX IF NOT EXISTS idx_findings_exp
    ON findings(project, exp_id);
"""


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA_V1_SQL.strip().split(";"):
        sql = stmt.strip()
        if sql:
            conn.execute(sql)


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_v1,
}


class SchemaVersionError(RuntimeError):
    """Raised when the database schema is newer than the running Exploom version."""


class Database:
    """SQLite WAL database wrapper for Exploom."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        """Open a new configured SQLite connection (autocommit mode for explicit locking)."""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def read_conn(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """Execute a write transaction with BEGIN IMMEDIATE to avoid WAL upgrade deadlocks."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        self.migrate(target_version=SCHEMA_VERSION, migrations=MIGRATIONS)

    def get_schema_version(self) -> int:
        with self.read_conn() as conn:
            row = conn.execute("PRAGMA user_version;").fetchone()
            return int(row[0]) if row else 0

    def migrate(
        self,
        target_version: int = SCHEMA_VERSION,
        migrations: dict[int, Callable[[sqlite3.Connection], None]] = MIGRATIONS,
    ) -> int:
        """Run incremental schema migrations using PRAGMA user_version.

        Creates a registry.db.bak backup before upgrading an existing schema (version > 0).
        """
        current = self.get_schema_version()
        if current > target_version:
            raise SchemaVersionError(
                f"Database schema version {current} is newer than supported version {target_version}."
            )
        if current == target_version:
            return current

        if current > 0 and self.db_path.exists():
            backup_path = self.db_path.with_name(f"{self.db_path.name}.bak")
            # Checkpoint WAL before backing up so the backup is self-contained
            with self.read_conn() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            shutil.copy2(self.db_path, backup_path)

        with self.write_tx() as conn:
            # Re-check inside lock in case another process migrated concurrently
            actual = int(conn.execute("PRAGMA user_version;").fetchone()[0])
            for version in range(actual + 1, target_version + 1):
                fn = migrations.get(version)
                if fn is None:
                    raise RuntimeError(f"Missing migration function for schema version {version}")
                fn(conn)
                conn.execute(f"PRAGMA user_version = {version};")
            return target_version

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def ensure_project(
        self,
        slug: str,
        local_root: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Ensure project row exists; optionally bind local_root."""
        now = utc_now_iso()

        def _op(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
            if row is None:
                c.execute(
                    """
                    INSERT INTO projects (slug, local_root, next_exp_seq, last_synced_at, created_at)
                    VALUES (?, ?, 1, NULL, ?)
                    """,
                    (slug, local_root, now),
                )
            elif local_root is not None and row["local_root"] != local_root:
                c.execute(
                    "UPDATE projects SET local_root = ? WHERE slug = ?",
                    (local_root, slug),
                )
            updated = c.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
            return dict(updated)

        if conn is not None:
            return _op(conn)
        with self.write_tx() as c:
            return _op(c)

    def get_project(self, slug: str) -> dict[str, Any] | None:
        with self.read_conn() as conn:
            row = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
            return dict(row) if row else None

    def list_projects(self) -> list[dict[str, Any]]:
        with self.read_conn() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY slug ASC").fetchall()
            return [dict(r) for r in rows]

    def update_project_synced_at(self, slug: str, synced_at: str | None = None) -> None:
        ts = synced_at or utc_now_iso()
        with self.write_tx() as conn:
            conn.execute("UPDATE projects SET last_synced_at = ? WHERE slug = ?", (ts, slug))

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------

    def create_experiment(
        self,
        *,
        project: str,
        title: str,
        note_path_factory: Callable[[str], str],
        parent_id: str | None = None,
        based_on_finding: str | None = None,
        hypothesis: str = "",
        tags: list[str] | None = None,
        metrics: dict[str, Any] | None = None,
        status_override: str | None = None,
        dashboard_url: str | None = None,
        local_root: str | None = None,
    ) -> dict[str, Any]:
        """Atomically allocate an E0xx ID and insert a new experiment."""
        now = utc_now_iso()
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        metrics_json = json.dumps(metrics or {}, ensure_ascii=False)

        with self.write_tx() as conn:
            self.ensure_project(project, local_root=local_root, conn=conn)
            seq_row = conn.execute(
                "SELECT next_exp_seq FROM projects WHERE slug = ?", (project,)
            ).fetchone()
            seq = int(seq_row["next_exp_seq"])
            exp_id = f"E{seq:03d}"
            conn.execute(
                "UPDATE projects SET next_exp_seq = ? WHERE slug = ?",
                (seq + 1, project),
            )

            note_path = note_path_factory(exp_id)
            conn.execute(
                """
                INSERT INTO experiments (
                    project, id, parent_id, based_on_finding, title, hypothesis,
                    tags_json, status_override, metrics_json, dashboard_url,
                    note_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project,
                    exp_id,
                    parent_id,
                    based_on_finding,
                    title,
                    hypothesis,
                    tags_json,
                    status_override,
                    metrics_json,
                    dashboard_url,
                    note_path,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM experiments WHERE project = ? AND id = ?",
                (project, exp_id),
            ).fetchone()
            return self._deserialize_experiment(row)

    def update_experiment(
        self,
        *,
        project: str,
        exp_id: str,
        title: str | None = None,
        parent_id: str | None = None,
        based_on_finding: str | None = None,
        hypothesis: str | None = None,
        tags: list[str] | None = None,
        metrics: dict[str, Any] | None = None,
        status_override: str | None = None,
        clear_status_override: bool = False,
        dashboard_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Update mutable fields on an existing experiment."""
        now = utc_now_iso()
        with self.write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM experiments WHERE project = ? AND id = ?",
                (project, exp_id),
            ).fetchone()
            if row is None:
                return None

            existing = self._deserialize_experiment(row)
            new_title = title if title is not None else existing["title"]
            new_parent = parent_id if parent_id is not None else existing["parent_id"]
            new_finding = (
                based_on_finding if based_on_finding is not None else existing["based_on_finding"]
            )
            new_hypothesis = hypothesis if hypothesis is not None else existing["hypothesis"]
            new_tags = tags if tags is not None else existing["tags"]
            merged_metrics = dict(existing["metrics"])
            if metrics:
                merged_metrics.update(metrics)
            if clear_status_override:
                new_override = None
            elif status_override is not None:
                new_override = status_override
            else:
                new_override = existing["status_override"]
            new_dashboard = dashboard_url if dashboard_url is not None else existing["dashboard_url"]

            conn.execute(
                """
                UPDATE experiments
                SET title = ?, parent_id = ?, based_on_finding = ?, hypothesis = ?,
                    tags_json = ?, status_override = ?, metrics_json = ?,
                    dashboard_url = ?, updated_at = ?
                WHERE project = ? AND id = ?
                """,
                (
                    new_title,
                    new_parent,
                    new_finding,
                    new_hypothesis,
                    json.dumps(new_tags, ensure_ascii=False),
                    new_override,
                    json.dumps(merged_metrics, ensure_ascii=False),
                    new_dashboard,
                    now,
                    project,
                    exp_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM experiments WHERE project = ? AND id = ?",
                (project, exp_id),
            ).fetchone()
            return self._deserialize_experiment(updated)

    def get_experiment(self, project: str, exp_id: str) -> dict[str, Any] | None:
        with self.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM experiments WHERE project = ? AND id = ?",
                (project, exp_id),
            ).fetchone()
            return self._deserialize_experiment(row) if row else None

    def list_experiments(self, project: str | None = None) -> list[dict[str, Any]]:
        with self.read_conn() as conn:
            if project is not None:
                rows = conn.execute(
                    "SELECT * FROM experiments WHERE project = ? ORDER BY created_at ASC, id ASC",
                    (project,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM experiments ORDER BY project ASC, created_at ASC, id ASC"
                ).fetchall()
            return [self._deserialize_experiment(r) for r in rows]

    # ------------------------------------------------------------------
    # Attempts
    # ------------------------------------------------------------------

    def create_attempt(
        self,
        *,
        project: str,
        exp_id: str,
        server: str,
        workdir: str,
        run_cmd: str,
        meta_dir_factory: Callable[[int], str],
        runner_ref_factory: Callable[[int], str],
        resume_cmd: str | None = None,
        resource: str | None = None,
        runner: str = "ssh-tmux",
        log_path: str | None = None,
        status: str = "running",
        started_at: str | None = None,
        ended_at: str | None = None,
        git_commit: str | None = None,
        git_branch: str | None = None,
        diff_hash: str | None = None,
        patch_path: str | None = None,
    ) -> dict[str, Any]:
        """Atomically allocate attempt_no and insert a new attempt."""
        now = utc_now_iso()
        effective_started_at = started_at or now
        with self.write_tx() as conn:
            exp_row = conn.execute(
                "SELECT id FROM experiments WHERE project = ? AND id = ?",
                (project, exp_id),
            ).fetchone()
            if exp_row is None:
                raise KeyError(f"Experiment {project}/{exp_id} not found")

            max_row = conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) AS max_no FROM attempts WHERE project = ? AND exp_id = ?",
                (project, exp_id),
            ).fetchone()
            attempt_no = int(max_row["max_no"]) + 1
            meta_dir = meta_dir_factory(attempt_no)
            runner_ref = runner_ref_factory(attempt_no)

            conn.execute(
                """
                INSERT INTO attempts (
                    project, exp_id, attempt_no, server, workdir, resource,
                    run_cmd, resume_cmd, runner, runner_ref, log_path, meta_dir,
                    git_commit, git_branch, diff_hash, patch_path,
                    status, exit_code, error_summary, metrics_json,
                    log_mtime, started_at, ended_at, last_synced_at, sync_error
                ) VALUES (
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, NULL, NULL, '{}',
                    NULL, ?, ?, NULL, NULL
                )
                """,
                (
                    project,
                    exp_id,
                    attempt_no,
                    server,
                    workdir,
                    resource,
                    run_cmd,
                    resume_cmd,
                    runner,
                    runner_ref,
                    log_path,
                    meta_dir,
                    git_commit,
                    git_branch,
                    diff_hash,
                    patch_path,
                    status,
                    effective_started_at,
                    ended_at,
                ),
            )
            conn.execute(
                "UPDATE experiments SET updated_at = ? WHERE project = ? AND id = ?",
                (now, project, exp_id),
            )
            row = conn.execute(
                "SELECT * FROM attempts WHERE project = ? AND exp_id = ? AND attempt_no = ?",
                (project, exp_id, attempt_no),
            ).fetchone()
            return self._deserialize_attempt(row)

    def update_attempt(
        self,
        *,
        project: str,
        exp_id: str,
        attempt_no: int,
        server: str | None = None,
        workdir: str | None = None,
        resource: str | None = None,
        run_cmd: str | None = None,
        resume_cmd: str | None = None,
        runner: str | None = None,
        status: str | None = None,
        exit_code: int | None = None,
        error_summary: str | None = None,
        metrics: dict[str, Any] | None = None,
        runner_ref: str | None = None,
        log_path: str | None = None,
        log_mtime: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        last_synced_at: str | None = None,
        sync_error: str | None = None,
        clear_sync_error: bool = False,
        git_commit: str | None = None,
        git_branch: str | None = None,
        diff_hash: str | None = None,
        patch_path: str | None = None,
    ) -> dict[str, Any] | None:
        """Update fields on an existing attempt and sync experiment metrics/updated_at."""
        now = utc_now_iso()
        with self.write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE project = ? AND exp_id = ? AND attempt_no = ?",
                (project, exp_id, attempt_no),
            ).fetchone()
            if row is None:
                return None

            existing = self._deserialize_attempt(row)
            new_server = server.strip() if server is not None and server.strip() else existing["server"]
            new_workdir = workdir if workdir is not None else existing["workdir"]
            new_resource = resource if resource is not None else existing["resource"]
            new_run_cmd = run_cmd if run_cmd is not None else existing["run_cmd"]
            new_resume_cmd = resume_cmd if resume_cmd is not None else existing["resume_cmd"]
            new_runner = runner if runner is not None else existing["runner"]
            new_status = status if status is not None else existing["status"]
            new_exit = exit_code if exit_code is not None else existing["exit_code"]
            new_err = error_summary if error_summary is not None else existing["error_summary"]
            merged_metrics = dict(existing["metrics"])
            if metrics:
                merged_metrics.update(metrics)
            new_runner_ref = runner_ref if runner_ref is not None else existing["runner_ref"]
            new_log_path = log_path if log_path is not None else existing["log_path"]
            new_log_mtime = log_mtime if log_mtime is not None else existing["log_mtime"]
            new_started_at = started_at if started_at is not None else existing["started_at"]
            new_ended_at = ended_at if ended_at is not None else existing["ended_at"]
            if new_ended_at is None and new_status != "running":
                new_ended_at = now
            new_synced = last_synced_at if last_synced_at is not None else existing["last_synced_at"]
            if clear_sync_error:
                new_sync_err = None
            elif sync_error is not None:
                new_sync_err = sync_error
            else:
                new_sync_err = existing["sync_error"]
            new_commit = git_commit if git_commit is not None else existing["git_commit"]
            new_branch = git_branch if git_branch is not None else existing["git_branch"]
            new_diff = diff_hash if diff_hash is not None else existing["diff_hash"]
            new_patch = patch_path if patch_path is not None else existing["patch_path"]

            conn.execute(
                """
                UPDATE attempts
                SET server = ?, workdir = ?, resource = ?, run_cmd = ?, resume_cmd = ?, runner = ?,
                    status = ?, exit_code = ?, error_summary = ?, metrics_json = ?,
                    runner_ref = ?, log_path = ?, log_mtime = ?, started_at = ?, ended_at = ?,
                    last_synced_at = ?, sync_error = ?, git_commit = ?, git_branch = ?,
                    diff_hash = ?, patch_path = ?
                WHERE project = ? AND exp_id = ? AND attempt_no = ?
                """,
                (
                    new_server,
                    new_workdir,
                    new_resource,
                    new_run_cmd,
                    new_resume_cmd,
                    new_runner,
                    new_status,
                    new_exit,
                    new_err,
                    json.dumps(merged_metrics, ensure_ascii=False),
                    new_runner_ref,
                    new_log_path,
                    new_log_mtime,
                    new_started_at,
                    new_ended_at,
                    new_synced,
                    new_sync_err,
                    new_commit,
                    new_branch,
                    new_diff,
                    new_patch,
                    project,
                    exp_id,
                    attempt_no,
                ),
            )

            # Propagate metrics snapshot and updated_at to parent experiment
            if metrics:
                exp_row = conn.execute(
                    "SELECT metrics_json FROM experiments WHERE project = ? AND id = ?",
                    (project, exp_id),
                ).fetchone()
                exp_metrics = json.loads(exp_row["metrics_json"]) if exp_row else {}
                exp_metrics.update(metrics)
                conn.execute(
                    "UPDATE experiments SET metrics_json = ?, updated_at = ? WHERE project = ? AND id = ?",
                    (json.dumps(exp_metrics, ensure_ascii=False), now, project, exp_id),
                )
            else:
                conn.execute(
                    "UPDATE experiments SET updated_at = ? WHERE project = ? AND id = ?",
                    (now, project, exp_id),
                )

            updated = conn.execute(
                "SELECT * FROM attempts WHERE project = ? AND exp_id = ? AND attempt_no = ?",
                (project, exp_id, attempt_no),
            ).fetchone()
            return self._deserialize_attempt(updated)

    def get_attempt(self, project: str, exp_id: str, attempt_no: int) -> dict[str, Any] | None:
        with self.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE project = ? AND exp_id = ? AND attempt_no = ?",
                (project, exp_id, attempt_no),
            ).fetchone()
            return self._deserialize_attempt(row) if row else None

    def get_latest_attempt(self, project: str, exp_id: str) -> dict[str, Any] | None:
        with self.read_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM attempts
                WHERE project = ? AND exp_id = ?
                ORDER BY attempt_no DESC
                LIMIT 1
                """,
                (project, exp_id),
            ).fetchone()
            return self._deserialize_attempt(row) if row else None

    def list_attempts(
        self,
        project: str | None = None,
        exp_id: str | None = None,
        status: str | None = None,
        server: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if exp_id is not None:
            clauses.append("exp_id = ?")
            params.append(exp_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if server is not None:
            clauses.append("server = ?")
            params.append(server)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM attempts {where} ORDER BY project ASC, exp_id ASC, attempt_no ASC"
        with self.read_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._deserialize_attempt(r) for r in rows]

    # ------------------------------------------------------------------
    # Findings
    # ------------------------------------------------------------------

    def create_finding(
        self,
        *,
        project: str,
        exp_id: str,
        kind: str,
        summary: str,
        attempt_no: int | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Atomically allocate <exp_id>-F<n> and insert a finding."""
        now = utc_now_iso()
        artifacts_json = json.dumps(artifacts or [], ensure_ascii=False)
        with self.write_tx() as conn:
            exp_row = conn.execute(
                "SELECT id FROM experiments WHERE project = ? AND id = ?",
                (project, exp_id),
            ).fetchone()
            if exp_row is None:
                raise KeyError(f"Experiment {project}/{exp_id} not found")

            rows = conn.execute(
                "SELECT id FROM findings WHERE project = ? AND exp_id = ?",
                (project, exp_id),
            ).fetchall()
            max_idx = 0
            prefix = f"{exp_id}-F"
            for r in rows:
                fid = str(r["id"])
                if fid.startswith(prefix):
                    suffix = fid[len(prefix) :]
                    if suffix.isdigit():
                        max_idx = max(max_idx, int(suffix))
            finding_id = f"{prefix}{max_idx + 1}"

            conn.execute(
                """
                INSERT INTO findings (
                    project, id, exp_id, attempt_no, kind, summary, artifacts_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project, finding_id, exp_id, attempt_no, kind, summary, artifacts_json, now),
            )
            conn.execute(
                "UPDATE experiments SET updated_at = ? WHERE project = ? AND id = ?",
                (now, project, exp_id),
            )
            row = conn.execute(
                "SELECT * FROM findings WHERE project = ? AND id = ?",
                (project, finding_id),
            ).fetchone()
            return self._deserialize_finding(row)

    def get_finding(self, project: str, finding_id: str) -> dict[str, Any] | None:
        with self.read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM findings WHERE project = ? AND id = ?",
                (project, finding_id),
            ).fetchone()
            return self._deserialize_finding(row) if row else None

    def list_findings(
        self, project: str | None = None, exp_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if exp_id is not None:
            clauses.append("exp_id = ?")
            params.append(exp_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM findings {where} ORDER BY created_at ASC, id ASC"
        with self.read_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._deserialize_finding(r) for r in rows]

    # ------------------------------------------------------------------
    # Deserializers
    # ------------------------------------------------------------------

    @staticmethod
    def _deserialize_experiment(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["tags"] = json.loads(d.pop("tags_json", "[]") or "[]")
        d["metrics"] = json.loads(d.pop("metrics_json", "{}") or "{}")
        return d

    @staticmethod
    def _deserialize_attempt(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["metrics"] = json.loads(d.pop("metrics_json", "{}") or "{}")
        return d

    @staticmethod
    def _deserialize_finding(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["artifacts"] = json.loads(d.pop("artifacts_json", "[]") or "[]")
        return d
