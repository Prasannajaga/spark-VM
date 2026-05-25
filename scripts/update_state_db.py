#!/usr/bin/env python3
"""Reset SparkVM state.db from schema.sql."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_db_path() -> Path:
    return Path.home() / ".sparkvm" / "state.db"


def _schema_path() -> Path:
    return _repo_root() / "src" / "sparkvm" / "storage" / "schema.sql"


def _ensure_rollouts_columns(conn: sqlite3.Connection) -> None:
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(rollouts)").fetchall()}
    if "vm_config_json" not in cols:
        conn.execute("ALTER TABLE rollouts ADD COLUMN vm_config_json TEXT")


def _create_fresh_db(db_path: Path, schema_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    wal_path = db_path.with_name(db_path.name + "-wal")
    shm_path = db_path.with_name(db_path.name + "-shm")
    if wal_path.exists():
        wal_path.unlink()
    if shm_path.exists():
        shm_path.unlink()

    schema_sql = schema_path.read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.executescript(schema_sql)
        _ensure_rollouts_columns(conn)
        conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset SparkVM state.db using src/sparkvm/storage/schema.sql")
    parser.add_argument(
        "--db",
        default=str(_default_db_path()),
        help="Path to SQLite database file (default: ~/.sparkvm/state.db)",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create <db>.bak before reset",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    schema_path = _schema_path()
    if not schema_path.is_file():
        raise SystemExit(f"Schema file not found: {schema_path}")

    if args.backup and db_path.exists():
        backup_path = db_path.with_suffix(db_path.suffix + ".bak")
        shutil.copy2(db_path, backup_path)
        print(f"Backup written: {backup_path}")

    _create_fresh_db(db_path, schema_path)
    print(f"Schema applied successfully: {schema_path}")
    print(f"Database reset: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
