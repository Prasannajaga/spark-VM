"""Cleanup helpers for rollouts and preserved worker directories."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sparkvm.core.config import DEFAULT_MEMORY, DEFAULT_RUNTIME, DEFAULT_TIMEOUT_SEC, DEFAULT_VCPU, SparkVMConfig, build_config
from sparkvm.core.constants import KERNEL_FILENAME
from sparkvm.storage.db import connect_db
from sparkvm.core.errors import SparkVMError
from sparkvm.core.logger import configure_logging, log_event
from sparkvm.core.fsops import (
    ensure_dir,
    list_dirs_with_prefix,
    remove_file,
    remove_tree,
)
from sparkvm.core.utils import (
    unmount_under,
)


def rollouts_dir(config: SparkVMConfig) -> Path:
    return config.home_dir / "rollouts"


def workers_dir(config: SparkVMConfig) -> Path:
    return config.workers_dir


def clear_dir_contents(path: Path) -> None:
    ensure_dir(path, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir():
            remove_tree(child, ignore_errors=False)
        else:
            remove_file(child, missing_ok=True)


def clear_dir_contents_except(path: Path, *, keep_files: set[str]) -> None:
    ensure_dir(path, exist_ok=True)
    for child in path.iterdir():
        if child.is_file() and child.name in keep_files:
            continue
        if child.is_dir():
            remove_tree(child, ignore_errors=False)
        else:
            remove_file(child, missing_ok=True)


def _safe_unlink(path_value: object) -> None:
    if isinstance(path_value, str) and path_value.strip():
        remove_file(Path(path_value), missing_ok=True)


def _delete_rollout_images_from_db(config: SparkVMConfig) -> None:
    with connect_db(config.home_dir) as conn:
        rollout_rows = conn.execute("SELECT image_path, runtime_image_json FROM rollouts").fetchall()
        runtime_rows = conn.execute("SELECT path, metadata_path FROM runtime_images").fetchall()

    for row in rollout_rows:
        image_path = row["image_path"] if isinstance(row, dict) else row[0]
        runtime_image_json = row["runtime_image_json"] if isinstance(row, dict) else row[1]
        _safe_unlink(image_path)
        if isinstance(runtime_image_json, str) and runtime_image_json.strip():
            try:
                payload = json.loads(runtime_image_json)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                _safe_unlink(payload.get("path"))
                _safe_unlink(payload.get("metadata_path"))

    for row in runtime_rows:
        runtime_path = row["path"] if isinstance(row, dict) else row[0]
        metadata_path = row["metadata_path"] if isinstance(row, dict) else row[1]
        _safe_unlink(runtime_path)
        _safe_unlink(metadata_path)


def cleanup_rollouts(config: SparkVMConfig, *, force: bool = False, dry_run: bool = False) -> None:
    del force  # CLI handles user confirmation before calling this function.

    rollouts_dir_path = rollouts_dir(config)
    if dry_run:
        return

    # Remove rollout images and metadata files referenced by DB.
    _delete_rollout_images_from_db(config)

    clear_dir_contents(rollouts_dir_path)
    with connect_db(config.home_dir) as conn:
        conn.execute("DELETE FROM runtime_images")
        conn.execute("DELETE FROM rollouts")
        conn.commit()


def cleanup_workers(config: SparkVMConfig, *, force: bool = False, dry_run: bool = False) -> None:
    del force  # CLI handles user confirmation before calling this function.

    workers_dir_path = workers_dir(config)
    if dry_run:
        return

    ensure_dir(workers_dir_path, exist_ok=True)
    for child in workers_dir_path.iterdir():
        if child.is_dir():
            unmount_under(child)
            remove_tree(child, ignore_errors=False)
        else:
            remove_file(child, missing_ok=True)
    with connect_db(config.home_dir) as conn:
        conn.execute("DELETE FROM reservations")
        conn.execute("DELETE FROM workers")
        conn.commit()


def cleanup_all(config: SparkVMConfig, *, force: bool = False, dry_run: bool = False) -> None:
    if dry_run:
        return

    cleanup_rollouts(config, force=force, dry_run=False)
    cleanup_workers(config, force=force, dry_run=False)

    # Remove any remaining image artifacts not tied to current rollout rows,
    # but keep the managed kernel image.
    clear_dir_contents_except(config.home_dir / "images", keep_files={KERNEL_FILENAME})

    # Clear runtime DB state.
    with connect_db(config.home_dir) as conn:
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM reservations")
        conn.execute("DELETE FROM workers")
        conn.execute("DELETE FROM runtime_images")
        conn.execute("DELETE FROM rollouts")
        conn.commit()


def confirm_cleanup(target: str, *, force: bool) -> bool:
    if force:
        return True
    response = input(f"This will delete {target}. Continue? [y/N] ").strip().lower()
    return response in {"y", "yes"}


def run_cleanup_command(home_dir: str | None, target: str, force: bool) -> int:
    if not confirm_cleanup(target, force=force):
        print("Aborted.")
        return 0

    # temp config build for path resolution 
    config = build_config(
        vcpu=DEFAULT_VCPU,
        memory=DEFAULT_MEMORY,
        timeout=DEFAULT_TIMEOUT_SEC,
        runtime=DEFAULT_RUNTIME,
        home_dir=home_dir,
    )
    configure_logging(home_dir=config.home_dir)
    logger = logging.getLogger("sparkvm.cleanup")

    if target == "rollouts":
        cleanup_rollouts(config, force=force, dry_run=False)
    elif target == "workers":
        cleanup_workers(config, force=force, dry_run=False)
    elif target == "all":
        cleanup_all(config, force=force, dry_run=False)
    else:
        raise SparkVMError(f"Unsupported cleanup target: {target}")

    log_event(logger, component="cleanup", event="done", fields={"target": target})
    print(f"SparkVM cleanup complete: {target}")
    return 0


def reset_home(config: SparkVMConfig, *, dry_run: bool = False) -> None:
    home_dir = config.home_dir
    if not home_dir.exists():
        return

    # Ensure mounted worker subpaths are unmounted before deleting home contents.
    workers_dir_path = config.workers_dir
    if workers_dir_path.exists():
        for vm_dir in list_dirs_with_prefix(workers_dir_path, "worker-"):
            if not dry_run:
                unmount_under(vm_dir)

    for child in home_dir.iterdir():
        if dry_run:
            continue
        if child.is_dir():
            remove_tree(child, ignore_errors=False)
        else:
            remove_file(child, missing_ok=True)

    if not dry_run:
        ensure_dir(home_dir, exist_ok=True)


def run_reset_command(home_dir: str | None, force: bool) -> int:
    if not confirm_cleanup("ALL SparkVM data under ~/.sparkvm", force=force):
        print("Aborted.")
        return 0

    config = build_config(
        vcpu=DEFAULT_VCPU,
        memory=DEFAULT_MEMORY,
        timeout=DEFAULT_TIMEOUT_SEC,
        runtime=DEFAULT_RUNTIME,
        home_dir=home_dir,
    )
    reset_home(config, dry_run=False)
    print("SparkVM reset complete.")
    return 0


__all__ = [
    "cleanup_rollouts",
    "cleanup_workers",
    "cleanup_all",
    "confirm_cleanup",
    "run_cleanup_command",
    "run_reset_command",
]
