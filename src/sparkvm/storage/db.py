"""SQLite helpers for SparkVM state."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..core.config import resolve_home_dir


def state_db_path(home_dir: str | Path | None = None) -> Path:
    return resolve_home_dir(home_dir) / "state.db"


def _schema_path() -> Path:
    return Path(__file__).with_name("schema.sql")


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA synchronous = NORMAL;")


def _ensure_rollouts_columns(conn: sqlite3.Connection) -> None:
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(rollouts)").fetchall()}
    if "vm_config_json" not in cols:
        conn.execute("ALTER TABLE rollouts ADD COLUMN vm_config_json TEXT")


def _ensure_workers_columns(conn: sqlite3.Connection) -> None:
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
    if "timeout_seconds" not in cols:
        conn.execute("ALTER TABLE workers ADD COLUMN timeout_seconds REAL")
        if "timeout" in cols:
            conn.execute("UPDATE workers SET timeout_seconds = timeout WHERE timeout_seconds IS NULL")
        conn.execute("UPDATE workers SET timeout_seconds = 60.0 WHERE timeout_seconds IS NULL")
    if "failure_json" not in cols:
        conn.execute("ALTER TABLE workers ADD COLUMN failure_json TEXT DEFAULT '{}'")


def init_db(home_dir: str | Path | None = None) -> Path:
    db_path = state_db_path(home_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = _schema_path().read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        _apply_pragmas(conn)
        conn.executescript(schema)
        _ensure_rollouts_columns(conn)
        _ensure_workers_columns(conn)
        conn.commit()
    return db_path


@contextmanager
def connect_db(home_dir: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    db_path = init_db(home_dir)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    try:
        conn.execute("BEGIN")
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def execute(conn: sqlite3.Connection, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
    return conn.execute(sql, tuple(params or ()))


def fetch_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
    row = execute(conn, sql, params).fetchone()
    if row is None:
        return None
    return dict(row)


def fetch_all(conn: sqlite3.Connection, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    rows = execute(conn, sql, params).fetchall()
    return [dict(row) for row in rows]


__all__ = [
    "state_db_path",
    "init_db",
    "connect_db",
    "transaction",
    "execute",
    "fetch_one",
    "fetch_all",
]
