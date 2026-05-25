"""Migration helpers for moving JSON state to SQLite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import resolve_home_dir
from .repositories import EventRepository, RolloutRepository, RuntimeImageRepository
from .utils import now_utc_iso


def _metadata_path(home_dir: str | Path | None = None) -> Path:
    return resolve_home_dir(home_dir) / "rollouts" / "metadata.json"


def _as_rollout_entries(payload: dict[str, Any], *, home_dir: str | Path | None = None) -> list[dict[str, Any]]:
    rollouts = payload.get("rollouts", {})
    if isinstance(rollouts, list):
        return [item for item in rollouts if isinstance(item, dict)]
    if isinstance(rollouts, dict):
        entries: list[dict[str, Any]] = []
        for rollout_id, entry in rollouts.items():
            if not isinstance(entry, dict):
                continue
            merged = dict(entry)
            if isinstance(rollout_id, str) and rollout_id and "id" not in merged:
                merged["id"] = rollout_id
            if isinstance(rollout_id, str) and rollout_id:
                rollout_json_path = resolve_home_dir(home_dir) / "rollouts" / rollout_id / "rollout.json"
                if rollout_json_path.exists():
                    try:
                        full_payload = json.loads(rollout_json_path.read_text(encoding="utf-8"))
                        if isinstance(full_payload, dict):
                            full_payload.update(merged)
                            merged = full_payload
                    except Exception:
                        pass
            entries.append(merged)
        return entries
    return []


def migrate_json_rollouts_to_sqlite(home_dir: str | Path | None = None) -> int:
    metadata_path = _metadata_path(home_dir)
    if not metadata_path.exists():
        return 0

    rollout_repo = RolloutRepository(home_dir=home_dir)
    runtime_repo = RuntimeImageRepository(home_dir=home_dir)
    event_repo = EventRepository(home_dir=home_dir)

    if rollout_repo.list_all():
        return 0

    try:
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(raw, dict):
        return 0

    entries = _as_rollout_entries(raw, home_dir=home_dir)
    migrated = 0

    for entry in entries:
        rollout_id = entry.get("id")
        if not isinstance(rollout_id, str) or not rollout_id:
            continue
        created_at = entry.get("created_at") if isinstance(entry.get("created_at"), str) else now_utc_iso()
        updated_at = entry.get("updated_at") if isinstance(entry.get("updated_at"), str) else created_at

        runtime = entry.get("runtime") if isinstance(entry.get("runtime"), str) else "Dockerfile"
        dockerfile_path = entry.get("dockerfile") if isinstance(entry.get("dockerfile"), str) else "Dockerfile"
        rollout_dir = entry.get("path") if isinstance(entry.get("path"), str) else str(resolve_home_dir(home_dir) / "rollouts" / rollout_id)
        image_path = entry.get("image_path") if isinstance(entry.get("image_path"), str) else ""
        delete_on_success = 1 if bool(entry.get("deleteOnSuccess", False)) else 0
        resolved_run_command = entry.get("resolved_run_command") if isinstance(entry.get("resolved_run_command"), dict) else {}
        runtime_image = entry.get("runtime_image") if isinstance(entry.get("runtime_image"), dict) else {}

        rollout_row = {
            "id": rollout_id,
            "name": str(entry.get("name") or rollout_id),
            "runtime": runtime,
            "dockerfile_path": dockerfile_path,
            "rollout_dir": rollout_dir,
            "image_path": image_path,
            "delete_on_success": delete_on_success,
            "resolved_run_command_json": json.dumps(resolved_run_command, sort_keys=True),
            "runtime_image_json": json.dumps(runtime_image, sort_keys=True),
            "status": str(entry.get("status") or "scheduled"),
            "priority": int(entry.get("priority") or 100),
            "retry_count": int(entry.get("retry_count") or 0),
            "max_retries": int(entry.get("max_retries") or 3),
            "scheduled_at": created_at,
            "started_at": None,
            "completed_at": None,
            "active_worker_id": None,
            "last_worker_id": None,
            "created_at": created_at,
            "updated_at": updated_at,
        }

        rollout_repo.upsert(rollout_row)

        runtime_image_id = runtime_image.get("id") if isinstance(runtime_image.get("id"), str) else f"image-{rollout_id}"
        runtime_image_path = runtime_image.get("path") if isinstance(runtime_image.get("path"), str) else image_path
        runtime_metadata_path = runtime_image.get("metadata_path") if isinstance(runtime_image.get("metadata_path"), str) else None

        if isinstance(runtime_image_path, str) and runtime_image_path:
            runtime_repo.create(
                {
                    "id": runtime_image_id,
                    "rollout_id": rollout_id,
                    "path": runtime_image_path,
                    "metadata_path": runtime_metadata_path,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )

        migrated += 1

    if migrated > 0:
        event_repo.add(
            "system",
            "migration",
            "json_rollouts_to_sqlite",
            message="Migrated rollout metadata.json records into SQLite",
            data={"count": migrated, "path": str(metadata_path)},
        )
    return migrated


__all__ = ["migrate_json_rollouts_to_sqlite"]
