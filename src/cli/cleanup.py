"""Cleanup helpers for rollouts and preserved worker directories."""

from __future__ import annotations

from pathlib import Path

from sparkvm.commands import run_checked
from sparkvm.config import DEFAULT_MEMORY, DEFAULT_RUNTIME, DEFAULT_TIMEOUT_SEC, DEFAULT_VCPU, SparkVMConfig, build_config
from sparkvm.errors import CleanupError, SparkVMError
from sparkvm.fsops import (
    ensure_dir,
    list_dirs_with_prefix,
    read_text,
    remove_file,
    remove_tree,
    write_json_atomic,
)

from sparkvm.constants import ROLLOUT_METADATA_VERSION
from sparkvm.utils import (
    mount_points_under,
    path_within,
    unescape_mount_path,
    unmount_under,
)


def rollouts_dir(config: SparkVMConfig) -> Path:
    return config.home_dir / "rollouts"


def workers_dir(config: SparkVMConfig) -> Path:
    return config.workers_dir


def metadata_payload() -> dict[str, object]:
    return {"version": ROLLOUT_METADATA_VERSION, "rollouts": {}}


def write_rollout_metadata_reset(rollouts_dir_path: Path) -> None:
    metadata_path = rollouts_dir_path / "metadata.json"
    try:
        write_json_atomic(metadata_path, metadata_payload(), encoding="utf-8", pretty=True)
    except OSError as exc:
        raise CleanupError(f"Could not reset rollout metadata at {metadata_path}") from exc


def cleanup_rollouts(config: SparkVMConfig, *, force: bool = False, dry_run: bool = False) -> None:
    del force  # CLI handles user confirmation before calling this function.

    rollouts_dir_path = rollouts_dir(config)
    for child in list_dirs_with_prefix(rollouts_dir_path, "rollout-"):
        if not dry_run:
            remove_tree(child, ignore_errors=False)

    if not dry_run:
        write_rollout_metadata_reset(rollouts_dir_path)


def cleanup_workers(config: SparkVMConfig, *, force: bool = False, dry_run: bool = False) -> None:
    del force  # CLI handles user confirmation before calling this function.

    workers_dir_path = workers_dir(config)
    if not workers_dir_path.exists():
        return

    for vm_dir in list_dirs_with_prefix(workers_dir_path, "vm-"):
        if dry_run:
            continue
        remove_tree(vm_dir, ignore_errors=False)

    if dry_run:
        return

    # Remove stale socket/image files left behind outside vm-* directories.
    for candidate in workers_dir_path.rglob("*"):
        if candidate.name not in {"firecracker.sock", "rollout.ext4"}:
            continue
        try:
            remove_file(candidate, missing_ok=True)
        except OSError:
            continue


def cleanup_all(config: SparkVMConfig, *, force: bool = False, dry_run: bool = False) -> None:
    cleanup_rollouts(config, force=force, dry_run=dry_run)
    cleanup_workers(config, force=force, dry_run=dry_run)


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

    if target == "rollouts":
        cleanup_rollouts(config, force=force, dry_run=False)
    elif target == "workers":
        cleanup_workers(config, force=force, dry_run=False)
    elif target == "all":
        cleanup_all(config, force=force, dry_run=False)
    else:
        raise SparkVMError(f"Unsupported cleanup target: {target}")

    print(f"SparkVM cleanup complete: {target}")
    return 0


def reset_home(config: SparkVMConfig, *, dry_run: bool = False) -> None:
    home_dir = config.home_dir
    if not home_dir.exists():
        return

    # Ensure mounted worker subpaths are unmounted before deleting home contents.
    workers_dir_path = config.workers_dir
    if workers_dir_path.exists():
        for vm_dir in list_dirs_with_prefix(workers_dir_path, "vm-"):
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
