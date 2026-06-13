"""Internal scheduler worker runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.config import resolve_home_dir
from ..core.fsops import write_text
from ..storage.db import connect_db
from ..storage.query_builder import QueryBuilder
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

    def _persist_phase_logs(self, *, result: Any) -> dict[str, Any]:
        worker_dir = self._worker_dir()
        results_log_path = worker_dir / "results.log"
        phase_meta: dict[str, dict[str, Any]] = {}
        sections: list[str] = []

        def _append_section(category: str, content: str) -> None:
            body = content.rstrip("\n")
            if body:
                sections.append(f"[{category}]\n{body}\n")
            else:
                sections.append(f"[{category}]\n<empty>\n")

        def _collect_phase(phase_name: str, phase_obj: Any) -> None:
            if phase_obj is None:
                return
            stdout_text = str(getattr(phase_obj, "stdout", "") or "")
            stderr_text = str(getattr(phase_obj, "stderr", "") or "")
            exit_code = getattr(phase_obj, "exit_code", None)

            _append_section(f"{phase_name}.stdout", stdout_text)
            _append_section(f"{phase_name}.stderr", stderr_text)

            phase_meta[phase_name] = {
                "exit_code": int(exit_code) if exit_code is not None else None,
            }

        _collect_phase("network", getattr(result, "network", None))
        _collect_phase("setup", getattr(result, "setup", None))
        _collect_phase("run", getattr(result, "run", None))
        _append_section("final_exit_code", f"{int(getattr(result, 'exit_code', 0))}")
        write_text(results_log_path, "\n".join(sections).rstrip() + "\n", encoding="utf-8")

        return {
            "results_log_path": str(results_log_path),
            "result_files": ["results.log"],
            "phases": phase_meta,
        }

    def _finalize_worker_failure(self, rollout_id: str, reservation_id: str | None, failed_status: str, failure_payload: dict[str, Any], error_type: str) -> None:
        with connect_db(self.home_dir) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = now_utc_iso()
                qb = QueryBuilder(conn)
                
                # Update worker
                failure_json = json.dumps(failure_payload, sort_keys=True)
                qb.update(
                    "workers",
                    {"status": failed_status, "completed_at": now, "failure_json": failure_json, "updated_at": now},
                    where={"id": self.worker_id}
                )
                
                # Release reservation
                if reservation_id is not None:
                    qb.update(
                        "reservations",
                        {"status": "released", "updated_at": now},
                        where={"id": reservation_id}
                    )

                # Update rollout
                row = qb.from_table("rollouts").where(id=rollout_id).fetch_one()
                if row is not None:
                    rollout = dict(row)
                    retry_count = int(rollout.get("retry_count", 0)) + 1
                    max_retries = int(rollout.get("max_retries", 3))
                    exhausted = retry_count >= max_retries
                    status = "exhausted" if exhausted else "retry_pending"
                    qb.update(
                        "rollouts",
                        {
                            "retry_count": retry_count,
                            "status": status,
                            "active_worker_id": None,
                            "last_worker_id": self.worker_id,
                            "completed_at": now,
                            "updated_at": now
                        },
                        where={"id": rollout_id}
                    )

                # Add events
                worker_data = json.dumps({"rollout_id": rollout_id, "status": failed_status, "error_type": error_type})
                rollout_data = json.dumps({"worker_id": self.worker_id, "status": failed_status, "error_type": error_type})
                qb.insert("events", {
                    "entity_type": "worker",
                    "entity_id": self.worker_id,
                    "event_type": "worker_failed",
                    "data_json": worker_data,
                    "created_at": now
                })
                qb.insert("events", {
                    "entity_type": "rollout",
                    "entity_id": rollout_id,
                    "event_type": "rollout_failed",
                    "data_json": rollout_data,
                    "created_at": now
                })
                
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _finalize_worker_success(self, rollout_id: str, reservation_id: str | None) -> None:
        now = now_utc_iso()
        with connect_db(self.home_dir) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                qb = QueryBuilder(conn)
                
                # Update worker
                qb.update(
                    "workers",
                    {"status": "passed", "completed_at": now, "updated_at": now},
                    where={"id": self.worker_id}
                )

                # Release reservation
                if reservation_id is not None:
                    qb.update(
                        "reservations",
                        {"status": "released", "updated_at": now},
                        where={"id": reservation_id}
                    )

                # Update rollout
                qb.update(
                    "rollouts",
                    {
                        "status": "passed",
                        "active_worker_id": None,
                        "last_worker_id": self.worker_id,
                        "completed_at": now,
                        "updated_at": now
                    },
                    where={"id": rollout_id}
                )

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        try:
            with connect_db(self.home_dir) as conn:
                qb = QueryBuilder(conn)
                worker_data = json.dumps({"rollout_id": rollout_id})
                rollout_data = json.dumps({"worker_id": self.worker_id})
                qb.insert("events", {
                    "entity_type": "worker",
                    "entity_id": self.worker_id,
                    "event_type": "worker_passed",
                    "data_json": worker_data,
                    "created_at": now
                })
                qb.insert("events", {
                    "entity_type": "rollout",
                    "entity_id": rollout_id,
                    "event_type": "rollout_passed",
                    "data_json": rollout_data,
                    "created_at": now
                })
                conn.commit()
        except Exception:
            pass

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
            secure=bool(worker.get("secure", True)),
            env=dict(worker.get("env", {})),
        )

        try:
            result = vm.run_as_worker(rollout_id, self.worker_id)
            phase_logs = self._persist_phase_logs(result=result)
            result_payload = {
                "worker_id": self.worker_id,
                "rollout_id": rollout_id,
                "vm_id": result.vm_id,
                "status": result.status,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "passed": result.passed,
                "created_at": now_utc_iso(),
                "results_log_path": phase_logs["results_log_path"],
                "result_files": phase_logs["result_files"],
                "phase_summary": phase_logs["phases"],
            }
            self._write_json(self._worker_dir() / "result.json", result_payload)
            if result.passed:
                self._finalize_worker_success(rollout_id, reservation_id)
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
            self._finalize_worker_failure(rollout_id, reservation_id, failed_status, failure_payload, "VMRunFailed")
            return 1
        except Exception as exc:
            failed_status = "timeout" if isinstance(exc, JobTimeoutError) else "failed"
            error_type = type(exc).__name__
            failure_payload = {
                "worker_id": self.worker_id,
                "rollout_id": rollout_id,
                "status": failed_status,
                "error_type": error_type,
                "error_message": str(exc),
                "created_at": now_utc_iso(),
            }
            self._write_json(self._worker_dir() / "failure.json", failure_payload)
            self._finalize_worker_failure(rollout_id, reservation_id, failed_status, failure_payload, error_type)
            return 1


__all__ = ["WorkerRunner"]
