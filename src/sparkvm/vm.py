"""High-level one-shot SparkVM API."""

from __future__ import annotations

from dataclasses import replace
import json
import os
import pwd
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .firecracker.api import FirecrackerAPIClient
from .config import DEFAULT_MEMORY, DEFAULT_TIMEOUT_SEC, DEFAULT_VCPU, SparkVMConfig, build_config
from .disk import ExecutionDisk
from .errors import (
    CleanupError,
    FirecrackerAPIError,
    FirecrackerBootError,
    FirecrackerProcessError,
    SparkVMError,
    RuntimeImagePermissionError,
    RolloutNotFoundError,
)
from .fsops import ensure_dir, read_text, remove_file, remove_tree, write_text
from .image import ManagedImageResolver, RuntimeImage
from .firecracker.process import FirecrackerProcess
from .result import VMResult
from .rollouts import Rollout, Rollouts
from cli.setup import ManagedSetup


class SparkVM:
    def __init__(
        self,
        *,
        vcpu: int = DEFAULT_VCPU,
        memory: int | str = DEFAULT_MEMORY,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        runtime: str | None = None,
        base_image: str | None = None,
        home_dir: str | Path | None = None,
    ) -> None:
        explicit_runtime = runtime
        if explicit_runtime is None:
            explicit_runtime = base_image
        self._runtime_override = explicit_runtime is not None

        self.config: SparkVMConfig = build_config(
            vcpu=vcpu,
            memory=memory,
            timeout=timeout,
            runtime=explicit_runtime,
            base_image=base_image,
            home_dir=home_dir,
        )
        self._setup = ManagedSetup(self.config)
        self._images = ManagedImageResolver(self.config)
        self._rollouts = Rollouts(home_dir=self.config.home_dir)

    def run(self, rollout: str | Rollout) -> VMResult:
        rollout_obj = self._resolve_rollout(rollout)
        selected_runtime = self.config.runtime if self._runtime_override else rollout_obj.runtime

        self._setup.ensure_layout()

        vm_id = f"vm-{uuid4().hex[:12]}"
        worker_dir = self.config.workers_dir / vm_id
        ensure_dir(worker_dir, exist_ok=False)

        socket_path = worker_dir / "firecracker.sock"
        firecracker_log_path = worker_dir / "firecracker.log"
        execution_disk_path = worker_dir / "rollout.ext4"
        mount_base = worker_dir / "mnt"

        execution_disk = ExecutionDisk(
            rollout=rollout_obj,
            path=execution_disk_path,
            size_mb=rollout_obj.disk_mb,
            mount_base=mount_base,
        )
        started_at = time.monotonic()
        firecracker: FirecrackerProcess | None = None

        try:
            firecracker_bin = self._setup.firecracker_binary_path()
            self._setup.assert_kvm_available()
            runtime_image = self._images.resolve(selected_runtime)
            self._assert_runtime_image_permissions(runtime_image)

            execution_disk.copy_rollout()
            firecracker = FirecrackerProcess(
                firecracker_bin=firecracker_bin,
                socket_path=socket_path,
                log_path=firecracker_log_path,
            )
            firecracker.start(startup_timeout_sec=min(5.0, self.config.timeout_sec))

            api = FirecrackerAPIClient(socket_path)
            self._wait_for_firecracker_socket(api, firecracker, timeout_sec=self.config.timeout_sec)
            self._configure_microvm(
                api=api,
                runtime_image=runtime_image,
                execution_disk_path=execution_disk_path,
            )
            api.put("/actions", {"action_type": "InstanceStart"})

            try:
                firecracker.wait(timeout_sec=self.config.timeout_sec)
            except subprocess.TimeoutExpired:
                duration_ms = int((time.monotonic() - started_at) * 1000)
                timeout_result = VMResult(
                    rollout_id=rollout_obj.id,
                    rollout_name=rollout_obj.name,
                    rollout_mode=rollout_obj.mode,
                    base_image=selected_runtime,
                    vm_id=vm_id,
                    status="timeout",
                    exit_code=124,
                    duration_ms=duration_ms,
                    timed_out=True,
                    firecracker_log_path=firecracker_log_path,
                    execution_disk_path=execution_disk_path,
                )
                self._cleanup_worker_on_completion(
                    worker_dir=worker_dir,
                    socket_path=socket_path,
                    firecracker=firecracker,
                    execution_disk=execution_disk,
                )
                return timeout_result

            duration_ms = int((time.monotonic() - started_at) * 1000)
            result = execution_disk.read_result(
                vm_id=vm_id,
                duration_ms=duration_ms,
                firecracker_log_path=firecracker_log_path,
            )
            result = self._annotate_oom(result=result, firecracker_log_path=firecracker_log_path)

            self._cleanup_worker_on_completion(
                worker_dir=worker_dir,
                socket_path=socket_path,
                firecracker=firecracker,
                execution_disk=execution_disk,
            )
            return result
        except (FirecrackerAPIError, FirecrackerProcessError) as exc:
            wrapped = FirecrackerBootError(
                self._format_boot_failure(
                    vm_id=vm_id,
                    worker_dir=worker_dir,
                    socket_path=socket_path,
                    firecracker_log_path=firecracker_log_path,
                    reason=str(exc),
                )
            )
            self._write_failure_record(
                worker_dir=worker_dir,
                vm_id=vm_id,
                rollout=rollout_obj,
                runtime=selected_runtime,
                error=wrapped,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=execution_disk_path,
            )
            if firecracker is not None:
                firecracker.stop()
            raise wrapped from exc
        except SparkVMError as exc:
            self._write_failure_record(
                worker_dir=worker_dir,
                vm_id=vm_id,
                rollout=rollout_obj,
                runtime=selected_runtime,
                error=exc,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=execution_disk_path,
            )
            if firecracker is not None:
                firecracker.stop()
            raise
        except Exception as exc:
            wrapped = FirecrackerBootError(str(exc))
            self._write_failure_record(
                worker_dir=worker_dir,
                vm_id=vm_id,
                rollout=rollout_obj,
                runtime=selected_runtime,
                error=wrapped,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=execution_disk_path,
            )
            if firecracker is not None:
                firecracker.stop()
            raise wrapped from exc

    def _resolve_rollout(self, rollout: str | Rollout) -> Rollout:
        if isinstance(rollout, Rollout):
            if not rollout.path.exists():
                raise RolloutNotFoundError(f"Rollout path does not exist: {rollout.path}")
            if not (rollout.path / "rollout.json").exists():
                raise RolloutNotFoundError(f"rollout.json missing for rollout: {rollout.id}")
            return rollout

        if isinstance(rollout, str):
            return self._rollouts.get_by_id(rollout)

        raise TypeError("SparkVM.run expects a rollout id (str) or an object returned by Rollouts.create().")

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
                    api.get("/machine-config")
                    return
                except FirecrackerAPIError:
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
        worker_dir: Path,
        socket_path: Path,
        firecracker_log_path: Path,
        reason: str,
    ) -> str:
        parts = [
            f"Firecracker boot failed for vm_id={vm_id}.",
            f"Worker dir: {worker_dir}",
            f"Socket path: {socket_path}",
            f"Check Firecracker log: {firecracker_log_path}",
            f"Reason: {reason}",
        ]
        tail = self._read_log_tail(firecracker_log_path)
        if tail:
            parts.append("Firecracker log tail:")
            parts.append(tail)
        return "\n".join(parts)

    def _cleanup_worker_on_completion(
        self,
        *,
        worker_dir: Path,
        socket_path: Path,
        firecracker: FirecrackerProcess | None,
        execution_disk: ExecutionDisk,
    ) -> None:
        errors: list[Exception] = []

        if firecracker is not None:
            try:
                firecracker.stop()
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(exc)

        if socket_path.exists():
            try:
                remove_file(socket_path, missing_ok=True)
            except OSError as exc:
                errors.append(exc)

        try:
            execution_disk.cleanup(remove_disk=True)
        except Exception as exc:
            errors.append(exc)

        try:
            remove_tree(worker_dir, ignore_errors=False)
        except OSError as exc:
            errors.append(exc)

        if errors:
            raise CleanupError(f"Worker cleanup failed for {worker_dir}: {errors[0]}")

    def _write_failure_record(
        self,
        *,
        worker_dir: Path,
        vm_id: str,
        rollout: Rollout,
        runtime: str,
        error: Exception,
        duration_ms: int,
        firecracker_log_path: Path,
        execution_disk_path: Path,
    ) -> None:
        payload = {
            "vm_id": vm_id,
            "rollout_id": rollout.id,
            "rollout_name": rollout.name,
            "rollout_mode": rollout.mode,
            "runtime": runtime,
            "status": "infrastructure_failed",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "duration_ms": duration_ms,
            "firecracker_log_path": str(firecracker_log_path),
            "execution_disk_path": str(execution_disk_path),
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }

        failure_path = worker_dir / "failure.json"
        try:
            write_text(failure_path, json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _read_log_tail(self, log_path: Path, *, max_lines: int = 40) -> str:
        if not log_path.exists():
            return ""
        try:
            lines = read_text(log_path, encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-max_lines:])

    def _annotate_oom(self, *, result: VMResult, firecracker_log_path: Path) -> VMResult:
        if result.oom_killed:
            return result
        if result.timed_out:
            return result
        if result.exit_code == 0:
            return result

        log_tail = self._read_log_tail(firecracker_log_path, max_lines=200).lower()
        indicators = ("out of memory", "oom-kill", "oom killed", "killed process")
        if any(marker in log_tail for marker in indicators):
            return replace(result, status="oom", oom_killed=True)
        if result.run is not None and result.run.exit_code == 137:
            return replace(result, status="oom", oom_killed=True)
        return result

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

    def _assert_runtime_image_permissions(self, runtime_image: RuntimeImage) -> None:
        rootfs = runtime_image.rootfs_image
        if not rootfs.exists():
            return
        if not os.access(rootfs, os.R_OK | os.W_OK):
            raise RuntimeImagePermissionError(
                "Runtime image exists but is not readable/writable by the current user: "
                f"{rootfs}\n"
                "Fix ownership/permissions or recreate with ownership set:\n"
                f"  sudo chown {pwd.getpwuid(os.getuid()).pw_name}:{pwd.getpwuid(os.getuid()).pw_name} {rootfs}\n"
                f"  sparkvm dockify {runtime_image.name.replace('-', ':', 1)} --name {runtime_image.name} --force"
            )


__all__ = ["SparkVM"]
