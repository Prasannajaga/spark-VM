"""Result model for SparkVM rollout execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VMResult:
    rollout_id: str
    rollout_name: str
    vm_id: str
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool = False
    oom_killed: bool = False
    firecracker_log_path: Path | None = None
    execution_disk_path: Path | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.oom_killed


__all__ = ["VMResult"]
