"""Preserved failed worker attempt inspection and lifecycle helpers."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import resolve_home_dir
from .errors import CleanupError, WorkerMetadataError, WorkerNotFoundError

_WORKER_ID_RE = re.compile(r"^vm-[A-Za-z0-9]+$")


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


def _validate_worker_id(vm_id: str) -> str:
    if not isinstance(vm_id, str) or not vm_id.strip():
        raise WorkerNotFoundError("vm_id must be a non-empty string.")
    candidate = vm_id.strip()
    if not _WORKER_ID_RE.fullmatch(candidate):
        raise WorkerNotFoundError(f"Invalid worker id format: {vm_id!r}")
    return candidate


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
                f"Could not unmount active mount '{mount_path}' while deleting worker '{base_dir.name}'."
            ) from exc


@dataclass(frozen=True)
class Worker:
    vm_id: str
    path: Path
    rollout_id: str | None
    rollout_name: str | None
    status: str
    error_type: str | None
    error_message: str | None
    duration_ms: int | None
    created_at: str | None
    firecracker_log_path: Path
    failure_path: Path | None


class Workers:
    def __init__(self, home_dir: str | Path | None = None) -> None:
        self.home_dir = resolve_home_dir(home_dir)
        self.workers_dir = self.home_dir / "workers"

    def path(self, vm_id: str) -> Path:
        return self.workers_dir / _validate_worker_id(vm_id)

    def list(self) -> list[Worker]:
        if not self.workers_dir.exists():
            return []

        items: list[Worker] = []
        for candidate in sorted(self.workers_dir.iterdir()):
            if not candidate.is_dir() or not candidate.name.startswith("vm-"):
                continue
            items.append(self._build_worker(candidate))
        return items

    def get_by_id(self, vm_id: str) -> Worker:
        worker_path = self.path(vm_id)
        if not worker_path.is_dir():
            raise WorkerNotFoundError(f"Worker not found: {_validate_worker_id(vm_id)}")
        return self._build_worker(worker_path)

    def delete_by_id(self, vm_id: str, *, force: bool = False) -> None:
        del force  # CLI handles prompt semantics.
        worker = self.get_by_id(vm_id)
        _unmount_under(worker.path)
        try:
            shutil.rmtree(worker.path)
        except OSError as exc:
            raise CleanupError(f"Could not delete worker directory: {worker.path}") from exc

    def log_text(self, vm_id: str, *, tail: int | None = None) -> str:
        worker = self.get_by_id(vm_id)
        log_path = worker.firecracker_log_path
        if not log_path.exists():
            return ""
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise WorkerMetadataError(f"Could not read worker log: {log_path}") from exc

        if tail is None:
            return text
        if tail <= 0:
            return ""
        lines = text.splitlines()
        return "\n".join(lines[-tail:])

    def failure_json(self, vm_id: str) -> dict[str, Any]:
        worker_path = self.path(vm_id)
        if not worker_path.is_dir():
            raise WorkerNotFoundError(f"Worker not found: {_validate_worker_id(vm_id)}")
        failure_path = worker_path / "failure.json"
        if not failure_path.exists():
            raise WorkerMetadataError(f"Worker failure metadata missing: {failure_path}")

        return _read_failure_json(failure_path)

    def _build_worker(self, worker_path: Path) -> Worker:
        vm_id = worker_path.name
        failure_path = worker_path / "failure.json"
        firecracker_log_path = worker_path / "firecracker.log"

        status = "unknown"
        rollout_id: str | None = None
        rollout_name: str | None = None
        error_type: str | None = None
        error_message: str | None = None
        duration_ms: int | None = None
        created_at: str | None = None
        failure_path_value: Path | None = None

        if failure_path.exists():
            failure_path_value = failure_path
            try:
                data = _read_failure_json(failure_path)
            except WorkerMetadataError:
                status = "unknown"
            else:
                status = str(data.get("status") or "failed")
                rollout_id = _optional_str(data.get("rollout_id"))
                rollout_name = _optional_str(data.get("rollout_name"))
                error_type = _optional_str(data.get("error_type"))
                error_message = _optional_str(data.get("error_message"))
                duration_ms = _optional_int(data.get("duration_ms"))
                created_at = _optional_str(data.get("created_at"))
                log_from_json = _optional_str(data.get("firecracker_log_path"))
                if log_from_json:
                    firecracker_log_path = Path(log_from_json)

        return Worker(
            vm_id=vm_id,
            path=worker_path,
            rollout_id=rollout_id,
            rollout_name=rollout_name,
            status=status,
            error_type=error_type,
            error_message=error_message,
            duration_ms=duration_ms,
            created_at=created_at,
            firecracker_log_path=firecracker_log_path,
            failure_path=failure_path_value,
        )


def _read_failure_json(failure_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(failure_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkerMetadataError(f"Corrupt worker failure metadata: {failure_path}") from exc
    except OSError as exc:
        raise WorkerMetadataError(f"Could not read worker failure metadata: {failure_path}") from exc

    if not isinstance(data, dict):
        raise WorkerMetadataError(f"Worker failure metadata must be a JSON object: {failure_path}")
    return data


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


__all__ = [
    "Worker",
    "Workers",
]
