"""Repository layer over SparkVM SQLite state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from ..core.config import resolve_home_dir
from ..core.constants import DEFAULT_MACHINE_POLICY
from ..storage.db import connect_db
from ..storage.query_builder import QueryBuilder
from ..core.utils import now_utc_iso


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _json_loads(value: Any, *, fallback: Any) -> Any:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


class BaseRepository:
    def __init__(self, home_dir: str | Path | None = None) -> None:
        self.home_dir = resolve_home_dir(home_dir)


class RolloutRepository(BaseRepository):
    def create(self, rollout: dict[str, Any]) -> None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.insert("rollouts", rollout)
            conn.commit()

    def upsert(self, rollout: dict[str, Any]) -> None:
        cols = list(rollout.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{col}=excluded.{col}" for col in cols if col != "id")
        sql = f"INSERT INTO rollouts ({', '.join(cols)}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}"
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.execute(sql, tuple(rollout[col] for col in cols))
            conn.commit()

    def get(self, rollout_id: str) -> dict[str, Any] | None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return qb.from_table("rollouts").where(id=rollout_id).fetch_one()

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return qb.from_table("rollouts").where(name=name).order_by("created_at", "ASC").fetch_one()

    def list_all(self) -> list[dict[str, Any]]:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return qb.from_table("rollouts").order_by("created_at", "ASC").fetch_all()

    def list_by_status(self, statuses: Sequence[str]) -> list[dict[str, Any]]:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return (
                qb.from_table("rollouts")
                .where_in("status", list(statuses))
                .order_by("priority", "DESC")
                .order_by("created_at", "ASC")
                .fetch_all()
            )

    def update(self, rollout_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        if not patch:
            return self.get(rollout_id)
        data = dict(patch)
        data["updated_at"] = now_utc_iso()
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.update("rollouts", data, where={"id": rollout_id})
            conn.commit()
            return qb.from_table("rollouts").where(id=rollout_id).fetch_one()

    def delete(self, rollout_id: str) -> None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.delete("rollouts", where={"id": rollout_id})
            conn.commit()

    def set_status(self, rollout_id: str, status: str) -> dict[str, Any] | None:
        return self.update(rollout_id, {"status": status})

    def set_active_worker(self, rollout_id: str, worker_id: str) -> dict[str, Any] | None:
        return self.update(rollout_id, {"active_worker_id": worker_id, "last_worker_id": worker_id})

    def clear_active_worker(self, rollout_id: str) -> dict[str, Any] | None:
        return self.update(rollout_id, {"active_worker_id": None})

    def increment_retry_count(self, rollout_id: str) -> dict[str, Any] | None:
        now = now_utc_iso()
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.execute(
                "UPDATE rollouts SET retry_count = COALESCE(retry_count, 0) + 1, updated_at = ? WHERE id = ?",
                (now, rollout_id),
            )
            conn.commit()
            return qb.from_table("rollouts").where(id=rollout_id).fetch_one()


class RuntimeImageRepository(BaseRepository):
    def create(self, image: dict[str, Any]) -> None:
        cols = list(image.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{col}=excluded.{col}" for col in cols if col != "id")
        sql = f"INSERT INTO runtime_images ({', '.join(cols)}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}"
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.execute(sql, tuple(image[col] for col in cols))
            conn.commit()

    def get(self, image_id: str) -> dict[str, Any] | None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return qb.from_table("runtime_images").where(id=image_id).fetch_one()

    def get_by_rollout(self, rollout_id: str) -> dict[str, Any] | None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return qb.from_table("runtime_images").where(rollout_id=rollout_id).fetch_one()

    def delete_by_rollout(self, rollout_id: str) -> None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.delete("runtime_images", where={"rollout_id": rollout_id})
            conn.commit()


class WorkerRepository(BaseRepository):
    def create(self, worker: dict[str, Any]) -> None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.insert("workers", worker)
            conn.commit()

    def get(self, worker_id: str) -> dict[str, Any] | None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return qb.from_table("workers").where(id=worker_id).fetch_one()

    def update(self, worker_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        if not patch:
            return self.get(worker_id)
        data = dict(patch)
        data["updated_at"] = now_utc_iso()
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.update("workers", data, where={"id": worker_id})
            conn.commit()
            return qb.from_table("workers").where(id=worker_id).fetch_one()

    def delete(self, worker_id: str) -> None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.delete("workers", where={"id": worker_id})
            conn.commit()

    def list_by_status(self, statuses: Sequence[str]) -> list[dict[str, Any]]:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return (
                qb.from_table("workers")
                .where_in("status", list(statuses))
                .order_by("created_at", "ASC")
                .fetch_all()
            )

    def list_all(self) -> list[dict[str, Any]]:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return qb.from_table("workers").order_by("created_at", "ASC").fetch_all()

    def set_status(self, worker_id: str, status: str) -> dict[str, Any] | None:
        return self.update(worker_id, {"status": status})

    def attach_pid(self, worker_id: str, pid: int) -> dict[str, Any] | None:
        return self.update(worker_id, {"pid": int(pid)})

    def mark_passed(self, worker_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
        del result
        return self.update(
            worker_id,
            {
                "status": "passed",
                "completed_at": now_utc_iso(),
            },
        )

    def mark_failed(self, worker_id: str, failure: dict[str, Any]) -> dict[str, Any] | None:
        return self.update(
            worker_id,
            {
                "status": "failed",
                "completed_at": now_utc_iso(),
                "failure_json": _json_dumps(failure),
            },
        )

    def mark_timeout(self, worker_id: str, failure: dict[str, Any]) -> dict[str, Any] | None:
        return self.update(
            worker_id,
            {
                "status": "timeout",
                "completed_at": now_utc_iso(),
                "failure_json": _json_dumps(failure),
            },
        )


class ReservationRepository(BaseRepository):
    ACTIVE_STATUSES = {"reserved", "starting", "running"}

    def create(self, reservation: dict[str, Any]) -> None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.insert("reservations", reservation)
            conn.commit()

    def get(self, reservation_id: str) -> dict[str, Any] | None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return qb.from_table("reservations").where(id=reservation_id).fetch_one()

    def active(self) -> list[dict[str, Any]]:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return (
                qb.from_table("reservations")
                .where_in("status", sorted(self.ACTIVE_STATUSES))
                .order_by("created_at", "ASC")
                .fetch_all()
            )

    def list_all(self) -> list[dict[str, Any]]:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            return qb.from_table("reservations").order_by("created_at", "ASC").fetch_all()

    def update(self, reservation_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        if not patch:
            return self.get(reservation_id)
        data = dict(patch)
        data["updated_at"] = now_utc_iso()
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.update("reservations", data, where={"id": reservation_id})
            conn.commit()
            return qb.from_table("reservations").where(id=reservation_id).fetch_one()

    def attach_pid(self, reservation_id: str, pid: int) -> dict[str, Any] | None:
        return self.update(reservation_id, {"pid": int(pid)})

    def release(self, reservation_id: str) -> dict[str, Any] | None:
        return self.update(reservation_id, {"status": "released"})

    def mark_lost(self, reservation_id: str) -> dict[str, Any] | None:
        return self.update(reservation_id, {"status": "lost"})


class MachinePolicyRepository(BaseRepository):
    def get(self) -> dict[str, Any] | None:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            row = qb.from_table("machine_policy").where(id=1).fetch_one()
        if row is None:
            return None
        payload = dict(row)
        payload.pop("id", None)
        payload.pop("created_at", None)
        payload.pop("updated_at", None)
        return payload

    def set(self, policy: dict[str, Any]) -> dict[str, Any]:
        now = now_utc_iso()
        payload = dict(DEFAULT_MACHINE_POLICY)
        payload.update(policy)
        columns = [
            "host_reserved_memory",
            "host_reserved_memory_bytes",
            "host_reserved_disk",
            "host_reserved_disk_bytes",
            "max_memory_percent",
            "max_disk_percent",
            "max_concurrent_vms",
            "vm_memory_overhead",
            "vm_memory_overhead_bytes",
            "vm_disk_overhead",
            "vm_disk_overhead_bytes",
            "poll_interval",
            "cooldown_after_vm",
        ]
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.execute(
                """
                INSERT INTO machine_policy(
                    id,
                    host_reserved_memory,
                    host_reserved_memory_bytes,
                    host_reserved_disk,
                    host_reserved_disk_bytes,
                    max_memory_percent,
                    max_disk_percent,
                    max_concurrent_vms,
                    vm_memory_overhead,
                    vm_memory_overhead_bytes,
                    vm_disk_overhead,
                    vm_disk_overhead_bytes,
                    poll_interval,
                    cooldown_after_vm,
                    created_at,
                    updated_at
                )
                VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    host_reserved_memory=excluded.host_reserved_memory,
                    host_reserved_memory_bytes=excluded.host_reserved_memory_bytes,
                    host_reserved_disk=excluded.host_reserved_disk,
                    host_reserved_disk_bytes=excluded.host_reserved_disk_bytes,
                    max_memory_percent=excluded.max_memory_percent,
                    max_disk_percent=excluded.max_disk_percent,
                    max_concurrent_vms=excluded.max_concurrent_vms,
                    vm_memory_overhead=excluded.vm_memory_overhead,
                    vm_memory_overhead_bytes=excluded.vm_memory_overhead_bytes,
                    vm_disk_overhead=excluded.vm_disk_overhead,
                    vm_disk_overhead_bytes=excluded.vm_disk_overhead_bytes,
                    poll_interval=excluded.poll_interval,
                    cooldown_after_vm=excluded.cooldown_after_vm,
                    updated_at=excluded.updated_at
                """,
                tuple(payload[col] for col in columns) + (now, now),
            )
            conn.commit()
        return payload

    def ensure_default(self) -> dict[str, Any]:
        existing = self.get()
        if isinstance(existing, dict) and existing:
            return existing
        return self.set(dict(DEFAULT_MACHINE_POLICY))


class EventRepository(BaseRepository):
    def add(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        message: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        created_at = now_utc_iso()
        payload = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "message": message,
            "data_json": _json_dumps(data) if data is not None else None,
            "created_at": created_at,
        }
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.insert("events", payload)
            conn.commit()

    def list_for_entity(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            rows = (
                qb.from_table("events")
                .where(entity_type=entity_type, entity_id=entity_id)
                .order_by("created_at", "DESC")
                .fetch_all()
            )
        for row in rows:
            row["data"] = _json_loads(row.get("data_json"), fallback=None)
        return rows


__all__ = [
    "RolloutRepository",
    "RuntimeImageRepository",
    "WorkerRepository",
    "ReservationRepository",
    "MachinePolicyRepository",
    "EventRepository",
    "DEFAULT_MACHINE_POLICY",
]
