"""Tests for SQLite WAL database, migrations, and concurrency safety."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from exploom.db import MIGRATIONS, SCHEMA_VERSION, Database, SchemaVersionError


def test_wal_and_pragmas(tmp_path: Path) -> None:
    db = Database(tmp_path / "registry.db")
    with db.read_conn() as conn:
        journal = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        version = conn.execute("PRAGMA user_version;").fetchone()[0]
    assert journal.lower() == "wal"
    assert fk == 1
    assert version == SCHEMA_VERSION


def test_schema_migration_and_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "registry.db"
    db = Database(db_path)
    db.ensure_project("proj-a")
    assert db.get_schema_version() == 1

    def _migrate_v2(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE projects ADD COLUMN description TEXT DEFAULT '';")

    extended = {**MIGRATIONS, 2: _migrate_v2}
    new_ver = db.migrate(target_version=2, migrations=extended)
    assert new_ver == 2
    assert db.get_schema_version() == 2

    backup_path = tmp_path / "registry.db.bak"
    assert backup_path.is_file()

    # Verify backup still has v1 schema while active DB has v2
    bak_conn = sqlite3.connect(str(backup_path))
    assert bak_conn.execute("PRAGMA user_version;").fetchone()[0] == 1
    bak_conn.close()

    # Verify newer schema raises SchemaVersionError
    with pytest.raises(SchemaVersionError):
        db.migrate(target_version=1, migrations=MIGRATIONS)


def test_concurrent_wal_writes_and_reads(tmp_path: Path) -> None:
    """Multiple threads creating experiments/attempts/findings concurrently must not lock or collide."""
    db = Database(tmp_path / "registry.db")
    db.ensure_project("stress")

    num_workers = 12
    exps_per_worker = 4

    def worker(worker_idx: int) -> list[str]:
        created_ids: list[str] = []
        for i in range(exps_per_worker):
            exp = db.create_experiment(
                project="stress",
                title=f"w{worker_idx}_e{i}",
                note_path_factory=lambda eid: str(tmp_path / f"{eid}.md"),
            )
            created_ids.append(exp["id"])
            att = db.create_attempt(
                project="stress",
                exp_id=exp["id"],
                server="gpu-1",
                workdir="/work",
                run_cmd="python train.py",
                meta_dir_factory=lambda n: f"/tmp/meta/{exp['id']}_{n}",
                runner_ref_factory=lambda n: f"ref_{exp['id']}_{n}",
            )
            assert att["attempt_no"] == 1
            finding = db.create_finding(
                project="stress",
                exp_id=exp["id"],
                kind="analysis",
                summary=f"finding from worker {worker_idx}",
            )
            assert finding["id"] == f"{exp['id']}-F1"
            # Interleaved read
            assert db.get_experiment("stress", exp["id"]) is not None
        return created_ids

    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        results = list(pool.map(worker, range(num_workers)))

    all_ids = [eid for batch in results for eid in batch]
    expected_total = num_workers * exps_per_worker
    assert len(all_ids) == expected_total
    assert len(set(all_ids)) == expected_total
    assert sorted(all_ids) == [f"E{i:03d}" for i in range(1, expected_total + 1)]
