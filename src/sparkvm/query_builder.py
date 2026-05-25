"""Reusable parameterized SQL wrapper for SQLite.

The wrapper is intentionally small:
- single QueryBuilder object per connection
- optional table-scoped helpers via ``table(name)``
- dict rows with sqlite3.Row
- parameterized values only
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from typing import Any, Sequence


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class OrderBy:
    column: str
    direction: str = "ASC"


class TableQuery:
    """Table-scoped query facade built from a QueryBuilder instance."""

    def __init__(self, qb: QueryBuilder, table: str) -> None:
        self._qb = qb
        self._table = table

    def insert(self, data: dict[str, Any]) -> None:
        self._qb.insert(self._table, data)

    def update(self, values: dict[str, Any], *, where: dict[str, Any]) -> int:
        return self._qb.update(self._table, values, where=where)

    def delete(self, *, where: dict[str, Any]) -> int:
        return self._qb.delete(self._table, where=where)

    def select_one(
        self,
        *,
        columns: Sequence[str] | None = None,
        where: dict[str, Any] | None = None,
        where_in: dict[str, Sequence[Any]] | None = None,
        order_by: Sequence[tuple[str, str] | OrderBy] | None = None,
    ) -> dict[str, Any] | None:
        return self._qb.select_one(
            self._table,
            columns=columns,
            where=where,
            where_in=where_in,
            order_by=order_by,
        )

    def select_many(
        self,
        *,
        columns: Sequence[str] | None = None,
        where: dict[str, Any] | None = None,
        where_in: dict[str, Sequence[Any]] | None = None,
        order_by: Sequence[tuple[str, str] | OrderBy] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._qb.select_many(
            self._table,
            columns=columns,
            where=where,
            where_in=where_in,
            order_by=order_by,
            limit=limit,
        )


class QueryBuilder:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def table(self, table: str) -> TableQuery:
        self._validate_identifier(table)
        return TableQuery(self, table)

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params or ()))

    def fetch_one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        row = self.execute(sql, params).fetchone()
        if row is None:
            return None
        return dict(row)

    def fetch_all(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        return [dict(row) for row in self.execute(sql, params).fetchall()]

    def insert(self, table: str, data: dict[str, Any]) -> None:
        self._validate_identifier(table)
        if not data:
            raise ValueError("insert data cannot be empty")
        columns = list(data.keys())
        self._validate_identifier_list(columns)
        placeholders = ", ".join("?" for _ in columns)
        cols = ", ".join(columns)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        self.execute(sql, tuple(data[col] for col in columns))

    def update(self, table: str, values: dict[str, Any], *, where: dict[str, Any]) -> int:
        self._validate_identifier(table)
        if not values:
            return 0
        self._validate_identifier_list(values.keys())
        set_sql = ", ".join(f"{col} = ?" for col in values.keys())
        where_sql, where_params = self._build_where(where=where)
        sql = f"UPDATE {table} SET {set_sql}{where_sql}"
        cur = self.execute(sql, tuple(values.values()) + where_params)
        return int(cur.rowcount)

    def delete(self, table: str, *, where: dict[str, Any]) -> int:
        self._validate_identifier(table)
        where_sql, where_params = self._build_where(where=where)
        sql = f"DELETE FROM {table}{where_sql}"
        cur = self.execute(sql, where_params)
        return int(cur.rowcount)

    def select_one(
        self,
        table: str,
        *,
        columns: Sequence[str] | None = None,
        where: dict[str, Any] | None = None,
        where_in: dict[str, Sequence[Any]] | None = None,
        order_by: Sequence[tuple[str, str] | OrderBy] | None = None,
    ) -> dict[str, Any] | None:
        rows = self.select_many(
            table,
            columns=columns,
            where=where,
            where_in=where_in,
            order_by=order_by,
            limit=1,
        )
        if not rows:
            return None
        return rows[0]

    def select_many(
        self,
        table: str,
        *,
        columns: Sequence[str] | None = None,
        where: dict[str, Any] | None = None,
        where_in: dict[str, Sequence[Any]] | None = None,
        order_by: Sequence[tuple[str, str] | OrderBy] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._validate_identifier(table)
        select_cols = "*"
        if columns:
            self._validate_identifier_list(columns)
            select_cols = ", ".join(columns)

        sql = f"SELECT {select_cols} FROM {table}"
        where_clause, params = self._build_where(where=where, where_in=where_in)
        sql += where_clause

        if order_by:
            order_parts: list[str] = []
            for item in order_by:
                if isinstance(item, OrderBy):
                    col, direction = item.column, item.direction
                else:
                    col, direction = item
                self._validate_identifier(col)
                direction_up = str(direction).upper()
                if direction_up not in {"ASC", "DESC"}:
                    raise ValueError(f"Unsupported order direction: {direction}")
                order_parts.append(f"{col} {direction_up}")
            sql += " ORDER BY " + ", ".join(order_parts)

        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be >= 0")
            sql += " LIMIT ?"
            params = params + (int(limit),)

        return self.fetch_all(sql, params)

    def _build_where(
        self,
        *,
        where: dict[str, Any] | None = None,
        where_in: dict[str, Sequence[Any]] | None = None,
    ) -> tuple[str, tuple[Any, ...]]:
        clauses: list[str] = []
        params: list[Any] = []

        if where:
            self._validate_identifier_list(where.keys())
            for key, value in where.items():
                if value is None:
                    clauses.append(f"{key} IS NULL")
                else:
                    clauses.append(f"{key} = ?")
                    params.append(value)

        if where_in:
            self._validate_identifier_list(where_in.keys())
            for key, values in where_in.items():
                normalized = list(values)
                if not normalized:
                    clauses.append("1=0")
                    continue
                placeholders = ", ".join("?" for _ in normalized)
                clauses.append(f"{key} IN ({placeholders})")
                params.extend(normalized)

        if not clauses:
            return "", tuple()

        return " WHERE " + " AND ".join(clauses), tuple(params)

    @staticmethod
    def _validate_identifier(name: str) -> None:
        if not _IDENTIFIER_RE.match(name):
            raise ValueError(f"Unsafe SQL identifier: {name!r}")

    @classmethod
    def _validate_identifier_list(cls, names: Sequence[str] | Any) -> None:
        for name in names:
            cls._validate_identifier(str(name))


__all__ = ["QueryBuilder", "TableQuery", "OrderBy"]
