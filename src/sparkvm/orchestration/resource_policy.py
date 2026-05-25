"""Global resource policy helpers for SparkVM."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import InvalidResourceError

DEFAULT_RESOURCE_POLICY = {
    "max_vm_cpu_percent": 80,
    "max_vm_memory_percent": 80,
    "max_vm_disk_percent": 80,
    "min_host_cpu_percent": 20,
    "min_host_memory_percent": 20,
    "min_host_disk_percent": 20,
}


@dataclass(frozen=True)
class CapacityDecision:
    allowed: bool
    reason: str


def _clamp_percent(value: Any, *, fallback: int) -> float:
    if isinstance(value, bool):
        return float(fallback)
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            numeric = float(value.strip())
        except ValueError:
            return float(fallback)
    else:
        return float(fallback)
    return max(0.0, min(100.0, numeric))


def load_resource_policy(home_dir: Path) -> dict[str, float]:
    policy = dict(DEFAULT_RESOURCE_POLICY)
    config_path = home_dir / "config.json"
    if not config_path.exists():
        return {key: float(value) for key, value in policy.items()}

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {key: float(value) for key, value in policy.items()}

    if not isinstance(payload, dict):
        return {key: float(value) for key, value in policy.items()}

    policy_raw = payload.get("resource_policy")
    if not isinstance(policy_raw, dict):
        return {key: float(value) for key, value in policy.items()}

    for key, fallback in DEFAULT_RESOURCE_POLICY.items():
        policy[key] = _clamp_percent(policy_raw.get(key), fallback=fallback)
    return {key: float(value) for key, value in policy.items()}


def _read_meminfo_mib() -> tuple[float, float]:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return (0.0, 0.0)

    total_kib: float | None = None
    avail_kib: float | None = None
    try:
        lines = meminfo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return (0.0, 0.0)

    for line in lines:
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2:
                total_kib = float(parts[1])
        elif line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2:
                avail_kib = float(parts[1])

    if total_kib is None or total_kib <= 0:
        return (0.0, 0.0)
    if avail_kib is None:
        avail_kib = 0.0
    return (total_kib / 1024.0, avail_kib / 1024.0)


def check_resource_capacity(
    *,
    home_dir: Path,
    vcpu: int,
    memory_mib: int,
    disk_mib: int,
) -> CapacityDecision:
    policy = load_resource_policy(home_dir)

    cpu_count = os.cpu_count() or 1
    requested_cpu_percent = (max(1, int(vcpu)) / float(cpu_count)) * 100.0
    try:
        load1, _load5, _load15 = os.getloadavg()
        used_cpu_percent = min(100.0, (load1 / float(cpu_count)) * 100.0)
    except OSError:
        used_cpu_percent = 0.0
    free_cpu_percent = max(0.0, 100.0 - used_cpu_percent)

    total_mem_mib, avail_mem_mib = _read_meminfo_mib()
    used_mem_percent = 0.0
    free_mem_percent = 100.0
    requested_mem_percent = 0.0
    if total_mem_mib > 0:
        used_mem_percent = max(0.0, min(100.0, ((total_mem_mib - avail_mem_mib) / total_mem_mib) * 100.0))
        free_mem_percent = max(0.0, 100.0 - used_mem_percent)
        requested_mem_percent = (max(1, int(memory_mib)) / total_mem_mib) * 100.0

    disk = shutil.disk_usage(home_dir)
    total_disk_mib = disk.total / (1024.0 * 1024.0)
    free_disk_mib = disk.free / (1024.0 * 1024.0)
    used_disk_percent = 0.0
    free_disk_percent = 100.0
    requested_disk_percent = 0.0
    if total_disk_mib > 0:
        used_disk_percent = max(0.0, min(100.0, ((total_disk_mib - free_disk_mib) / total_disk_mib) * 100.0))
        free_disk_percent = max(0.0, 100.0 - used_disk_percent)
        requested_disk_percent = (max(1, int(disk_mib)) / total_disk_mib) * 100.0

    cpu_headroom = free_cpu_percent - policy["min_host_cpu_percent"]
    if cpu_headroom < 0:
        return CapacityDecision(False, "host CPU free percent is below min_host_cpu_percent")
    if requested_cpu_percent > policy["max_vm_cpu_percent"] or requested_cpu_percent > cpu_headroom:
        return CapacityDecision(False, "requested VM CPU exceeds policy limits")

    mem_headroom = free_mem_percent - policy["min_host_memory_percent"]
    if mem_headroom < 0:
        return CapacityDecision(False, "host memory free percent is below min_host_memory_percent")
    if requested_mem_percent > policy["max_vm_memory_percent"] or requested_mem_percent > mem_headroom:
        return CapacityDecision(False, "requested VM memory exceeds policy limits")

    disk_headroom = free_disk_percent - policy["min_host_disk_percent"]
    if disk_headroom < 0:
        return CapacityDecision(False, "host disk free percent is below min_host_disk_percent")
    if requested_disk_percent > policy["max_vm_disk_percent"] or requested_disk_percent > disk_headroom:
        return CapacityDecision(False, "requested VM disk exceeds policy limits")

    return CapacityDecision(True, "capacity available")


def assert_resource_capacity(*, home_dir: Path, vcpu: int, memory_mib: int, disk_mib: int) -> None:
    decision = check_resource_capacity(home_dir=home_dir, vcpu=vcpu, memory_mib=memory_mib, disk_mib=disk_mib)
    if not decision.allowed:
        raise InvalidResourceError(f"Global resource policy check failed: {decision.reason}")
