"""Scheduler reservation state."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import resolve_home_dir
from .machine_config import parse_size_to_bytes
from .state_store import atomic_write_json, read_json
from .utils import now_utc_iso

ACTIVE_STATUSES = {"reserved", "starting", "running"}


def _scheduler_dir(home_dir: str | Path | None = None) -> Path:
    return resolve_home_dir(home_dir) / "scheduler"


def _reservations_path(home_dir: str | Path | None = None) -> Path:
    return _scheduler_dir(home_dir) / "reservations.json"


def _default_payload() -> dict[str, Any]:
    return {"version": 1, "reservations": {}}


def load_reservations(home_dir: str | Path | None = None) -> dict[str, Any]:
    path = _reservations_path(home_dir)
    if not path.exists():
        payload = _default_payload()
        atomic_write_json(path, payload)
        return payload

    raw = read_json(path)
    if not isinstance(raw, dict):
        payload = _default_payload()
        atomic_write_json(path, payload)
        return payload

    reservations = raw.get("reservations")
    if not isinstance(reservations, dict):
        raw["reservations"] = {}
    raw.setdefault("version", 1)
    return raw


def save_reservations(data: dict[str, Any], home_dir: str | Path | None = None) -> None:
    payload = {
        "version": int(data.get("version", 1)),
        "reservations": data.get("reservations", {}),
    }
    atomic_write_json(_reservations_path(home_dir), payload)


def active_reservations(home_dir: str | Path | None = None) -> list[dict[str, Any]]:
    payload = load_reservations(home_dir)
    items = payload.get("reservations", {})
    if not isinstance(items, dict):
        return []
    return [entry for entry in items.values() if isinstance(entry, dict) and entry.get("status") in ACTIVE_STATUSES]


def reserve(
    rollout_id: str,
    worker_id: str,
    vm_config: dict[str, Any],
    *,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    payload = load_reservations(home_dir)
    reservations = payload.setdefault("reservations", {})
    if not isinstance(reservations, dict):
        reservations = {}
        payload["reservations"] = reservations

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
    reservations[reservation_id] = entry
    save_reservations(payload, home_dir)
    return entry


def _update_reservation(
    reservation_id: str,
    patch: dict[str, Any],
    *,
    home_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    payload = load_reservations(home_dir)
    reservations = payload.get("reservations", {})
    if not isinstance(reservations, dict):
        return None
    current = reservations.get(reservation_id)
    if not isinstance(current, dict):
        return None
    current.update(patch)
    current["updated_at"] = now_utc_iso()
    reservations[reservation_id] = current
    save_reservations(payload, home_dir)
    return current


def attach_pid(reservation_id: str, pid: int, *, home_dir: str | Path | None = None) -> dict[str, Any] | None:
    return _update_reservation(reservation_id, {"pid": int(pid)}, home_dir=home_dir)


def release(reservation_id: str, *, home_dir: str | Path | None = None) -> dict[str, Any] | None:
    return _update_reservation(reservation_id, {"status": "released"}, home_dir=home_dir)


def mark_lost(reservation_id: str, *, home_dir: str | Path | None = None) -> dict[str, Any] | None:
    return _update_reservation(reservation_id, {"status": "lost"}, home_dir=home_dir)


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
