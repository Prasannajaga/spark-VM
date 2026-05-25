from __future__ import annotations

import functools
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@functools.lru_cache(maxsize=256)
def _validate_identifier(name: str) -> None: 
    if not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")


def _validate_identifier_list(names: Sequence[str]) -> None:
    for name in names:
        _validate_identifier(str(name))


@dataclass(frozen=True)
class OrderBy:
    column: str
    direction: str = "ASC"


@dataclass
class SelectQuery: 
    _conn: sqlite3.Connection
    _table: str
    _columns: list[str] = field(default_factory=list)
    _where: dict[str, Any] = field(default_factory=dict)
    _where_in: dict[str, list[Any]] = field(default_factory=dict)
    _order_by: list[OrderBy] = field(default_factory=list)
    _limit: int | None = None

    def columns(self, *cols: str) -> SelectQuery:
        _validate_identifier_list(cols)
        self._columns.extend(cols)
        return self

    def where(self, **conditions: Any) -> SelectQuery:
        _validate_identifier_list(conditions.keys())
        self._where.update(conditions)
        return self

    def where_in(self, column: str, values: Sequence[Any]) -> SelectQuery:
        _validate_identifier(column)
        self._where_in[column] = list(values)
        return self

    def where_not_null(self, column: str) -> SelectQuery:
        _validate_identifier(column)
        self._where[column] = _NOT_NULL
        return self

    def order_by(self, column: str, direction: str = "ASC") -> SelectQuery:
        _validate_identifier(column)
        direction_up = direction.upper()
        if direction_up not in {"ASC", "DESC"}:
            raise ValueError(f"Unsupported order direction: {direction!r}")
        self._order_by.append(OrderBy(column=column, direction=direction_up))
        return self

    def limit(self, n: int) -> SelectQuery:
        if n < 0:
            raise ValueError("limit must be >= 0")
        self._limit = n
        return self

    def to_sql(self) -> tuple[str, tuple[Any, ...]]:
        """Return the (sql, params) tuple without executing. Useful for logging and testing."""
        select_cols = ", ".join(self._columns) if self._columns else "*"
        sql = f"SELECT {select_cols} FROM {self._table}"

        where_clauses: list[str] = []
        params: list[Any] = []

        for key, value in self._where.items():
            if value is _NOT_NULL:
                where_clauses.append(f"{key} IS NOT NULL")
            elif value is None:
                where_clauses.append(f"{key} IS NULL")
            else:
                where_clauses.append(f"{key} = ?")
                params.append(value)

        for key, values in self._where_in.items():
            if not values:
                where_clauses.append("1=0")
            else:
                placeholders = ", ".join(["?"] * len(values))
                where_clauses.append(f"{key} IN ({placeholders})")
                params.extend(values)

        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        if self._order_by:
            order_parts = [f"{ob.column} {ob.direction}" for ob in self._order_by]
            sql += " ORDER BY " + ", ".join(order_parts)

        if self._limit is not None:
            sql += " LIMIT ?"
            params.append(self._limit)

        return sql, tuple(params)

    def fetch_one(self) -> dict[str, Any] | None:
        """Execute with LIMIT 1 and return a single row dict, or None."""
        original_limit = self._limit
        self._limit = 1
        sql, params = self.to_sql()
        self._limit = original_limit
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetch_all(self) -> list[dict[str, Any]]:
        """Execute and return all matching rows as a list of dicts."""
        sql, params = self.to_sql()
        return [dict(row) for row in self._conn.execute(sql, params).fetchall()]


class _NotNull:
    """Sentinel singleton for IS NOT NULL WHERE clauses."""
    _instance: "_NotNull | None" = None

    def __new__(cls) -> "_NotNull":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance


_NOT_NULL = _NotNull()


class QueryBuilder: 

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def from_table(self, table: str) -> SelectQuery:
        _validate_identifier(table)
        return SelectQuery(_conn=self.conn, _table=table)

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params or ()))

    def fetch_one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        row = self.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetch_all(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        return [dict(row) for row in self.execute(sql, params).fetchall()]

    def insert(self, table: str, data: dict[str, Any]) -> None:
        _validate_identifier(table)
        if not data:
            raise ValueError("insert data cannot be empty")
        columns = list(data.keys())
        _validate_identifier_list(columns)
        placeholders = ", ".join(["?"] * len(columns))
        cols = ", ".join(columns)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        self.conn.execute(sql, tuple(data[col] for col in columns))

    def update(self, table: str, values: dict[str, Any], *, where: dict[str, Any]) -> int:
        _validate_identifier(table)
        if not values:
            return 0
        _validate_identifier_list(values.keys())
        set_sql = ", ".join(f"{col} = ?" for col in values.keys())
        where_clauses, where_params = self._build_where_dict(where)
        sql = f"UPDATE {table} SET {set_sql}"
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        cur = self.conn.execute(sql, tuple(values.values()) + where_params)
        return int(cur.rowcount)

    def delete(self, table: str, *, where: dict[str, Any]) -> int:
        _validate_identifier(table)
        where_clauses, where_params = self._build_where_dict(where)
        sql = f"DELETE FROM {table}"
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        cur = self.conn.execute(sql, where_params)
        return int(cur.rowcount)

    @staticmethod
    def _build_where_dict(where: dict[str, Any]) -> tuple[list[str], tuple[Any, ...]]:
        _validate_identifier_list(where.keys())
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in where.items():
            if value is None:
                clauses.append(f"{key} IS NULL")
            else:
                clauses.append(f"{key} = ?")
                params.append(value)
        return clauses, tuple(params)


__all__ = ["QueryBuilder", "SelectQuery", "OrderBy"]
