"""Result model for SparkVM rollout execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PhaseResult:
    name: str
    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True)
class VMResult:
    rollout_id: str
    rollout_name: str
    rollout_mode: str
    runtime: str
    vm_id: str
    status: str
    exit_code: int
    duration_ms: int
    network: PhaseResult | None = None
    setup: PhaseResult | None = None
    run: PhaseResult | None = None
    timed_out: bool = False
    oom_killed: bool = False
    worker_path: Path | None = None
    firecracker_log_path: Path | None = None
    execution_disk_path: Path | None = None

    @property
    def base_image(self) -> str:
        return self.runtime

    @property
    def stdout(self) -> str:
        if self.run is None:
            return ""
        return self.run.stdout

    @property
    def stderr(self) -> str:
        if self.run is None:
            return ""
        return self.run.stderr

    @property
    def passed(self) -> bool:
        return self.status == "passed" and self.exit_code == 0 and not self.timed_out and not self.oom_killed


__all__ = ["PhaseResult", "VMResult"]
