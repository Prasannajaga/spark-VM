"""Shared JSON state store helpers for rollouts and scheduler."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
import fcntl

from .config import resolve_home_dir
from .constants import METADATA_VERSION
from .utils import now_utc_iso


def _home_dir(home_dir: str | Path | None = None) -> Path:
    return resolve_home_dir(home_dir)


def _rollouts_dir(home_dir: str | Path | None = None) -> Path:
    return _home_dir(home_dir) / "rollouts"


def _scheduler_dir(home_dir: str | Path | None = None) -> Path:
    return _home_dir(home_dir) / "scheduler"


def _rollout_metadata_path(home_dir: str | Path | None = None) -> Path:
    return _rollouts_dir(home_dir) / "metadata.json"


def _rollout_dir(rollout_id: str, home_dir: str | Path | None = None) -> Path:
    return _rollouts_dir(home_dir) / rollout_id


def _rollout_json_path(rollout_id: str, home_dir: str | Path | None = None) -> Path:
    return _rollout_dir(rollout_id, home_dir) / "rollout.json"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, data: Any) -> None:
    _ensure_parent(path)
    tmp_path = path.parent / f".{path.name}.tmp"
    encoded = json.dumps(data, indent=2, sort_keys=True) + "\n"

    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(tmp_path, path)

    dir_fd: int | None = None
    try:
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        os.fsync(dir_fd)
    finally:
        if dir_fd is not None:
            os.close(dir_fd)


def _normalize_index_entry(rollout_id: str, entry: dict[str, Any], created_at: str) -> dict[str, Any]:
    def _as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    status = entry.get("status")
    if not isinstance(status, str) or not status.strip():
        status = "scheduled"

    return {
        "id": rollout_id,
        "status": status,
        "priority": _as_int(entry.get("priority"), 100),
        "retry_count": max(0, _as_int(entry.get("retry_count"), 0)),
        "max_retries": max(0, _as_int(entry.get("max_retries"), 3)),
        "created_at": entry.get("created_at") if isinstance(entry.get("created_at"), str) else created_at,
        "updated_at": entry.get("updated_at") if isinstance(entry.get("updated_at"), str) else created_at,
    }


def _migrate_rollouts_metadata_unlocked(home_dir: str | Path | None = None) -> dict[str, Any]:
    rollouts_dir = _rollouts_dir(home_dir)
    metadata_path = _rollout_metadata_path(home_dir)
    rollouts_dir.mkdir(parents=True, exist_ok=True)

    if not metadata_path.exists():
        payload = {"version": METADATA_VERSION, "rollouts": {}}
        atomic_write_json(metadata_path, payload)
        return payload

    raw = read_json(metadata_path)
    if not isinstance(raw, dict):
        payload = {"version": METADATA_VERSION, "rollouts": {}}
        atomic_write_json(metadata_path, payload)
        return payload

    version = raw.get("version", METADATA_VERSION)
    rollouts_raw = raw.get("rollouts", {})

    if isinstance(rollouts_raw, dict):
        normalized: dict[str, dict[str, Any]] = {}
        for rollout_id, entry in rollouts_raw.items():
            if not isinstance(rollout_id, str):
                continue
            if not isinstance(entry, dict):
                continue
            created_at = entry.get("created_at") if isinstance(entry.get("created_at"), str) else now_utc_iso()
            normalized[rollout_id] = _normalize_index_entry(rollout_id, entry, created_at)

        payload = {"version": int(version) if isinstance(version, int) else METADATA_VERSION, "rollouts": normalized}
        if payload != raw:
            atomic_write_json(metadata_path, payload)
        return payload

    if not isinstance(rollouts_raw, list):
        payload = {"version": METADATA_VERSION, "rollouts": {}}
        atomic_write_json(metadata_path, payload)
        return payload

    migrated_rollouts: dict[str, dict[str, Any]] = {}
    for item in rollouts_raw:
        if not isinstance(item, dict):
            continue
        rollout_id = item.get("id")
        if not isinstance(rollout_id, str) or not rollout_id:
            continue

        rollout_dir = _rollout_dir(rollout_id, home_dir)
        rollout_dir.mkdir(parents=True, exist_ok=True)

        full = dict(item)
        full["path"] = str(rollout_dir)
        atomic_write_json(_rollout_json_path(rollout_id, home_dir), full)

        created_at = full.get("created_at") if isinstance(full.get("created_at"), str) else now_utc_iso()
        migrated_rollouts[rollout_id] = _normalize_index_entry(rollout_id, full, created_at)

    payload = {"version": int(version) if isinstance(version, int) else METADATA_VERSION, "rollouts": migrated_rollouts}
    atomic_write_json(metadata_path, payload)
    return payload


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    _ensure_parent(lock_path)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def rollout_metadata_lock(home_dir: str | Path | None = None) -> Iterator[None]:
    lock_path = _rollouts_dir(home_dir) / "metadata.lock"
    with _file_lock(lock_path):
        yield


@contextmanager
def scheduler_lock(home_dir: str | Path | None = None) -> Iterator[None]:
    lock_path = _scheduler_dir(home_dir) / "scheduler.lock"
    with _file_lock(lock_path):
        yield


def load_rollouts_metadata(home_dir: str | Path | None = None) -> dict[str, Any]:
    return _migrate_rollouts_metadata_unlocked(home_dir)


def save_rollouts_metadata(data: dict[str, Any], home_dir: str | Path | None = None) -> None:
    metadata_path = _rollout_metadata_path(home_dir)
    payload = {
        "version": int(data.get("version", METADATA_VERSION)),
        "rollouts": data.get("rollouts", {}),
    }
    atomic_write_json(metadata_path, payload)


def get_rollout(rollout_id: str, home_dir: str | Path | None = None) -> dict[str, Any]:
    path = _rollout_json_path(rollout_id, home_dir)
    if not path.exists():
        raise FileNotFoundError(f"Missing rollout.json for {rollout_id}")
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid rollout.json for {rollout_id}")
    payload["path"] = str(_rollout_dir(rollout_id, home_dir))
    return payload


def save_rollout(rollout: dict[str, Any], home_dir: str | Path | None = None) -> None:
    rollout_id = rollout.get("id")
    if not isinstance(rollout_id, str) or not rollout_id:
        raise ValueError("rollout payload must include id")

    rollout_dir = _rollout_dir(rollout_id, home_dir)
    rollout_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(rollout)
    payload["path"] = str(rollout_dir)
    atomic_write_json(_rollout_json_path(rollout_id, home_dir), payload)


def update_rollout_index(rollout_id: str, patch: dict[str, Any], home_dir: str | Path | None = None) -> dict[str, Any]:
    with rollout_metadata_lock(home_dir):
        metadata = load_rollouts_metadata(home_dir)
        entries = metadata.setdefault("rollouts", {})
        if not isinstance(entries, dict):
            entries = {}
            metadata["rollouts"] = entries

        existing = entries.get(rollout_id)
        created_at = now_utc_iso()
        if isinstance(existing, dict) and isinstance(existing.get("created_at"), str):
            created_at = existing["created_at"]

        current = _normalize_index_entry(rollout_id, existing if isinstance(existing, dict) else {}, created_at)
        current.update(patch)
        current["id"] = rollout_id
        current.setdefault("created_at", created_at)
        current["updated_at"] = now_utc_iso()
        entries[rollout_id] = current

        save_rollouts_metadata(metadata, home_dir)
        return current


__all__ = [
    "read_json",
    "atomic_write_json",
    "rollout_metadata_lock",
    "scheduler_lock",
    "load_rollouts_metadata",
    "save_rollouts_metadata",
    "get_rollout",
    "save_rollout",
    "update_rollout_index",
]
