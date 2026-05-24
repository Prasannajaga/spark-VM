"""Internal scheduler worker runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import resolve_home_dir
from .errors import JobTimeoutError
from .vm import SparkVM
from .state_store import get_rollout, load_rollouts_metadata, save_rollouts_metadata, scheduler_lock
from .utils import now_utc_iso
from .workers import Workers
from .reservations import release


class WorkerRunner:
    def __init__(self, worker_id: str, *, home_dir: str | Path | None = None) -> None:
        self.home_dir = resolve_home_dir(home_dir)
        self.worker_id = worker_id
        self.workers = Workers(home_dir=self.home_dir)

    def _worker_dir(self) -> Path:
        return self.home_dir / "workers" / self.worker_id

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _finalize_rollout_on_failure(self, rollout_id: str, worker_id: str) -> None:
        with scheduler_lock(self.home_dir):
            metadata = load_rollouts_metadata(self.home_dir)
            rollouts = metadata.get("rollouts", {})
            if not isinstance(rollouts, dict):
                return
            entry = rollouts.get(rollout_id)
            if not isinstance(entry, dict):
                return

            retry_count = int(entry.get("retry_count", 0)) + 1
            max_retries = int(entry.get("max_retries", 3))
            exhausted = retry_count >= max_retries
            entry["retry_count"] = retry_count
            entry["status"] = "exhausted" if exhausted else "retry_pending"
            entry["active_worker_id"] = None
            entry["last_worker_id"] = worker_id
            entry["completed_at"] = now_utc_iso()
            entry["updated_at"] = now_utc_iso()
            rollouts[rollout_id] = entry
            save_rollouts_metadata(metadata, self.home_dir)

    def _finalize_rollout_on_success(self, rollout_id: str, worker_id: str) -> None:
        with scheduler_lock(self.home_dir):
            metadata = load_rollouts_metadata(self.home_dir)
            rollouts = metadata.get("rollouts", {})
            if not isinstance(rollouts, dict):
                return
            entry = rollouts.get(rollout_id)
            if not isinstance(entry, dict):
                return
            entry["status"] = "passed"
            entry["active_worker_id"] = None
            entry["last_worker_id"] = worker_id
            entry["completed_at"] = now_utc_iso()
            entry["updated_at"] = now_utc_iso()
            rollouts[rollout_id] = entry
            save_rollouts_metadata(metadata, self.home_dir)

    def run(self) -> int:
        worker = self.workers.load_worker(self.worker_id)
        rollout_id = str(worker["rollout_id"])
        reservation_id = str(worker["reservation_id"])
        get_rollout(rollout_id, self.home_dir)

        self.workers.mark_worker_status(self.worker_id, "running")

        vm = SparkVM(
            vcpu=int(worker.get("vcpu", 2)),
            memory=str(worker.get("memory", "2G")),
            disk=str(worker.get("disk", "4G")),
            timeout=float(worker.get("timeout", 60.0)),
            network=bool(worker.get("network", True)),
            env=dict(worker.get("env", {})),
        )

        try:
            result = vm.run(rollout_id)
            result_payload = {
                "worker_id": self.worker_id,
                "rollout_id": rollout_id,
                "vm_id": result.vm_id,
                "status": result.status,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "passed": result.passed,
                "created_at": now_utc_iso(),
            }
            self._write_json(self._worker_dir() / "result.json", result_payload)
            self.workers.mark_worker_status(self.worker_id, "passed")

            with scheduler_lock(self.home_dir):
                release(reservation_id, home_dir=self.home_dir)
            self._finalize_rollout_on_success(rollout_id, self.worker_id)
            return 0
        except Exception as exc:
            failed_status = "timeout" if isinstance(exc, JobTimeoutError) else "failed"
            failure_payload = {
                "worker_id": self.worker_id,
                "rollout_id": rollout_id,
                "status": failed_status,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "created_at": now_utc_iso(),
            }
            self._write_json(self._worker_dir() / "failure.json", failure_payload)
            self.workers.mark_worker_status(self.worker_id, failed_status)

            with scheduler_lock(self.home_dir):
                release(reservation_id, home_dir=self.home_dir)
            self._finalize_rollout_on_failure(rollout_id, self.worker_id)
            return 1


__all__ = ["WorkerRunner"]
