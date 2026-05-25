"""Shared state helpers, now backed by SQLite for metadata/state."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import resolve_home_dir
from .constants import METADATA_VERSION
from .repositories import RolloutRepository, RuntimeImageRepository
from .utils import now_utc_iso


def _home_dir(home_dir: str | Path | None = None) -> Path:
    return resolve_home_dir(home_dir)


def _rollouts_dir(home_dir: str | Path | None = None) -> Path:
    return _home_dir(home_dir) / "rollouts"


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
        "active_worker_id": entry.get("active_worker_id") if isinstance(entry.get("active_worker_id"), str) else None,
        "last_worker_id": entry.get("last_worker_id") if isinstance(entry.get("last_worker_id"), str) else None,
        "completed_at": entry.get("completed_at") if isinstance(entry.get("completed_at"), str) else None,
    }


def _row_to_rollout_payload(row: dict[str, Any], runtime_image_row: dict[str, Any] | None) -> dict[str, Any]:
    resolved = {}
    runtime_image_json = {}
    if isinstance(row.get("resolved_run_command_json"), str):
        try:
            parsed = json.loads(str(row["resolved_run_command_json"]))
            if isinstance(parsed, dict):
                resolved = parsed
        except Exception:
            pass
    if isinstance(row.get("runtime_image_json"), str):
        try:
            parsed = json.loads(str(row["runtime_image_json"]))
            if isinstance(parsed, dict):
                runtime_image_json = parsed
        except Exception:
            pass

    runtime_image = dict(runtime_image_json)
    if runtime_image_row is not None:
        runtime_image.setdefault("id", runtime_image_row.get("id"))
        runtime_image.setdefault("path", runtime_image_row.get("path"))
        runtime_image.setdefault("metadata_path", runtime_image_row.get("metadata_path"))

    payload = {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "runtime": str(row["runtime"]),
        "path": str(row.get("rollout_dir") or _rollout_dir(str(row["id"]))),
        "image_path": str(row["image_path"]),
        "deleteOnSuccess": bool(int(row.get("delete_on_success", 0))),
        "created_at": str(row["created_at"]),
        "dockerfile": str(row.get("dockerfile_path") or "Dockerfile"),
        "runtime_image": runtime_image,
        "resolved_run_command": resolved,
    }

    vm_config_json = row.get("vm_config_json")
    if isinstance(vm_config_json, str) and vm_config_json:
        try:
            vm_config = json.loads(vm_config_json)
            if isinstance(vm_config, dict):
                payload["vm_config"] = vm_config
        except Exception:
            pass

    return payload


def load_rollouts_metadata(home_dir: str | Path | None = None) -> dict[str, Any]:
    repo = RolloutRepository(home_dir)
    entries: dict[str, dict[str, Any]] = {}
    for row in repo.list_all():
        rollout_id = str(row["id"])
        created_at = str(row.get("created_at") or now_utc_iso())
        entries[rollout_id] = _normalize_index_entry(rollout_id, row, created_at)
    return {"version": METADATA_VERSION, "rollouts": entries}


def save_rollouts_metadata(data: dict[str, Any], home_dir: str | Path | None = None) -> None:
    repo = RolloutRepository(home_dir)
    rollouts = data.get("rollouts", {})
    if not isinstance(rollouts, dict):
        return
    allowed = {
        "status",
        "priority",
        "retry_count",
        "max_retries",
        "created_at",
        "updated_at",
        "active_worker_id",
        "last_worker_id",
        "scheduled_at",
        "started_at",
        "completed_at",
    }
    for rollout_id, entry in rollouts.items():
        if not isinstance(rollout_id, str) or not isinstance(entry, dict):
            continue
        patch = {k: v for k, v in entry.items() if k in allowed}
        if patch:
            repo.update(rollout_id, patch)


def get_rollout(rollout_id: str, home_dir: str | Path | None = None) -> dict[str, Any]:
    rollout_repo = RolloutRepository(home_dir)
    runtime_repo = RuntimeImageRepository(home_dir)
    row = rollout_repo.get(rollout_id)
    if row is None:
        raise FileNotFoundError(f"Missing rollout for {rollout_id}")
    runtime_row = runtime_repo.get_by_rollout(rollout_id)
    payload = _row_to_rollout_payload(row, runtime_row)
    payload["path"] = str(_rollout_dir(rollout_id, home_dir))
    return payload


def save_rollout(rollout: dict[str, Any], home_dir: str | Path | None = None) -> None:
    rollout_id = rollout.get("id")
    if not isinstance(rollout_id, str) or not rollout_id:
        raise ValueError("rollout payload must include id")

    rollout_dir = _rollout_dir(rollout_id, home_dir)
    rollout_dir.mkdir(parents=True, exist_ok=True)
    created_at = rollout.get("created_at") if isinstance(rollout.get("created_at"), str) else now_utc_iso()

    runtime_image = rollout.get("runtime_image") if isinstance(rollout.get("runtime_image"), dict) else {}
    resolved_run_command = (
        rollout.get("resolved_run_command") if isinstance(rollout.get("resolved_run_command"), dict) else {}
    )

    row = {
        "id": rollout_id,
        "name": str(rollout.get("name") or rollout_id),
        "runtime": str(rollout.get("runtime") or "Dockerfile"),
        "dockerfile_path": str(rollout.get("dockerfile") or "Dockerfile"),
        "rollout_dir": str(rollout_dir),
        "image_path": str(rollout.get("image_path") or ""),
        "delete_on_success": 1 if bool(rollout.get("deleteOnSuccess", False)) else 0,
        "resolved_run_command_json": json.dumps(resolved_run_command, sort_keys=True),
        "runtime_image_json": json.dumps(runtime_image, sort_keys=True),
        "status": str(rollout.get("status") or "scheduled"),
        "priority": int(rollout.get("priority") or 100),
        "retry_count": int(rollout.get("retry_count") or 0),
        "max_retries": int(rollout.get("max_retries") or 3),
        "scheduled_at": str(rollout.get("scheduled_at") or created_at),
        "started_at": rollout.get("started_at"),
        "completed_at": rollout.get("completed_at"),
        "active_worker_id": rollout.get("active_worker_id"),
        "last_worker_id": rollout.get("last_worker_id"),
        "created_at": created_at,
        "updated_at": str(rollout.get("updated_at") or created_at),
    }

    rollout_repo = RolloutRepository(home_dir)
    rollout_repo.upsert(row)

    image_id = runtime_image.get("id") if isinstance(runtime_image.get("id"), str) else f"image-{rollout_id}"
    image_path = runtime_image.get("path") if isinstance(runtime_image.get("path"), str) else row["image_path"]
    if image_path:
        RuntimeImageRepository(home_dir).create(
            {
                "id": image_id,
                "rollout_id": rollout_id,
                "path": image_path,
                "metadata_path": runtime_image.get("metadata_path")
                if isinstance(runtime_image.get("metadata_path"), str)
                else None,
                "created_at": created_at,
                "updated_at": row["updated_at"],
            }
        )

    export_payload = dict(rollout)
    export_payload["path"] = str(rollout_dir)
    atomic_write_json(_rollout_json_path(rollout_id, home_dir), export_payload)


def update_rollout_index(rollout_id: str, patch: dict[str, Any], home_dir: str | Path | None = None) -> dict[str, Any]:
    repo = RolloutRepository(home_dir)
    existing = repo.get(rollout_id)
    if existing is None:
        created_at = now_utc_iso()
        repo.upsert(
            {
                "id": rollout_id,
                "name": rollout_id,
                "runtime": "Dockerfile",
                "dockerfile_path": "Dockerfile",
                "rollout_dir": str(_rollout_dir(rollout_id, home_dir)),
                "image_path": "",
                "delete_on_success": 0,
                "resolved_run_command_json": "{}",
                "runtime_image_json": "{}",
                "status": "scheduled",
                "priority": 100,
                "retry_count": 0,
                "max_retries": 3,
                "scheduled_at": created_at,
                "started_at": None,
                "completed_at": None,
                "active_worker_id": None,
                "last_worker_id": None,
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
        existing = repo.get(rollout_id)

    created_at = str(existing.get("created_at") or now_utc_iso()) if isinstance(existing, dict) else now_utc_iso()
    current = _normalize_index_entry(rollout_id, existing if isinstance(existing, dict) else {}, created_at)
    current.update(patch)
    current["updated_at"] = now_utc_iso()
    repo.update(rollout_id, current)
    return current


__all__ = [
    "read_json",
    "atomic_write_json",
    "load_rollouts_metadata",
    "save_rollouts_metadata",
    "get_rollout",
    "save_rollout",
    "update_rollout_index",
]
