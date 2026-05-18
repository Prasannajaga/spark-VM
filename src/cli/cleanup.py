"""Cleanup helpers for rollouts and preserved worker directories."""

from __future__ import annotations

import subprocess
from pathlib import Path

from sparkvm.config import DEFAULT_BASE_IMAGE, DEFAULT_MEMORY, DEFAULT_TIMEOUT_SEC, DEFAULT_VCPU, SparkVMConfig, build_config
from sparkvm.errors import CleanupError, SparkVMError
from sparkvm.fsops import (
    ensure_dir,
    list_dirs_with_prefix,
    read_text,
    remove_file,
    remove_tree,
    write_json_atomic,
)

_ROLLOUT_METADATA_VERSION = 1


def _run_checked(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise CleanupError(f"Required command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or "command failed"
        raise CleanupError(f"Command failed: {' '.join(cmd)}\n{detail}") from exc


def _rollouts_dir(config: SparkVMConfig) -> Path:
    return config.home_dir / "rollouts"


def _workers_dir(config: SparkVMConfig) -> Path:
    return config.workers_dir


def _metadata_payload() -> dict[str, object]:
    return {"version": _ROLLOUT_METADATA_VERSION, "rollouts": []}


def _write_rollout_metadata_reset(rollouts_dir: Path) -> None:
    metadata_path = rollouts_dir / "metadata.json"
    try:
        write_json_atomic(metadata_path, _metadata_payload(), encoding="utf-8", pretty=True)
    except OSError as exc:
        raise CleanupError(f"Could not reset rollout metadata at {metadata_path}") from exc


def _path_within(base: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(base)
        return True
    except ValueError:
        return False


def _unescape_mount_path(raw: str) -> str:
    return (
        raw.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _mount_points_under(base_dir: Path) -> list[Path]:
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return []

    points: list[Path] = []
    try:
        lines = read_text(mountinfo, encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue
        mount_path = Path(_unescape_mount_path(parts[4]))
        if mount_path == base_dir or _path_within(base_dir, mount_path):
            points.append(mount_path)

    return sorted(points, key=lambda path: len(path.parts), reverse=True)


def _unmount_under(base_dir: Path) -> None:
    for mount_path in _mount_points_under(base_dir):
        try:
            _run_checked(["umount", str(mount_path)])
        except CleanupError as exc:
            raise CleanupError(
                f"Could not unmount active mount '{mount_path}' while cleaning '{base_dir}'. "
                "Resolve mounts and try again."
            ) from exc


def cleanup_rollouts(config: SparkVMConfig, *, force: bool = False, dry_run: bool = False) -> None:
    del force  # CLI handles user confirmation before calling this function.

    rollouts_dir = _rollouts_dir(config)
    for child in list_dirs_with_prefix(rollouts_dir, "rollout-"):
        if not dry_run:
            remove_tree(child, ignore_errors=False)

    if not dry_run:
        _write_rollout_metadata_reset(rollouts_dir)


def cleanup_workers(config: SparkVMConfig, *, force: bool = False, dry_run: bool = False) -> None:
    del force  # CLI handles user confirmation before calling this function.

    workers_dir = _workers_dir(config)
    if not workers_dir.exists():
        return

    for vm_dir in list_dirs_with_prefix(workers_dir, "vm-"):
        if dry_run:
            continue
        _unmount_under(vm_dir)
        remove_tree(vm_dir, ignore_errors=False)

    if dry_run:
        return

    # Remove stale socket/image files left behind outside vm-* directories.
    for candidate in workers_dir.rglob("*"):
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

    config = build_config(
        vcpu=DEFAULT_VCPU,
        memory=DEFAULT_MEMORY,
        timeout=DEFAULT_TIMEOUT_SEC,
        base_image=DEFAULT_BASE_IMAGE,
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


def _reset_home(config: SparkVMConfig, *, dry_run: bool = False) -> None:
    home_dir = config.home_dir
    if not home_dir.exists():
        return

    # Ensure mounted worker subpaths are unmounted before deleting home contents.
    workers_dir = config.workers_dir
    if workers_dir.exists():
        for vm_dir in list_dirs_with_prefix(workers_dir, "vm-"):
            if not dry_run:
                _unmount_under(vm_dir)

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
        base_image=DEFAULT_BASE_IMAGE,
        home_dir=home_dir,
    )
    _reset_home(config, dry_run=False)
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
