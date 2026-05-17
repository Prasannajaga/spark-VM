"""High-level one-shot SparkVM API."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from .api import FirecrackerAPIClient
from .config import DEFAULT_MEMORY, DEFAULT_RUNTIME, DEFAULT_TIMEOUT_SEC, DEFAULT_VCPU, SparkVMConfig, build_config
from .disk import ExecutionDisk
from .errors import (
    FirecrackerAPIError,
    FirecrackerBootError,
    FirecrackerProcessError,
    JobTimeoutError,
    RolloutError,
    RolloutNotFoundError,
)
from .image import ManagedImageResolver, RuntimeImage
from .process import FirecrackerProcess
from .result import VMResult
from .rollouts import Rollout, RolloutItem
from .runtimes.python import PYTHON_RUNTIME_ID
from .setup import ManagedSetup


def _estimate_execution_disk_size_mb(rollout_path: Path) -> int:
    total_bytes = 0
    for entry in rollout_path.rglob("*"):
        if entry.is_file():
            total_bytes += entry.stat().st_size
    total_mib = (total_bytes + (1024 * 1024 - 1)) // (1024 * 1024)
    # Headroom for result files + filesystem overhead.
    return max(64, int(total_mib) + 64)


class SparkVM:
    def __init__(
        self,
        *,
        vcpu: int = DEFAULT_VCPU,
        memory: int | str = DEFAULT_MEMORY,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        runtime: str = DEFAULT_RUNTIME,
        home_dir: str | Path | None = None,
        keep_firecracker_log_on_success: bool = False,
    ) -> None:
        self.config: SparkVMConfig = build_config(
            vcpu=vcpu,
            memory=memory,
            timeout=timeout,
            runtime=runtime,
            home_dir=home_dir,
        )
        self.keep_firecracker_log_on_success = bool(keep_firecracker_log_on_success)
        self._setup = ManagedSetup(self.config)
        self._images = ManagedImageResolver(self.config)
        self._rollouts = Rollout(home_dir=self.config.home_dir)

    def run(self, rollout: str | RolloutItem) -> VMResult:
        rollout_obj = self._resolve_rollout(rollout)
        self._validate_rollout_runtime(rollout_obj)

        self._setup.ensure_layout()
        firecracker_bin = self._setup.firecracker_binary_path()
        self._setup.assert_kvm_available()
        runtime_image = self._images.resolve(self.config.runtime)

        vm_id = f"vm-{uuid4().hex[:12]}"
        workdir = self.config.work_dir / vm_id
        workdir.mkdir(parents=True, exist_ok=False)

        socket_path = workdir / "firecracker.sock"
        firecracker_log_path = workdir / "firecracker.log"
        execution_disk_path = workdir / "rollout.ext4"
        mount_base = workdir / "mnt"

        execution_disk = ExecutionDisk(
            rollout=rollout_obj,
            path=execution_disk_path,
            size_mb=_estimate_execution_disk_size_mb(rollout_obj.path),
            mount_base=mount_base,
        )
        execution_disk.copy_rollout()

        firecracker = FirecrackerProcess(
            firecracker_bin=firecracker_bin,
            socket_path=socket_path,
            log_path=firecracker_log_path,
        )

        run_succeeded = False
        try:
            firecracker.start(startup_timeout_sec=min(5.0, self.config.timeout_sec))
            api = FirecrackerAPIClient(socket_path)
            self._wait_for_firecracker_socket(api, firecracker, timeout_sec=self.config.timeout_sec)

            self._configure_microvm(
                api=api,
                runtime_image=runtime_image,
                execution_disk_path=execution_disk_path,
            )

            started_at = time.monotonic()
            api.put("/actions", {"action_type": "InstanceStart"})

            try:
                firecracker.wait(timeout_sec=self.config.timeout_sec)
            except subprocess.TimeoutExpired as exc:
                raise JobTimeoutError(
                    f"Rollout '{rollout_obj.id}' timed out after {self.config.timeout_sec:.2f} seconds."
                ) from exc

            duration_ms = int((time.monotonic() - started_at) * 1000)
            result = execution_disk.read_result(
                vm_id=vm_id,
                duration_ms=duration_ms,
                firecracker_log_path=firecracker_log_path,
            )
            run_succeeded = True
            return result
        except (FirecrackerAPIError, FirecrackerProcessError) as exc:
            detail = self._format_boot_failure(
                vm_id=vm_id,
                workdir=workdir,
                socket_path=socket_path,
                firecracker_log_path=firecracker_log_path,
                reason=str(exc),
            )
            raise FirecrackerBootError(detail) from exc
        finally:
            try:
                firecracker.stop()
            finally:
                if run_succeeded and socket_path.exists():
                    try:
                        socket_path.unlink()
                    except OSError:
                        pass

            if run_succeeded:
                try:
                    execution_disk.cleanup(remove_disk=True)
                except Exception:
                    # Cleanup failures should not hide the original run outcome.
                    pass

                if not self.keep_firecracker_log_on_success:
                    shutil.rmtree(workdir, ignore_errors=True)

    def _resolve_rollout(self, rollout: str | RolloutItem) -> RolloutItem:
        if isinstance(rollout, RolloutItem):
            if not rollout.path.exists():
                raise RolloutNotFoundError(f"Rollout path does not exist: {rollout.path}")
            if not (rollout.path / "rollout.json").exists():
                raise RolloutNotFoundError(f"rollout.json missing for rollout: {rollout.id}")
            return rollout

        if isinstance(rollout, str):
            return self._rollouts.get_by_id(rollout)

        raise TypeError("SparkVM.run expects a rollout id (str) or an object returned by Rollout.create().")

    def _validate_rollout_runtime(self, rollout: RolloutItem) -> None:
        if rollout.runtime != PYTHON_RUNTIME_ID:
            raise RolloutError(
                f"Unsupported rollout runtime '{rollout.runtime}'. Only '{PYTHON_RUNTIME_ID}' is supported."
            )
        if self.config.runtime != rollout.runtime:
            raise RolloutError(
                f"Rollout runtime '{rollout.runtime}' does not match SparkVM runtime '{self.config.runtime}'."
            )

    def _wait_for_firecracker_socket(
        self,
        api: FirecrackerAPIClient,
        process: FirecrackerProcess,
        *,
        timeout_sec: float,
    ) -> None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                detail = self._format_firecracker_process_diagnostic(
                    process=process,
                    reason=f"Firecracker exited before API socket became ready (exit code {exit_code}).",
                )
                raise FirecrackerProcessError(detail)
            if api.socket_path.exists():
                try:
                    # /machine-config is a stable Firecracker API endpoint.
                    api.get("/machine-config")
                    return
                except FirecrackerAPIError:
                    # Socket can become available before HTTP endpoint is ready.
                    pass
            time.sleep(0.05)
        detail = self._format_firecracker_process_diagnostic(
            process=process,
            reason=f"Timed out waiting for Firecracker API socket after {timeout_sec:.2f}s.",
        )
        raise FirecrackerProcessError(detail)

    def _format_firecracker_process_diagnostic(self, *, process: FirecrackerProcess, reason: str) -> str:
        parts = [reason, f"Socket path: {process.socket_path}"]
        if process.log_path is not None:
            parts.append(f"Check Firecracker log: {process.log_path}")
            tail = self._read_log_tail(process.log_path)
            if tail:
                parts.append("Firecracker log tail:")
                parts.append(tail)
        return "\n".join(parts)

    def _format_boot_failure(
        self,
        *,
        vm_id: str,
        workdir: Path,
        socket_path: Path,
        firecracker_log_path: Path,
        reason: str,
    ) -> str:
        parts = [
            f"Firecracker boot failed for vm_id={vm_id}.",
            f"Workdir: {workdir}",
            f"Socket path: {socket_path}",
            f"Check Firecracker log: {firecracker_log_path}",
            f"Reason: {reason}",
        ]
        tail = self._read_log_tail(firecracker_log_path)
        if tail:
            parts.append("Firecracker log tail:")
            parts.append(tail)
        return "\n".join(parts)

    def _read_log_tail(self, log_path: Path, *, max_lines: int = 40) -> str:
        if not log_path.exists():
            return ""
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-max_lines:])

    def _configure_microvm(
        self,
        *,
        api: FirecrackerAPIClient,
        runtime_image: RuntimeImage,
        execution_disk_path: Path,
    ) -> None:
        api.put(
            "/boot-source",
            {
                "kernel_image_path": str(runtime_image.kernel_image),
                "boot_args": str(runtime_image.boot_args),
            },
        )
        api.put(
            "/machine-config",
            {
                "vcpu_count": self.config.vcpu,
                "mem_size_mib": self.config.memory_mib,
                "smt": False,
                "track_dirty_pages": False,
            },
        )
        api.put(
            "/drives/rootfs",
            {
                "drive_id": "rootfs",
                "path_on_host": str(runtime_image.rootfs_image),
                "is_root_device": True,
                "is_read_only": False,
            },
        )
        api.put(
            "/drives/job",
            {
                "drive_id": "job",
                "path_on_host": str(execution_disk_path),
                "is_root_device": False,
                "is_read_only": False,
            },
        )


__all__ = ["SparkVM"]
