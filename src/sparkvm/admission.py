"""Admission checks for scheduler worker launches."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .config import resolve_home_dir
from .machine_config import MachineConfig, parse_size_to_bytes


def _read_total_memory_bytes() -> int:
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        try:
            for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
        except Exception:
            pass

    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages) * int(page_size)
    except Exception:
        return 0


def _read_available_memory_bytes() -> int:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return 0
    try:
        for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
    except Exception:
        return 0
    return 0


def _sum_reserved_bytes(reservations: list[dict[str, Any]]) -> tuple[int, int, int]:
    reserved_memory = 0
    reserved_disk = 0
    active_count = 0
    for item in reservations:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status not in {"reserved", "starting", "running"}:
            continue
        active_count += 1
        try:
            reserved_memory += int(item.get("memory_bytes", 0))
        except (TypeError, ValueError):
            pass
        try:
            reserved_disk += int(item.get("disk_bytes", 0))
        except (TypeError, ValueError):
            pass
    return reserved_memory, reserved_disk, active_count


class AdmissionController:
    def __init__(self, *, home_dir: str | Path | None = None) -> None:
        self.home_dir = resolve_home_dir(home_dir)
        self.policy = MachineConfig(self.home_dir).get_policy()

    def check(self, vm_config: dict[str, Any], reservations: list[dict[str, Any]]) -> dict[str, Any]:
        total_memory = _read_total_memory_bytes()
        total_disk = shutil.disk_usage(self.home_dir).total
        free_disk = shutil.disk_usage(self.home_dir).free
        available_memory_live = _read_available_memory_bytes()

        host_reserved_memory = parse_size_to_bytes(str(self.policy["host_reserved_memory"]))
        host_reserved_disk = parse_size_to_bytes(str(self.policy["host_reserved_disk"]))

        usable_memory = min(
            max(0, total_memory - host_reserved_memory),
            int(total_memory * (float(self.policy["max_memory_percent"]) / 100.0)),
        )
        usable_disk = min(
            max(0, total_disk - host_reserved_disk),
            int(total_disk * (float(self.policy["max_disk_percent"]) / 100.0)),
        )

        requested_memory = parse_size_to_bytes(str(vm_config.get("memory", "2G"))) + parse_size_to_bytes(
            str(self.policy["vm_memory_overhead"])
        )
        requested_disk = parse_size_to_bytes(str(vm_config.get("disk", "4G"))) + parse_size_to_bytes(
            str(self.policy["vm_disk_overhead"])
        )

        reserved_memory, reserved_disk, active_vm_count = _sum_reserved_bytes(reservations)

        available_memory_budget = max(0, usable_memory - reserved_memory)
        available_memory = min(available_memory_budget, max(0, available_memory_live)) if available_memory_live > 0 else available_memory_budget

        available_disk_budget = max(0, usable_disk - reserved_disk)
        available_disk = min(available_disk_budget, max(0, free_disk))

        allowed = (
            requested_memory <= available_memory
            and requested_disk <= available_disk
            and active_vm_count < int(self.policy["max_concurrent_vms"])
        )

        reason: str | None = None
        if not allowed:
            if active_vm_count >= int(self.policy["max_concurrent_vms"]):
                reason = "max_concurrent_vms reached"
            elif requested_memory > available_memory:
                reason = "insufficient_memory"
            elif requested_disk > available_disk:
                reason = "insufficient_disk"
            else:
                reason = "rejected"

        return {
            "allowed": allowed,
            "reason": reason,
            "requested_memory_bytes": requested_memory,
            "requested_disk_bytes": requested_disk,
            "available_memory_bytes": available_memory,
            "available_disk_bytes": available_disk,
        }


__all__ = ["AdmissionController"]
