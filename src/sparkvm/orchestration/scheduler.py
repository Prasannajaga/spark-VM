"""Background rollout scheduler."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..orchestration.admission import AdmissionController
from ..core.config import resolve_home_dir
from ..core.logger import configure_logging, log_event
from ..storage.db import connect_db
from ..machine.machine_config import MachineConfig
from ..core.utils import now_utc_iso, parse_size_to_bytes
from ..storage.query_builder import QueryBuilder

DEFAULT_VM_CONFIG = {
    "vcpu": 2,
    "memory": "2G",
    "disk": "4G",
    "timeout": 60.0,
    "network": True,
    "env": {},
}


class SparkScheduler:
    def __init__(self, *, home_dir: str | Path | None = None) -> None:
        self.home_dir = resolve_home_dir(home_dir)
        configure_logging(home_dir=self.home_dir)
        self.logger = logging.getLogger("sparkvm.scheduler")
        self._running = False
        self._stop_requested = False

    def _log_scheduler(self, event: str, *, fields: dict[str, Any], level: str = "info", detail: bool = False) -> None:
        log_event(
            self.logger,
            component="scheduler",
            event=event,
            fields=fields,
            level=level,
            detail=detail,
        )

    def _pid_alive(self, pid: int | None) -> bool:
        if pid is None or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _resolve_vm_config(self, rollout_row: dict[str, Any]) -> dict[str, Any]:
        vm_config: dict[str, Any] = {}
        vm_raw = rollout_row.get("vm_config_json")
        if isinstance(vm_raw, str) and vm_raw:
            try:
                parsed = json.loads(vm_raw)
                if isinstance(parsed, dict):
                    vm_config = parsed
            except Exception:
                vm_config = {}

        resolved = dict(DEFAULT_VM_CONFIG)
        resolved.update(vm_config)
        env = resolved.get("env", {})
        resolved["env"] = dict(env) if isinstance(env, dict) else {}
        return resolved

    def _current_active_reservations(self, qb: QueryBuilder) -> list[dict[str, Any]]:
        return (
            qb.from_table("reservations")
            .where_in("status", ["reserved", "starting", "running"])
            .order_by("created_at", "ASC")
            .fetch_all()
        )

    def _fail_rollout(self, qb: QueryBuilder, rollout: dict[str, Any], *, worker_id: str | None) -> None:
        retry_count = int(rollout.get("retry_count", 0)) + 1
        max_retries = int(rollout.get("max_retries", 3))
        status = "retry_pending" if retry_count < max_retries else "exhausted"
        now = now_utc_iso()
        qb.update(
            "rollouts",
            {
                "retry_count": retry_count,
                "status": status,
                "active_worker_id": None,
                "last_worker_id": worker_id,
                "completed_at": now,
                "updated_at": now,
            },
            where={"id": str(rollout["id"])},
        )

    def _pass_rollout(self, qb: QueryBuilder, rollout: dict[str, Any], *, worker_id: str | None) -> None:
        now = now_utc_iso()
        qb.update(
            "rollouts",
            {
                "status": "passed",
                "active_worker_id": None,
                "last_worker_id": worker_id,
                "completed_at": now,
                "updated_at": now,
            },
            where={"id": str(rollout["id"])},
        )

    def _reconcile_unlocked(self, qb: QueryBuilder, *, tick_id: str | None = None) -> None:
        workers = {str(item["id"]): item for item in qb.from_table("workers").fetch_all()}

        # reservation exists but worker row is gone
        for reservation in qb.from_table("reservations").fetch_all():
            if reservation.get("status") not in {"reserved", "starting", "running"}:
                continue
            worker_id = reservation.get("worker_id")
            if isinstance(worker_id, str) and worker_id in workers:
                continue
            qb.update(
                "reservations",
                {"status": "lost", "updated_at": now_utc_iso()},
                where={"id": str(reservation["id"])},
            )

        # worker is active but pid is gone
        for worker_id, worker in list(workers.items()):
            if worker.get("status") not in {"starting", "running"}:
                continue
            pid = worker.get("pid")
            pid_int = int(pid) if isinstance(pid, int) else None
            if self._pid_alive(pid_int):
                continue

            now = now_utc_iso()
            qb.update(
                "workers",
                {"status": "lost", "completed_at": now, "updated_at": now},
                where={"id": worker_id},
            )
            reservation_id = worker.get("reservation_id")
            if isinstance(reservation_id, str):
                qb.update(
                    "reservations",
                    {"status": "lost", "updated_at": now},
                    where={"id": reservation_id},
                )

            rollout_id = worker.get("rollout_id")
            if isinstance(rollout_id, str):
                rollout = qb.from_table("rollouts").where(id=rollout_id).fetch_one()
                if isinstance(rollout, dict) and rollout.get("status") in {"running", "retrying"}:
                    self._fail_rollout(qb, rollout, worker_id=worker_id)
            if tick_id is not None:
                self._log_scheduler(
                    "rollout_terminal",
                    fields={
                        "tick": tick_id,
                        "rollout": rollout_id or "-",
                        "worker": worker_id,
                        "status": "FAILED",
                        "exit": "-",
                        "dur": "-",
                        "err": "lost_pid",
                    },
                    level="warning",
                )

        # rollout consistency from worker terminal states
        for rollout in qb.from_table("rollouts").where_in("status", ["running", "retrying"]).fetch_all():
            rollout_id = str(rollout["id"])
            active_worker_id = rollout.get("active_worker_id")
            if not isinstance(active_worker_id, str):
                self._fail_rollout(qb, rollout, worker_id=None)
                continue

            worker = workers.get(active_worker_id)
            if not isinstance(worker, dict):
                self._fail_rollout(qb, rollout, worker_id=active_worker_id)
                continue

            worker_status = worker.get("status")
            reservation_id = worker.get("reservation_id")
            if worker_status == "passed":
                self._pass_rollout(qb, rollout, worker_id=active_worker_id)
                if isinstance(reservation_id, str):
                    qb.update(
                        "reservations",
                        {"status": "released", "updated_at": now_utc_iso()},
                        where={"id": reservation_id},
                    )
                if tick_id is not None:
                    self._log_scheduler(
                        "rollout_terminal",
                        fields={
                            "tick": tick_id,
                            "rollout": rollout_id,
                            "worker": active_worker_id,
                            "status": "PASSED",
                            "exit": worker.get("exit_code") if worker.get("exit_code") is not None else "-",
                            "dur": "-",
                            "err": "-",
                        },
                    )
                continue

            if worker_status in {"failed", "timeout", "lost"}:
                self._fail_rollout(qb, rollout, worker_id=active_worker_id)
                if isinstance(reservation_id, str):
                    qb.update(
                        "reservations",
                        {"status": "released", "updated_at": now_utc_iso()},
                        where={"id": reservation_id},
                    )
                if tick_id is not None:
                    self._log_scheduler(
                        "rollout_terminal",
                        fields={
                            "tick": tick_id,
                            "rollout": rollout_id,
                            "worker": active_worker_id,
                            "status": "FAILED",
                            "exit": worker.get("exit_code") if worker.get("exit_code") is not None else "-",
                            "dur": "-",
                            "err": worker_status,
                        },
                        level="warning",
                    )

    def _spawn_worker_process(self, worker_id: str) -> int | None:
        cmd = [
            sys.executable,
            "-m",
            "sparkvm.cli.main",
            "--home-dir",
            str(self.home_dir),
            "worker",
            "run",
            worker_id,
        ]
        try:
            proc = subprocess.Popen(cmd, start_new_session=True)
            return int(proc.pid)
        except Exception:
            return None

    def tick(self) -> dict[str, Any]:
        tick_started = time.monotonic()
        tick_id = f"tick-{time.strftime('%H%M%S')}-{uuid4().hex[:4]}"
        policy = MachineConfig.get_policy(home_dir=self.home_dir)
        poll_interval = float(policy.get("poll_interval", 5.0))
        self._log_scheduler(
            "tick_start",
            fields={
                "tick": tick_id,
                "ts": now_utc_iso(),
                "poll": f"{poll_interval:.1f}s",
            },
            detail=True,
        )

        to_spawn: list[dict[str, Any]] = []
        candidates_count = 0
        admitted_count = 0
        spawn_success = 0
        status_counts = {
            "running": 0,
            "passed": 0,
            "failed": 0,
            "retry_pending": 0,
            "exhausted": 0,
            "lost": 0,
        }

        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            qb.execute("BEGIN IMMEDIATE")
            try:
                self._reconcile_unlocked(qb, tick_id=tick_id)

                candidates = qb.fetch_all(
                    """
                    SELECT *
                    FROM rollouts
                    WHERE status IN ('scheduled', 'retry_pending')
                    ORDER BY
                        CASE status
                            WHEN 'scheduled' THEN 0
                            WHEN 'retry_pending' THEN 1
                            ELSE 2
                        END,
                        priority DESC,
                        created_at ASC
                    """
                )
                candidates_count = len(candidates)

                admission = AdmissionController(home_dir=self.home_dir)

                for rollout in candidates:
                    rollout_id = str(rollout["id"])
                    vm_config = self._resolve_vm_config(rollout)
                    live_reservations = self._current_active_reservations(qb)
                    decision = admission.check(vm_config, reservations=live_reservations)
                    if not bool(decision.get("allowed")):
                        continue
                    admitted_count += 1

                    worker_id = f"worker-{uuid4().hex[:12]}"
                    now = now_utc_iso()
                    attempt = int(rollout.get("retry_count", 0)) + 1
                    retry_of = rollout.get("last_worker_id") if rollout.get("status") == "retry_pending" else None
                    worker_dir = self.home_dir / "workers" / worker_id
                    memory = str(vm_config.get("memory", "2G"))
                    disk = str(vm_config.get("disk", "4G"))
                    qb.insert(
                        "workers",
                        {
                            "id": worker_id,
                            "rollout_id": rollout_id,
                            "reservation_id": None,
                            "attempt": attempt,
                            "retry_of": retry_of,
                            "vcpu": int(vm_config.get("vcpu", 2)),
                            "memory": memory,
                            "memory_bytes": parse_size_to_bytes(memory),
                            "disk": disk,
                            "disk_bytes": parse_size_to_bytes(disk),
                            "timeout_seconds": float(vm_config.get("timeout", 60.0)),
                            "network": 1 if bool(vm_config.get("network", True)) else 0,
                            "env_json": json.dumps(dict(vm_config.get("env", {})), sort_keys=True),
                            "pid": None,
                            "worker_dir": str(worker_dir),
                            "rootfs_path": str(worker_dir / "rootfs.ext4"),
                            "execution_disk_path": str(worker_dir / "execution.ext4"),
                            "firecracker_sock_path": str(worker_dir / "firecracker.sock"),
                            "firecracker_log_path": str(worker_dir / "firecracker.log"),
                            "result_path": str(worker_dir / "result.json"),
                            "failure_path": str(worker_dir / "failure.json"),
                            "status": "reserved",
                            "exit_code": None,
                            "failure_phase": None,
                            "created_at": now,
                            "started_at": None,
                            "completed_at": None,
                            "updated_at": now,
                        }
                    )

                    reservation_id = f"res-{uuid4().hex[:12]}"
                    qb.insert(
                        "reservations",
                        {
                            "id": reservation_id,
                            "worker_id": worker_id,
                            "rollout_id": rollout_id,
                            "pid": None,
                            "vcpu": int(vm_config.get("vcpu", 2)),
                            "memory": memory,
                            "memory_bytes": parse_size_to_bytes(memory),
                            "disk": disk,
                            "disk_bytes": parse_size_to_bytes(disk),
                            "status": "reserved",
                            "created_at": now,
                            "updated_at": now,
                            "released_at": None,
                            "last_heartbeat_at": None,
                        }
                    )

                    qb.update(
                        "workers",
                        {"reservation_id": reservation_id, "updated_at": now},
                        where={"id": worker_id},
                    )

                    next_status = "running" if str(rollout.get("status")) == "scheduled" else "retrying"
                    qb.execute(
                        """
                        UPDATE rollouts
                        SET status = ?, active_worker_id = ?, last_worker_id = ?, started_at = COALESCE(started_at, ?), updated_at = ?
                        WHERE id = ?
                        """,
                        (next_status, worker_id, worker_id, now, now, rollout_id),
                    )

                    to_spawn.append(
                        {
                            "worker_id": worker_id,
                            "reservation_id": reservation_id,
                            "rollout_id": rollout_id,
                            "vm_vcpu": int(vm_config.get("vcpu", 2)),
                            "vm_memory": memory,
                            "vm_disk": disk,
                            "vm_timeout": float(vm_config.get("timeout", 60.0)),
                            "vm_network": bool(vm_config.get("network", True)),
                            "vm_env_keys": ",".join(sorted(dict(vm_config.get("env", {})).keys())) or "-",
                        }
                    )

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        for item in to_spawn:
            worker_id = str(item["worker_id"])
            reservation_id = str(item["reservation_id"])
            rollout_id = str(item["rollout_id"])
            pid = self._spawn_worker_process(worker_id)

            with connect_db(self.home_dir) as conn:
                qb = QueryBuilder(conn)
                qb.execute("BEGIN IMMEDIATE")
                try:
                    now = now_utc_iso()
                    if pid is None:
                        qb.update(
                            "workers",
                            {"status": "lost", "completed_at": now, "updated_at": now},
                            where={"id": worker_id},
                        )
                        qb.update(
                            "reservations",
                            {"status": "lost", "updated_at": now},
                            where={"id": reservation_id},
                        )
                        rollout = qb.from_table("rollouts").where(id=rollout_id).fetch_one()
                        if isinstance(rollout, dict):
                            self._fail_rollout(qb, rollout, worker_id=worker_id)
                        self._log_scheduler(
                            "rollout_terminal",
                            fields={
                                "tick": tick_id,
                                "rollout": rollout_id,
                                "worker": worker_id,
                                "status": "FAILED",
                                "exit": "-",
                                "dur": "-",
                                "err": "spawn_failed",
                            },
                            level="warning",
                        )
                        conn.commit()
                        continue

                    qb.execute(
                        "UPDATE workers SET status = 'starting', pid = ?, started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
                        (pid, now, now, worker_id),
                    )
                    qb.update(
                        "reservations",
                        {"pid": pid, "status": "starting", "updated_at": now},
                        where={"id": reservation_id},
                    )
                    spawn_success += 1
                    self._log_scheduler(
                        "spawn_result",
                        fields={
                            "tick": tick_id,
                            "rollout": rollout_id,
                            "worker": worker_id,
                            "pid": pid,
                            "vm": (
                                f"{item.get('vm_vcpu', 2)}cpu "
                                f"{item.get('vm_memory', '2G')} "
                                f"{item.get('vm_disk', '4G')} "
                                f"t={item.get('vm_timeout', 60.0)}s "
                                f"net={'on' if item.get('vm_network', True) else 'off'} "
                                f"env={item.get('vm_env_keys', '-')}"
                            ),
                        },
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

        with connect_db(self.home_dir) as conn:
            qb = QueryBuilder(conn)
            for row in qb.from_table("rollouts").fetch_all():
                status = str(row.get("status", ""))
                if status in status_counts:
                    status_counts[status] += 1

        tick_duration_ms = int((time.monotonic() - tick_started) * 1000)
        self._log_scheduler(
            "tick_summary",
            fields={
                "tick": tick_id,
                "cand": candidates_count,
                "admit": admitted_count,
                "spawn": spawn_success,
                "run": status_counts["running"],
                "pass": status_counts["passed"],
                "fail": status_counts["failed"],
                "retry": status_counts["retry_pending"],
                "exhst": status_counts["exhausted"],
                "lost": status_counts["lost"],
                "dur": f"{tick_duration_ms}ms",
            },
            detail=True,
        )

        return {
            "tick_id": tick_id,
            "poll_interval": poll_interval,
            "candidates": candidates_count,
            "admitted": admitted_count,
            "spawned": spawn_success,
            "status_counts": dict(status_counts),
            "duration_ms": tick_duration_ms,
        }

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_requested = False
        try:
            while not self._stop_requested:
                tick_result = self.tick()
                if self._stop_requested:
                    break
                poll_interval = float(tick_result.get("poll_interval", 5.0))
                time.sleep(max(0.1, poll_interval))
        finally:
            self._running = False
            self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def start_loop(self) -> None:
        self.start()

 

Scheduler = SparkScheduler


__all__ = ["SparkScheduler",]
