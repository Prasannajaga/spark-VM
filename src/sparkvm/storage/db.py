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


def init_db(home_dir: str | Path | None = None) -> Path:
    db_path = state_db_path(home_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema = _schema_path().read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        _apply_pragmas(conn)
        conn.executescript(schema)
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
