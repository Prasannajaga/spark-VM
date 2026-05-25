"""Machine-wide scheduler policy API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.config import resolve_home_dir
from ..core.utils import sanitize_machine_policy
from ..storage.repositories import MachinePolicyRepository


class MachineConfig:
    @staticmethod
    def get_policy(home_dir: str | Path | None = None) -> dict[str, Any]:
        resolved_home = resolve_home_dir(home_dir)
        repo = MachinePolicyRepository(resolved_home)
        payload = repo.get()
        if not isinstance(payload, dict):
            payload = {}
        policy = sanitize_machine_policy(payload)
        if policy != payload:
            repo.set(policy)
        return policy

    @staticmethod
    def set_policy(home_dir: str | Path | None = None, **policy_patch: Any) -> dict[str, Any]:
        current = MachineConfig.get_policy(home_dir=home_dir)
        current.update(policy_patch)
        policy = sanitize_machine_policy(current)
        MachinePolicyRepository(resolve_home_dir(home_dir)).set(policy)
        return policy


__all__ = ["MachineConfig"]
