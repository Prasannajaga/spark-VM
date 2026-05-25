"""Background rollout scheduler."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .admission import AdmissionController
from .config import resolve_home_dir
from .machine_config import MachineConfig
from .reservations import attach_pid, load_reservations, mark_lost, release, reserve, save_reservations
from .state_store import get_rollout, load_rollouts_metadata, save_rollouts_metadata, scheduler_lock
from .utils import now_utc_iso
from .workers import Workers

DEFAULT_VM_CONFIG = {
    "vcpu": 2,
    "memory": "2G",
    "disk": "4G",
    "timeout": 60.0,
    "network": True,
    "env": {},
}


class Scheduler:
    def __init__(self, *, home_dir: str | Path | None = None) -> None:
        self.home_dir = resolve_home_dir(home_dir)
        self.workers = Workers(home_dir=self.home_dir)

    def _pid_alive(self, pid: int | None) -> bool:
        if pid is None or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _resolve_vm_config(self, rollout_id: str) -> dict[str, Any] | None:
        try:
            rollout = get_rollout(rollout_id, self.home_dir)
        except Exception:
            return None

        vm_config = rollout.get("vm_config")
        if not isinstance(vm_config, dict):
            vm_config = dict(DEFAULT_VM_CONFIG)

        resolved = dict(DEFAULT_VM_CONFIG)
        resolved.update(vm_config)
        resolved["env"] = dict(resolved.get("env", {}))
        return resolved

    def _fail_rollout(self, entry: dict[str, Any], *, worker_id: str | None) -> dict[str, Any]:
        retry_count = int(entry.get("retry_count", 0)) + 1
        max_retries = int(entry.get("max_retries", 3))
        entry["retry_count"] = retry_count
        entry["status"] = "retry_pending" if retry_count < max_retries else "exhausted"
        entry["active_worker_id"] = None
        entry["last_worker_id"] = worker_id
        entry["completed_at"] = now_utc_iso()
        entry["updated_at"] = now_utc_iso()
        return entry

    def _pass_rollout(self, entry: dict[str, Any], *, worker_id: str | None) -> dict[str, Any]:
        entry["status"] = "passed"
        entry["active_worker_id"] = None
        entry["last_worker_id"] = worker_id
        entry["completed_at"] = now_utc_iso()
        entry["updated_at"] = now_utc_iso()
        return entry

    def _reconcile_unlocked(self) -> None:
        metadata = load_rollouts_metadata(self.home_dir)
        rollouts = metadata.get("rollouts", {})
        if not isinstance(rollouts, dict):
            return

        reservations_payload = load_reservations(self.home_dir)
        reservations = reservations_payload.get("reservations", {})
        if not isinstance(reservations, dict):
            reservations = {}
            reservations_payload["reservations"] = reservations

        worker_items = {worker["id"]: worker for worker in self.workers.list_workers() if isinstance(worker, dict)}

        # reservation exists but worker metadata is gone
        for reservation_id, reservation in list(reservations.items()):
            if not isinstance(reservation, dict):
                continue
            if reservation.get("status") not in {"reserved", "starting", "running"}:
                continue
            worker_id = reservation.get("worker_id")
            if not isinstance(worker_id, str) or worker_id in worker_items:
                continue
            mark_lost(reservation_id, home_dir=self.home_dir)

        # worker is marked active but process pid is dead
        for worker_id, worker in list(worker_items.items()):
            status = worker.get("status")
            if status not in {"starting", "running"}:
                continue
            pid = worker.get("pid")
            pid_int = int(pid) if isinstance(pid, int) else None
            if self._pid_alive(pid_int):
                continue

            self.workers.mark_worker_status(worker_id, "lost")
            reservation_id = worker.get("reservation_id")
            if isinstance(reservation_id, str):
                mark_lost(reservation_id, home_dir=self.home_dir)

            rollout_id = worker.get("rollout_id")
            if isinstance(rollout_id, str):
                entry = rollouts.get(rollout_id)
                if isinstance(entry, dict) and entry.get("status") in {"running", "retrying"}:
                    rollouts[rollout_id] = self._fail_rollout(entry, worker_id=worker_id)

        # rollout-level consistency from worker terminal states
        for rollout_id, entry in list(rollouts.items()):
            if not isinstance(entry, dict):
                continue
            if entry.get("status") not in {"running", "retrying"}:
                continue

            active_worker_id = entry.get("active_worker_id")
            if not isinstance(active_worker_id, str):
                rollouts[rollout_id] = self._fail_rollout(entry, worker_id=None)
                continue

            worker = worker_items.get(active_worker_id)
            if not isinstance(worker, dict):
                rollouts[rollout_id] = self._fail_rollout(entry, worker_id=active_worker_id)
                continue

            worker_status = worker.get("status")
            reservation_id = worker.get("reservation_id")
            if worker_status == "passed":
                rollouts[rollout_id] = self._pass_rollout(entry, worker_id=active_worker_id)
                if isinstance(reservation_id, str):
                    release(reservation_id, home_dir=self.home_dir)
                continue

            if worker_status in {"failed", "timeout", "lost"}:
                rollouts[rollout_id] = self._fail_rollout(entry, worker_id=active_worker_id)
                if isinstance(reservation_id, str):
                    release(reservation_id, home_dir=self.home_dir)
                continue

        save_rollouts_metadata(metadata, self.home_dir)

    def _candidate_rank(self, entry: dict[str, Any]) -> tuple[int, int, str]:
        status = str(entry.get("status", "scheduled"))
        if status == "scheduled":
            status_rank = 0
        elif status == "retry_pending":
            status_rank = 1
        else:
            status_rank = 9

        priority = int(entry.get("priority", 100))
        created_at = str(entry.get("created_at", ""))
        return (status_rank, -priority, created_at)

    def _build_candidates(self, rollouts: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for rollout_id, entry in rollouts.items():
            if not isinstance(entry, dict):
                continue

            retry_count = int(entry.get("retry_count", 0))
            max_retries = int(entry.get("max_retries", 3))
            status = str(entry.get("status", "scheduled"))

            if status == "failed" and retry_count < max_retries:
                entry["status"] = "retry_pending"
                status = "retry_pending"

            if status == "scheduled":
                candidates.append(entry)
                continue
            if status == "retry_pending" and retry_count < max_retries:
                candidates.append(entry)
                continue

        candidates.sort(key=self._candidate_rank)
        return candidates

    def _spawn_worker_process(self, worker_id: str) -> int | None:
        cmd = [
            sys.executable,
            "-m",
            "cli.main",
            "--home-dir",
            str(self.home_dir),
            "__worker-run",
            worker_id,
        ]
        try:
            proc = subprocess.Popen(cmd, start_new_session=True)
            return int(proc.pid)
        except Exception:
            return None

    def start_loop(self) -> None:
        while True:
            policy = MachineConfig(self.home_dir).get_policy()
            poll_interval = float(policy.get("poll_interval", 5.0))
            cooldown_after_vm = float(policy.get("cooldown_after_vm", 5.0))

            to_spawn: list[dict[str, Any]] = []

            with scheduler_lock(self.home_dir):
                self._reconcile_unlocked()

                metadata = load_rollouts_metadata(self.home_dir)
                rollouts = metadata.get("rollouts", {})
                if not isinstance(rollouts, dict):
                    rollouts = {}
                    metadata["rollouts"] = rollouts

                candidates = self._build_candidates(rollouts)
                admission = AdmissionController(home_dir=self.home_dir)

                for entry in candidates:
                    rollout_id = entry.get("id")
                    if not isinstance(rollout_id, str):
                        continue

                    vm_config = self._resolve_vm_config(rollout_id)
                    if vm_config is None:
                        continue

                    live_reservations = [
                        item
                        for item in load_reservations(self.home_dir).get("reservations", {}).values()
                        if isinstance(item, dict)
                    ]
                    decision = admission.check(vm_config, live_reservations)
                    if not bool(decision.get("allowed")):
                        continue

                    worker_id = f"worker-{uuid4().hex[:12]}"
                    attempt = int(entry.get("retry_count", 0)) + 1
                    retry_of = entry.get("last_worker_id") if entry.get("status") == "retry_pending" else None

                    reservation = reserve(rollout_id, worker_id, vm_config, home_dir=self.home_dir)
                    self.workers.create_worker(
                        worker_id=worker_id,
                        rollout_id=rollout_id,
                        reservation_id=str(reservation["id"]),
                        attempt=attempt,
                        retry_of=str(retry_of) if isinstance(retry_of, str) else None,
                        vm_config=vm_config,
                        status="reserved",
                    )

                    entry["active_worker_id"] = worker_id
                    entry["last_worker_id"] = worker_id
                    entry["status"] = "running" if str(entry.get("status")) == "scheduled" else "retrying"
                    entry["updated_at"] = now_utc_iso()
                    rollouts[rollout_id] = entry

                    to_spawn.append({"worker_id": worker_id, "reservation_id": reservation["id"]})

                save_rollouts_metadata(metadata, self.home_dir)

            if to_spawn:
                for item in to_spawn:
                    worker_id = str(item["worker_id"])
                    reservation_id = str(item["reservation_id"])
                    pid = self._spawn_worker_process(worker_id)
                    with scheduler_lock(self.home_dir):
                        if pid is None:
                            self.workers.mark_worker_status(worker_id, "lost")
                            mark_lost(reservation_id, home_dir=self.home_dir)
                            metadata = load_rollouts_metadata(self.home_dir)
                            rollouts = metadata.get("rollouts", {})
                            worker_payload = self.workers.load_worker(worker_id)
                            rollout_id = str(worker_payload.get("rollout_id"))
                            entry = rollouts.get(rollout_id) if isinstance(rollouts, dict) else None
                            if isinstance(entry, dict):
                                rollouts[rollout_id] = self._fail_rollout(entry, worker_id=worker_id)
                                save_rollouts_metadata(metadata, self.home_dir)
                            continue

                        self.workers.mark_worker_status(worker_id, "starting", pid=pid)
                        attach_pid(reservation_id, pid, home_dir=self.home_dir)
                        reservations_payload = load_reservations(self.home_dir)
                        reservations = reservations_payload.get("reservations", {})
                        if isinstance(reservations, dict):
                            reservation = reservations.get(reservation_id)
                            if isinstance(reservation, dict):
                                reservation["status"] = "starting"
                                reservation["updated_at"] = now_utc_iso()
                                reservations[reservation_id] = reservation
                                save_reservations(reservations_payload, self.home_dir)

                if cooldown_after_vm > 0:
                    time.sleep(cooldown_after_vm)

            time.sleep(max(0.1, poll_interval))


__all__ = ["Scheduler"]
