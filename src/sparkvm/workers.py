"""Preserved failed worker attempt inspection and lifecycle helpers."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import resolve_home_dir
from .errors import CleanupError, WorkerMetadataError, WorkerNotFoundError
from .fsops import ensure_dir, list_dirs_with_prefix, read_text, remove_tree, write_json_atomic

from .constants import WORKER_ID_RE
from .utils import now_utc_iso


def validate_worker_id(vm_id: str) -> str:
    if not isinstance(vm_id, str) or not vm_id.strip():
        raise WorkerNotFoundError("vm_id must be a non-empty string.")
    candidate = vm_id.strip()
    if not WORKER_ID_RE.fullmatch(candidate):
        raise WorkerNotFoundError(f"Invalid worker id format: {vm_id!r}")
    return candidate


from .utils import (
    mount_points_under,
    path_within,
    unescape_mount_path,
    unmount_under,
)


@dataclass(frozen=True)
class Worker:
    vm_id: str
    path: Path
    rollout_id: str | None
    rollout_name: str | None
    status: str
    exit_code: int | None
    error_type: str | None
    error_message: str | None
    duration_ms: int | None
    created_at: str | None
    firecracker_log_path: Path
    result_path: Path | None
    failure_path: Path | None


class Workers:
    def __init__(self, home_dir: str | Path | None = None) -> None:
        self.home_dir = resolve_home_dir(home_dir)
        self.workers_dir = self.home_dir / "workers"

    def path(self, vm_id: str) -> Path:
        return self.workers_dir / validate_worker_id(vm_id)

    def list(self) -> list[Worker]:
        items: list[Worker] = []
        candidates = list_dirs_with_prefix(self.workers_dir, "vm-") + list_dirs_with_prefix(self.workers_dir, "worker-")
        seen: set[str] = set()
        for candidate in sorted(candidates):
            if candidate.name in seen:
                continue
            seen.add(candidate.name)
            items.append(self.build_worker(candidate))
        return items

    def get_by_id(self, vm_id: str) -> Worker:
        worker_path = self.path(vm_id)
        if not worker_path.is_dir():
            raise WorkerNotFoundError(f"Worker not found: {validate_worker_id(vm_id)}")
        return self.build_worker(worker_path)

    def delete_by_id(self, vm_id: str, *, force: bool = False) -> None:
        del force  # CLI handles prompt semantics.
        worker = self.get_by_id(vm_id)
        unmount_under(worker.path)
        try:
            remove_tree(worker.path, ignore_errors=False)
        except OSError as exc:
            raise CleanupError(f"Could not delete worker directory: {worker.path}") from exc

    def log_text(self, vm_id: str, *, tail: int | None = None) -> str:
        worker = self.get_by_id(vm_id)
        log_path = worker.firecracker_log_path
        if not log_path.exists():
            return ""
        try:
            text = read_text(log_path, encoding="utf-8", errors="replace")
        except OSError as exc:
            raise WorkerMetadataError(f"Could not read worker log: {log_path}") from exc

        if tail is None:
            return text
        if tail <= 0:
            return ""
        lines = text.splitlines()
        return "\n".join(lines[-tail:])

    def stream_log(self, vm_id: str, *, tail: int | None = None, poll_interval_sec: float = 0.2):
        worker = self.get_by_id(vm_id)
        log_path = worker.firecracker_log_path

        # Emit initial snapshot (optionally tailed), then follow appends.
        initial = self.log_text(vm_id, tail=tail)
        if initial:
            if not initial.endswith("\n"):
                initial += "\n"
            yield initial

        offset = 0
        try:
            if log_path.exists():
                offset = log_path.stat().st_size
        except OSError:
            offset = 0

        while True:
            try:
                if not log_path.exists():
                    time.sleep(poll_interval_sec)
                    continue

                size = log_path.stat().st_size
                if size < offset:
                    # Log was truncated/rotated.
                    offset = 0
                if size == offset:
                    time.sleep(poll_interval_sec)
                    continue

                with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                offset = size
                if chunk:
                    yield chunk
            except OSError:
                time.sleep(poll_interval_sec)

    def failure_json(self, vm_id: str) -> dict[str, Any]:
        worker_path = self.path(vm_id)
        if not worker_path.is_dir():
            raise WorkerNotFoundError(f"Worker not found: {validate_worker_id(vm_id)}")
        failure_path = worker_path / "failure.json"
        if not failure_path.exists():
            raise WorkerMetadataError(f"Worker failure metadata missing: {failure_path}")

        return read_failure_json(failure_path)

    def result_json(self, vm_id: str) -> dict[str, Any]:
        worker_path = self.path(vm_id)
        if not worker_path.is_dir():
            raise WorkerNotFoundError(f"Worker not found: {validate_worker_id(vm_id)}")
        result_path = worker_path / "result.json"
        if not result_path.exists():
            raise WorkerMetadataError(f"Worker result metadata missing: {result_path}")
        return read_worker_json(result_path)

    def results_text(self, vm_id: str) -> str:
        worker_path = self.path(vm_id)
        if not worker_path.is_dir():
            raise WorkerNotFoundError(f"Worker not found: {validate_worker_id(vm_id)}")
        results_dir = worker_path / "results"
        if not results_dir.exists() or not results_dir.is_dir():
            return ""

        lines: list[str] = []
        for path in sorted(results_dir.glob("*")):
            if not path.is_file():
                continue
            lines.append(f"== {path.name} ==")
            try:
                lines.append(read_text(path, encoding="utf-8", errors="replace"))
            except OSError:
                lines.append("<unreadable>")
            lines.append("")
        return "\n".join(lines).rstrip()

    def build_worker(self, worker_path: Path) -> Worker:
        vm_id = worker_path.name
        result_path = worker_path / "result.json"
        failure_path = worker_path / "failure.json"
        worker_json_path = worker_path / "worker.json"
        firecracker_log_path = worker_path / "firecracker.log"

        status = "unknown"
        rollout_id: str | None = None
        rollout_name: str | None = None
        exit_code: int | None = None
        error_type: str | None = None
        error_message: str | None = None
        duration_ms: int | None = None
        created_at: str | None = None
        result_path_value: Path | None = None
        failure_path_value: Path | None = None

        if result_path.exists():
            result_path_value = result_path
            try:
                data = read_worker_json(result_path)
            except WorkerMetadataError:
                status = "unknown"
            else:
                status = optional_str(data.get("status")) or "failed"
                rollout_id = optional_str(data.get("rollout_id"))
                rollout_name = optional_str(data.get("rollout_name"))
                exit_code = optional_int(data.get("exit_code"))
                duration_ms = optional_int(data.get("duration_ms"))
                created_at = optional_str(data.get("created_at"))
                log_from_json = optional_str(data.get("firecracker_log_path"))
                if log_from_json:
                    firecracker_log_path = Path(log_from_json)
        elif failure_path.exists():
            failure_path_value = failure_path
            try:
                data = read_failure_json(failure_path)
            except WorkerMetadataError:
                status = "unknown"
            else:
                status = optional_str(data.get("status")) or "failed"
                rollout_id = optional_str(data.get("rollout_id"))
                rollout_name = optional_str(data.get("rollout_name"))
                error_type = optional_str(data.get("error_type"))
                error_message = optional_str(data.get("error_message"))
                duration_ms = optional_int(data.get("duration_ms"))
                created_at = optional_str(data.get("created_at"))
                log_from_json = optional_str(data.get("firecracker_log_path"))
                if log_from_json:
                    firecracker_log_path = Path(log_from_json)
        elif worker_json_path.exists():
            try:
                data = read_worker_json(worker_json_path)
            except WorkerMetadataError:
                status = "unknown"
            else:
                status = optional_str(data.get("status")) or "unknown"
                rollout_id = optional_str(data.get("rollout_id"))
                created_at = optional_str(data.get("created_at"))

        return Worker(
            vm_id=vm_id,
            path=worker_path,
            rollout_id=rollout_id,
            rollout_name=rollout_name,
            status=status,
            exit_code=exit_code,
            error_type=error_type,
            error_message=error_message,
            duration_ms=duration_ms,
            created_at=created_at,
            firecracker_log_path=firecracker_log_path,
            result_path=result_path_value,
            failure_path=failure_path_value,
        )

    def create_worker(
        self,
        *,
        worker_id: str,
        rollout_id: str,
        reservation_id: str,
        attempt: int,
        retry_of: str | None,
        vm_config: dict[str, Any],
        status: str = "reserved",
        pid: int | None = None,
    ) -> dict[str, Any]:
        worker_id = validate_worker_id(worker_id)
        worker_dir = self.path(worker_id)
        ensure_dir(worker_dir, exist_ok=False)
        now = now_utc_iso()
        payload: dict[str, Any] = {
            "id": worker_id,
            "rollout_id": rollout_id,
            "reservation_id": reservation_id,
            "attempt": int(attempt),
            "retry_of": retry_of,
            "vcpu": int(vm_config.get("vcpu", 2)),
            "memory": str(vm_config.get("memory", "2G")),
            "disk": str(vm_config.get("disk", "4G")),
            "timeout": float(vm_config.get("timeout", 60.0)),
            "network": bool(vm_config.get("network", True)),
            "env": dict(vm_config.get("env", {})),
            "status": status,
            "pid": pid,
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "updated_at": now,
        }
        write_json_atomic(worker_dir / "worker.json", payload, pretty=True)
        return payload

    def load_worker(self, worker_id: str) -> dict[str, Any]:
        worker_path = self.path(worker_id)
        worker_json = worker_path / "worker.json"
        if not worker_json.exists():
            raise WorkerNotFoundError(f"Worker metadata missing: {worker_id}")
        return read_worker_json(worker_json)

    def update_worker(self, worker_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        worker_path = self.path(worker_id)
        worker_json = worker_path / "worker.json"
        if not worker_json.exists():
            raise WorkerNotFoundError(f"Worker metadata missing: {worker_id}")
        payload = read_worker_json(worker_json)
        payload.update(patch)
        payload["updated_at"] = now_utc_iso()
        write_json_atomic(worker_json, payload, pretty=True)
        return payload

    def list_workers(self) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for candidate in list_dirs_with_prefix(self.workers_dir, "worker-"):
            worker_json = candidate / "worker.json"
            if not worker_json.exists():
                continue
            try:
                payloads.append(read_worker_json(worker_json))
            except WorkerMetadataError:
                continue
        payloads.sort(key=lambda item: str(item.get("created_at", "")))
        return payloads

    def mark_worker_status(self, worker_id: str, status: str, **extra: Any) -> dict[str, Any]:
        patch: dict[str, Any] = {"status": status}
        patch.update(extra)
        if status in {"starting", "running"}:
            patch.setdefault("started_at", now_utc_iso())
        if status in {"passed", "failed", "timeout", "lost"}:
            patch.setdefault("completed_at", now_utc_iso())
        return self.update_worker(worker_id, patch)


def read_failure_json(failure_path: Path) -> dict[str, Any]:
    return read_worker_json(failure_path)


def read_worker_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(read_text(path, encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkerMetadataError(f"Corrupt worker metadata: {path}") from exc
    except OSError as exc:
        raise WorkerMetadataError(f"Could not read worker metadata: {path}") from exc

    if not isinstance(data, dict):
        raise WorkerMetadataError(f"Worker metadata must be a JSON object: {path}")
    return data


def optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def optional_int(value: object) -> int | None:
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
