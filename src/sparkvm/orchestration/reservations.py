"""Scheduler reservation state backed by SQLite."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from ..core.config import resolve_home_dir
from ..machine.machine_config import parse_size_to_bytes
from ..storage.repositories import ReservationRepository
from ..core.utils import now_utc_iso

ACTIVE_STATUSES = {"reserved", "starting", "running"}


def _repo(home_dir: str | Path | None = None) -> ReservationRepository:
    return ReservationRepository(resolve_home_dir(home_dir))


def _default_payload() -> dict[str, Any]:
    return {"version": 1, "reservations": {}}


def load_reservations(home_dir: str | Path | None = None) -> dict[str, Any]:
    items = _repo(home_dir).list_all()
    payload = _default_payload()
    payload["reservations"] = {str(item["id"]): item for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)}
    return payload


def save_reservations(data: dict[str, Any], home_dir: str | Path | None = None) -> None:
    reservations = data.get("reservations", {})
    if not isinstance(reservations, dict):
        return
    repo = _repo(home_dir)
    for reservation_id, entry in reservations.items():
        if not isinstance(reservation_id, str) or not isinstance(entry, dict):
            continue
        current = repo.get(reservation_id)
        if current is None:
            continue
        patch = dict(entry)
        patch.pop("id", None)
        repo.update(reservation_id, patch)


def active_reservations(home_dir: str | Path | None = None) -> list[dict[str, Any]]:
    return _repo(home_dir).active()


def reserve(
    rollout_id: str,
    worker_id: str,
    vm_config: dict[str, Any],
    *,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    reservation_id = f"res-{uuid4().hex[:12]}"
    now = now_utc_iso()
    memory = str(vm_config.get("memory", "2G"))
    disk = str(vm_config.get("disk", "4G"))
    entry = {
        "id": reservation_id,
        "worker_id": worker_id,
        "rollout_id": rollout_id,
        "pid": None,
        "vcpu": int(vm_config.get("vcpu", 2)),
        "memory": memory,
        "memory_bytes": parse_size_to_bytes(memory),
        "disk": disk,
        "disk_bytes": parse_size_to_bytes(disk),
        "status": "reserved",
        "created_at": now,
        "updated_at": now,
        "last_heartbeat_at": None,
    }
    _repo(home_dir).create(entry)
    return entry


def _update_reservation(
    reservation_id: str,
    patch: dict[str, Any],
    *,
    home_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    return _repo(home_dir).update(reservation_id, patch)


def attach_pid(reservation_id: str, pid: int, *, home_dir: str | Path | None = None) -> dict[str, Any] | None:
    return _update_reservation(reservation_id, {"pid": int(pid)}, home_dir=home_dir)


def release(reservation_id: str, *, home_dir: str | Path | None = None) -> dict[str, Any] | None:
    return _repo(home_dir).release(reservation_id)


def mark_lost(reservation_id: str, *, home_dir: str | Path | None = None) -> dict[str, Any] | None:
    return _repo(home_dir).mark_lost(reservation_id)


__all__ = [
    "ACTIVE_STATUSES",
    "load_reservations",
    "save_reservations",
    "active_reservations",
    "reserve",
    "attach_pid",
    "release",
    "mark_lost",
]
