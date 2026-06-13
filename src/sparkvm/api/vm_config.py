"""Public VM execution configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class VMConfig:
    vcpu: int = 2
    memory: int | str = "2G"
    disk: int | str = "4G"
    timeout: float = 60.0
    network: bool = True
    secure: bool = True
    env: Mapping[str, str] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "vcpu": self.vcpu,
            "memory": self.memory,
            "disk": self.disk,
            "timeout": self.timeout,
            "network": self.network,
            "secure": self.secure,
            "env": dict(self.env or {}),
        }


__all__ = ["VMConfig"]
