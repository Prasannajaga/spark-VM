"""Machine-wide scheduler policy helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import resolve_home_dir
from .state_store import atomic_write_json, read_json

DEFAULT_POLICY: dict[str, Any] = {
    "host_reserved_memory": "2G",
    "host_reserved_disk": "20G",
    "max_memory_percent": 80,
    "max_disk_percent": 80,
    "max_concurrent_vms": 4,
    "vm_memory_overhead": "256M",
    "vm_disk_overhead": "2G",
    "poll_interval": 5.0,
    "cooldown_after_vm": 5.0,
}


def parse_size_to_bytes(value: int | str) -> int:
    if isinstance(value, bool):
        raise ValueError("size must be int or size string")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("size must be >= 0")
        return value
    if not isinstance(value, str):
        raise ValueError("size must be int or size string")

    raw = value.strip().upper()
    if not raw:
        raise ValueError("size cannot be empty")

    if raw.endswith("M"):
        amount = int(raw[:-1])
        return amount * 1024 * 1024
    if raw.endswith("G"):
        amount = int(raw[:-1])
        return amount * 1024 * 1024 * 1024

    return int(raw)


def _policy_path(home_dir: str | Path | None = None) -> Path:
    return resolve_home_dir(home_dir) / "machine-policy.json"


def _sanitize_policy(payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_POLICY)
    merged.update({k: v for k, v in payload.items() if k in DEFAULT_POLICY})

    merged["host_reserved_memory"] = str(merged["host_reserved_memory"])
    merged["host_reserved_disk"] = str(merged["host_reserved_disk"])
    merged["vm_memory_overhead"] = str(merged["vm_memory_overhead"])
    merged["vm_disk_overhead"] = str(merged["vm_disk_overhead"])
    merged["max_memory_percent"] = int(merged["max_memory_percent"])
    merged["max_disk_percent"] = int(merged["max_disk_percent"])
    merged["max_concurrent_vms"] = int(merged["max_concurrent_vms"])
    merged["poll_interval"] = float(merged["poll_interval"])
    merged["cooldown_after_vm"] = float(merged["cooldown_after_vm"])

    return merged


@dataclass
class MachineConfig:
    home_dir: Path | None = None

    def get_policy(self) -> dict[str, Any]:
        path = _policy_path(self.home_dir)
        if not path.exists():
            policy = _sanitize_policy({})
            atomic_write_json(path, policy)
            return policy

        try:
            payload = read_json(path)
        except Exception:
            payload = {}

        if not isinstance(payload, dict):
            payload = {}

        policy = _sanitize_policy(payload)
        if policy != payload:
            atomic_write_json(path, policy)
        return policy

    def set_policy(self, **policy_patch: Any) -> dict[str, Any]:
        current = self.get_policy()
        current.update(policy_patch)
        policy = _sanitize_policy(current)
        atomic_write_json(_policy_path(self.home_dir), policy)
        return policy


__all__ = ["MachineConfig", "DEFAULT_POLICY", "parse_size_to_bytes"]
