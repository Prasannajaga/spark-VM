"""Cleanup helpers for rollouts and preserved worker directories."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .config import SparkVMConfig
from .errors import CleanupError

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
    rollouts_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = rollouts_dir / "metadata.json"
    tmp_path = rollouts_dir / "metadata.json.tmp"
    payload = json.dumps(_metadata_payload(), indent=2, sort_keys=True) + "\n"
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, metadata_path)
    except OSError as exc:
        raise CleanupError(f"Could not reset rollout metadata at {metadata_path}") from exc
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


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
        lines = mountinfo.read_text(encoding="utf-8", errors="replace").splitlines()
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
    if rollouts_dir.exists():
        for child in rollouts_dir.iterdir():
            if child.is_dir() and child.name.startswith("rollout-") and not dry_run:
                shutil.rmtree(child, ignore_errors=False)

    if not dry_run:
        _write_rollout_metadata_reset(rollouts_dir)


def cleanup_workers(config: SparkVMConfig, *, force: bool = False, dry_run: bool = False) -> None:
    del force  # CLI handles user confirmation before calling this function.

    workers_dir = _workers_dir(config)
    if not workers_dir.exists():
        return

    vm_dirs = [path for path in workers_dir.iterdir() if path.is_dir() and path.name.startswith("vm-")]
    for vm_dir in vm_dirs:
        if dry_run:
            continue
        _unmount_under(vm_dir)
        shutil.rmtree(vm_dir, ignore_errors=False)

    if dry_run:
        return

    # Remove stale socket/image files left behind outside vm-* directories.
    for candidate in workers_dir.rglob("*"):
        if candidate.name not in {"firecracker.sock", "rollout.ext4"}:
            continue
        try:
            candidate.unlink()
        except OSError:
            # Ignore files that disappear concurrently or cannot be unlinked due to races.
            continue


def cleanup_all(config: SparkVMConfig, *, force: bool = False, dry_run: bool = False) -> None:
    cleanup_rollouts(config, force=force, dry_run=dry_run)
    cleanup_workers(config, force=force, dry_run=dry_run)


__all__ = [
    "cleanup_rollouts",
    "cleanup_workers",
    "cleanup_all",
]
