"""Internal scheduler worker runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.config import resolve_home_dir
from ..storage.db import connect_db
from ..core.errors import JobTimeoutError, WorkerNotFoundError
from ..storage.repositories import EventRepository, RolloutRepository, WorkerRepository
from ..storage.state_store import get_rollout
from ..core.utils import now_utc_iso
from ..api.vm import SparkVM
from ..api.workers import Workers


class WorkerRunner:
    def __init__(self, worker_id: str, *, home_dir: str | Path | None = None) -> None:
        self.home_dir = resolve_home_dir(home_dir)
        self.worker_id = worker_id
        self.workers = Workers(home_dir=self.home_dir)
        self.worker_repo = WorkerRepository(self.home_dir)
        self.rollout_repo = RolloutRepository(self.home_dir)
        self.events = EventRepository(self.home_dir)

    def _worker_dir(self) -> Path:
        return self.home_dir / "workers" / self.worker_id

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _finalize_rollout_on_failure(self, rollout_id: str, worker_id: str) -> None:
        with connect_db(self.home_dir) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM rollouts WHERE id = ?", (rollout_id,)).fetchone()
                if row is None:
                    conn.commit()
                    return
                rollout = dict(row)
                retry_count = int(rollout.get("retry_count", 0)) + 1
                max_retries = int(rollout.get("max_retries", 3))
                exhausted = retry_count >= max_retries
                status = "exhausted" if exhausted else "retry_pending"
                now = now_utc_iso()
                conn.execute(
                    """
                    UPDATE rollouts
                    SET retry_count = ?, status = ?, active_worker_id = NULL, last_worker_id = ?, completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (retry_count, status, worker_id, now, now, rollout_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _finalize_rollout_on_success(self, rollout_id: str, worker_id: str) -> None:
        with connect_db(self.home_dir) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = now_utc_iso()
                conn.execute(
                    """
                    UPDATE rollouts
                    SET status = 'passed', active_worker_id = NULL, last_worker_id = ?, completed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (worker_id, now, now, rollout_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _release_reservation(self, reservation_id: str) -> None:
        with connect_db(self.home_dir) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE reservations SET status = 'released', updated_at = ? WHERE id = ?",
                    (now_utc_iso(), reservation_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def run(self) -> int:
        worker = self.workers.load_worker(self.worker_id)
        rollout_id = str(worker["rollout_id"])
        reservation_raw = worker.get("reservation_id")
        reservation_id = str(reservation_raw) if isinstance(reservation_raw, str) and reservation_raw else None
        if self.rollout_repo.get(rollout_id) is None:
            raise WorkerNotFoundError(f"Rollout not found for worker: {rollout_id}")

        get_rollout(rollout_id, self.home_dir)
        self.workers.mark_worker_status(self.worker_id, "running")

        self.events.add("worker", self.worker_id, "worker_running", data={"rollout_id": rollout_id})

        vm = SparkVM(
            vcpu=int(worker.get("vcpu", 2)),
            memory=str(worker.get("memory", "2G")),
            disk=str(worker.get("disk", "4G")),
            timeout=float(worker.get("timeout", 60.0)),
            network=bool(worker.get("network", True)),
            env=dict(worker.get("env", {})),
        )

        try:
            result = vm.run_as_worker(rollout_id, self.worker_id)
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
            if result.passed:
                self.worker_repo.mark_passed(self.worker_id, result_payload)
                if reservation_id is not None:
                    self._release_reservation(reservation_id)
                self._finalize_rollout_on_success(rollout_id, self.worker_id)
                self.events.add("worker", self.worker_id, "worker_passed", data={"rollout_id": rollout_id})
                self.events.add("rollout", rollout_id, "rollout_passed", data={"worker_id": self.worker_id})
                return 0

            failed_status = "timeout" if str(result.status) == "timeout" else "failed"
            failure_payload = {
                "worker_id": self.worker_id,
                "rollout_id": rollout_id,
                "status": failed_status,
                "error_type": "VMRunFailed",
                "error_message": f"VM returned status={result.status} exit_code={result.exit_code}",
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "created_at": now_utc_iso(),
            }
            self._write_json(self._worker_dir() / "failure.json", failure_payload)
            if failed_status == "timeout":
                self.worker_repo.mark_timeout(self.worker_id, failure_payload)
            else:
                self.worker_repo.mark_failed(self.worker_id, failure_payload)

            if reservation_id is not None:
                self._release_reservation(reservation_id)
            self._finalize_rollout_on_failure(rollout_id, self.worker_id)
            self.events.add(
                "worker",
                self.worker_id,
                "worker_failed",
                data={"rollout_id": rollout_id, "status": failed_status, "error_type": "VMRunFailed"},
            )
            self.events.add(
                "rollout",
                rollout_id,
                "rollout_failed",
                data={"worker_id": self.worker_id, "status": failed_status, "error_type": "VMRunFailed"},
            )
            return 1
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
            if failed_status == "timeout":
                self.worker_repo.mark_timeout(self.worker_id, failure_payload)
            else:
                self.worker_repo.mark_failed(self.worker_id, failure_payload)

            if reservation_id is not None:
                self._release_reservation(reservation_id)
            self._finalize_rollout_on_failure(rollout_id, self.worker_id)
            self.events.add(
                "worker",
                self.worker_id,
                "worker_failed",
                data={"rollout_id": rollout_id, "status": failed_status, "error_type": type(exc).__name__},
            )
            self.events.add(
                "rollout",
                rollout_id,
                "rollout_failed",
                data={"worker_id": self.worker_id, "status": failed_status, "error_type": type(exc).__name__},
            )
            return 1


__all__ = ["WorkerRunner"]
