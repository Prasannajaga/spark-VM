"""High-level one-shot SparkVM API."""

from __future__ import annotations

from dataclasses import replace
import json
import os
import pwd
import re
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from .firecracker.api import FirecrackerAPIClient
from .config import DEFAULT_MEMORY, DEFAULT_TIMEOUT_SEC, DEFAULT_VCPU, SparkVMConfig, build_config
from .disk import ExecutionDisk, scrub_files_from_ext4_image
from .errors import (
    CleanupError,
    FirecrackerAPIError,
    FirecrackerBootError,
    FirecrackerProcessError,
    RuntimeImagePermissionError,
    RolloutNotFoundError,
    SparkVMSetupError,
    SparkVMError,
)
from .firecracker.process import FirecrackerProcess
from .fsops import ensure_dir, read_text, remove_file, remove_tree, write_text
from .image import ManagedImageResolver, RuntimeImage
from .network import NetworkConfig, NetworkManager, render_network_env_file
from .result import VMResult
from .rollouts import Rollout, Rollouts
from cli.setup import ManagedSetup

from .constants import ENV_KEY_RE


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def render_env_file(env: Mapping[str, str]) -> str:
    lines = [f"export {key}={shell_quote(value)}" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def validate_env_mapping(env: Mapping[str, str] | None) -> dict[str, str]:
    if env is None:
        return {}
    if not isinstance(env, Mapping):
        raise TypeError("env must be a mapping of string keys to string values.")

    validated: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not key:
            raise ValueError("Environment variable keys must be non-empty strings.")
        if ENV_KEY_RE.fullmatch(key) is None:
            raise ValueError(f"Invalid environment variable name: {key!r}")
        if not isinstance(value, str):
            raise TypeError(f"Environment variable value for {key!r} must be a string.")
        validated[key] = value
    return validated


def scrub_sensitive_execution_files(worker_dir: Path) -> None:
    execution_disk_path = worker_dir / "rollout.ext4"
    if not execution_disk_path.exists():
        return

    scrub_files_from_ext4_image(
        image_path=execution_disk_path,
        mount_base=worker_dir / "mnt",
        rel_paths=[".sparkvm/env.sh"],
    )


class SparkVM:
    def __init__(
        self,
        *,
        runtime: str | None = None,
        vcpu: int = DEFAULT_VCPU,
        memory: int | str = DEFAULT_MEMORY,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        network: bool = False,
        env: Mapping[str, str] | None = None,
        home_dir: str | Path | None = None,
        base_image: str | None = None,
    ) -> None:
        explicit_runtime = runtime
        if base_image is not None:
            explicit_runtime = base_image
        self._runtime_override = explicit_runtime is not None

        self.config: SparkVMConfig = build_config(
            vcpu=vcpu,
            memory=memory,
            timeout=timeout,
            runtime=explicit_runtime,
            base_image=base_image,
            network=network,
            home_dir=home_dir,
        )
        self._env = validate_env_mapping(env)
        self._setup = ManagedSetup(self.config)
        self._images = ManagedImageResolver(self.config)
        self._rollouts = Rollouts(home_dir=self.config.home_dir)
        self._network = NetworkManager(home_dir=self.config.home_dir)

    def run(self, rollout: str | Rollout) -> VMResult:
        rollout_obj = self.resolve_rollout(rollout)
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
        network_config: NetworkConfig | None = None

        failure: Exception | None = None
        cleanup_failure_message: str | None = None
        final_result: VMResult | None = None

        try:
            firecracker_bin = self._setup.firecracker_binary_path()
            self._setup.assert_kvm_available()
            runtime_image = self._images.resolve(selected_runtime)
            self.assert_runtime_image_permissions(runtime_image)
            self.ensure_rootfs_readonly_mountable(runtime_image)

            if self.config.network_enabled:
                network_config = self._network.setup(vm_id)

            runtime_files = self.runtime_execution_files(network_config)
            execution_disk.copy_rollout(runtime_files=runtime_files if runtime_files else None)

            firecracker = FirecrackerProcess(
                firecracker_bin=firecracker_bin,
                socket_path=socket_path,
                log_path=firecracker_log_path,
            )
            firecracker.start(startup_timeout_sec=min(5.0, self.config.timeout_sec))

            api = FirecrackerAPIClient(socket_path)
            self.wait_for_firecracker_socket(api, firecracker, timeout_sec=self.config.timeout_sec)
            self.configure_microvm(
                api=api,
                runtime_image=runtime_image,
                execution_disk_path=execution_disk_path,
            )
            if network_config is not None:
                api.attach_network(host_dev_name=network_config.tap_name, guest_mac=network_config.guest_mac)
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
                self.preserve_worker_for_failed_result(
                    worker_dir=worker_dir,
                    socket_path=socket_path,
                    firecracker=firecracker,
                    rollout=rollout_obj,
                    runtime=selected_runtime,
                    vm_id=vm_id,
                    result=timeout_result,
                )
                final_result = timeout_result
            else:
                duration_ms = int((time.monotonic() - started_at) * 1000)
                result = execution_disk.read_result(
                    vm_id=vm_id,
                    duration_ms=duration_ms,
                    firecracker_log_path=firecracker_log_path,
                )
                result = self.annotate_oom(result=result, firecracker_log_path=firecracker_log_path)
                if result.passed:
                    self.cleanup_worker_on_completion(
                        worker_dir=worker_dir,
                        socket_path=socket_path,
                        firecracker=firecracker,
                        execution_disk=execution_disk,
                    )
                else:
                    self.preserve_worker_for_failed_result(
                        worker_dir=worker_dir,
                        socket_path=socket_path,
                        firecracker=firecracker,
                        rollout=rollout_obj,
                        runtime=selected_runtime,
                        vm_id=vm_id,
                        result=result,
                    )
                final_result = result
        except (FirecrackerAPIError, FirecrackerProcessError) as exc:
            failure = FirecrackerBootError(
                self.format_boot_failure(
                    vm_id=vm_id,
                    worker_dir=worker_dir,
                    socket_path=socket_path,
                    firecracker_log_path=firecracker_log_path,
                    reason=str(exc),
                )
            )
        except SparkVMError as exc:
            failure = exc
        except Exception as exc:
            failure = FirecrackerBootError(str(exc))
        if network_config is not None:
            try:
                self._network.cleanup(network_config)
            except Exception as exc:
                if failure is None:
                    failure = exc if isinstance(exc, SparkVMError) else CleanupError(str(exc))
                else:
                    cleanup_failure_message = str(exc)

        if failure is not None:
            if firecracker is not None:
                firecracker.stop()

            duration_ms = int((time.monotonic() - started_at) * 1000)
            scrub = self.scrub_or_remove_execution_disk(worker_dir=worker_dir, env_present=bool(self._env))
            note_parts: list[str] = []
            if scrub.note is not None:
                note_parts.append(scrub.note)
            if cleanup_failure_message is not None:
                note_parts.append(f"network_cleanup_error={cleanup_failure_message}")
            self.write_failure_record(
                worker_dir=worker_dir,
                vm_id=vm_id,
                rollout=rollout_obj,
                runtime=selected_runtime,
                error=failure,
                duration_ms=duration_ms,
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=execution_disk_path,
                execution_disk_preserved=scrub.execution_disk_preserved,
                secret_scrubbed=scrub.secret_scrubbed,
                note="; ".join(note_parts) if note_parts else None,
            )
            raise failure

        assert final_result is not None
        return final_result

    def runtime_execution_files(self, network_config: NetworkConfig | None) -> dict[str, str]:
        files: dict[str, str] = {}
        if self._env:
            files[".sparkvm/env.sh"] = render_env_file(self._env)
        if network_config is not None:
            files[".sparkvm/network.env"] = render_network_env_file(network_config)
        return files

    def resolve_rollout(self, rollout: str | Rollout) -> Rollout:
        if isinstance(rollout, Rollout):
            if not rollout.path.exists():
                raise RolloutNotFoundError(f"Rollout path does not exist: {rollout.path}")
            if not (rollout.path / "rollout.json").exists():
                raise RolloutNotFoundError(f"rollout.json missing for rollout: {rollout.id}")
            return rollout

        if isinstance(rollout, str):
            return self._rollouts.get_by_id(rollout)

        raise TypeError("SparkVM.run expects a rollout id (str) or an object returned by Rollouts.create().")

    def wait_for_firecracker_socket(
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
                detail = self.format_firecracker_process_diagnostic(
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
        detail = self.format_firecracker_process_diagnostic(
            process=process,
            reason=f"Timed out waiting for Firecracker API socket after {timeout_sec:.2f}s.",
        )
        raise FirecrackerProcessError(detail)

    def format_firecracker_process_diagnostic(self, *, process: FirecrackerProcess, reason: str) -> str:
        parts = [reason, f"Socket path: {process.socket_path}"]
        if process.log_path is not None:
            parts.append(f"Check Firecracker log: {process.log_path}")
            tail = self.read_log_tail(process.log_path)
            if tail:
                parts.append("Firecracker log tail:")
                parts.append(tail)
        return "\n".join(parts)

    def format_boot_failure(
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
        tail = self.read_log_tail(firecracker_log_path)
        if tail:
            parts.append("Firecracker log tail:")
            parts.append(tail)
        return "\n".join(parts)

    def cleanup_worker_on_completion(
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

    def write_failure_record(
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
        execution_disk_preserved: bool,
        secret_scrubbed: bool,
        note: str | None,
        result_status: str | None = None,
    ) -> None:
        payload = {
            "vm_id": vm_id,
            "rollout_id": rollout.id,
            "rollout_name": rollout.name,
            "rollout_mode": rollout.mode,
            "runtime": runtime,
            "network_enabled": self.config.network_enabled,
            "env_keys": sorted(self._env.keys()),
            "env_values_stored": False,
            "status": "failed",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "duration_ms": duration_ms,
            "firecracker_log_path": str(firecracker_log_path),
            "execution_disk_path": str(execution_disk_path) if execution_disk_preserved else None,
            "execution_disk_preserved": execution_disk_preserved,
            "secret_scrubbed": secret_scrubbed,
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        if result_status is not None:
            payload["result_status"] = result_status
        if note is not None:
            payload["note"] = note

        failure_path = worker_dir / "failure.json"
        try:
            write_text(failure_path, json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass

    def read_log_tail(self, log_path: Path, *, max_lines: int = 40) -> str:
        if not log_path.exists():
            return ""
        try:
            lines = read_text(log_path, encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-max_lines:])

    def annotate_oom(self, *, result: VMResult, firecracker_log_path: Path) -> VMResult:
        if result.oom_killed:
            return result
        if result.timed_out:
            return result
        if result.exit_code == 0:
            return result

        log_tail = self.read_log_tail(firecracker_log_path, max_lines=200).lower()
        indicators = ("out of memory", "oom-kill", "oom killed", "killed process")
        if any(marker in log_tail for marker in indicators):
            return replace(result, status="oom", oom_killed=True)
        if result.run is not None and result.run.exit_code == 137:
            return replace(result, status="oom", oom_killed=True)
        return result

    def configure_microvm(
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
                "is_read_only": True,
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

    def assert_runtime_image_permissions(self, runtime_image: RuntimeImage) -> None:
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

    def ensure_rootfs_readonly_mountable(self, runtime_image: RuntimeImage) -> None:
        rootfs = runtime_image.rootfs_image
        if not rootfs.exists():
            return

        try:
            completed = subprocess.run(
                ["e2fsck", "-p", str(rootfs)],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise SparkVMSetupError(
                "e2fsck is required to verify runtime ext4 images for read-only VM boot. "
                "Install e2fsprogs on the host."
            ) from exc

        if completed.returncode in {0, 1}:
            return

        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or "unknown e2fsck failure"
        raise SparkVMSetupError(
            f"Runtime image filesystem check failed for {rootfs}: {detail}. "
            "Try rebuilding the runtime with `sparkvm dockify ... --force`."
        )

    def scrub_or_remove_execution_disk(self, *, worker_dir: Path, env_present: bool) -> SecretScrubResult:
        execution_disk_path = worker_dir / "rollout.ext4"
        if not execution_disk_path.exists():
            return SecretScrubResult(execution_disk_preserved=False, secret_scrubbed=not env_present, note=None)

        if not env_present:
            return SecretScrubResult(execution_disk_preserved=True, secret_scrubbed=True, note=None)

        try:
            scrub_sensitive_execution_files(worker_dir)
            return SecretScrubResult(execution_disk_preserved=True, secret_scrubbed=True, note=None)
        except Exception:
            try:
                remove_file(execution_disk_path, missing_ok=True)
            except OSError:
                pass
            return SecretScrubResult(
                execution_disk_preserved=False,
                secret_scrubbed=False,
                note="Execution disk removed to avoid preserving runtime secrets.",
            )

    def preserve_worker_for_failed_result(
        self,
        *,
        worker_dir: Path,
        socket_path: Path,
        firecracker: FirecrackerProcess | None,
        rollout: Rollout,
        runtime: str,
        vm_id: str,
        result: VMResult,
    ) -> None:
        if firecracker is not None:
            try:
                firecracker.stop()
            except Exception:
                pass
        if socket_path.exists():
            try:
                remove_file(socket_path, missing_ok=True)
            except OSError:
                pass

        error = SparkVMError(
            f"Guest execution failed with status={result.status}, exit_code={result.exit_code}"
        )
        self.write_failure_record(
            worker_dir=worker_dir,
            vm_id=vm_id,
            rollout=rollout,
            runtime=runtime,
            error=error,
            duration_ms=result.duration_ms,
            firecracker_log_path=result.firecracker_log_path or (worker_dir / "firecracker.log"),
            execution_disk_path=result.execution_disk_path or (worker_dir / "rollout.ext4"),
            execution_disk_preserved=True,
            secret_scrubbed=True,
            note=None,
            result_status=result.status,
        )


class SecretScrubResult:
    def __init__(self, *, execution_disk_preserved: bool, secret_scrubbed: bool, note: str | None) -> None:
        self.execution_disk_preserved = execution_disk_preserved
        self.secret_scrubbed = secret_scrubbed
        self.note = note


__all__ = ["SparkVM", "shell_quote", "render_env_file", "scrub_sensitive_execution_files"]
