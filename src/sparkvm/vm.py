"""High-level one-shot SparkVM API."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
import pwd
import re
import shlex
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .commands import run_checked
from .firecracker.api import FirecrackerAPIClient
from .config import DEFAULT_MEMORY, DEFAULT_TIMEOUT_SEC, DEFAULT_VCPU, SparkVMConfig, build_config, parse_memory_to_mib
from .disk import (
    ExecutionDisk,
    create_worker_rootfs,
    debugfs_dump_file,
    mount_ext4,
    scrub_files_from_ext4_image,
    unmount_ext4,
)
from .errors import (
    CleanupError,
    ExecutionDiskError,
    FirecrackerAPIError,
    FirecrackerBootError,
    FirecrackerProcessError,
    GuestPanicError,
    JobTimeoutError,
    KernelImageNotFound,
    RuntimeImagePermissionError,
    RolloutNotFoundError,
    SparkVMError,
    WorkerRootfsError,
)
from .firecracker.process import FirecrackerProcess
from .fsops import ensure_dir, read_text, remove_file, remove_tree, write_text
from .image import ManagedImageResolver, RuntimeImage
from .network import NetworkConfig, NetworkManager, render_network_env_file
from .result import VMResult
from .resource_policy import assert_resource_capacity, check_resource_capacity
from .rollouts import Rollout, Rollouts
from .utils import shell_quote
from .workers import Workers
from cli.setup import ManagedSetup

from .constants import BOOT_ARGS, DEFAULT_RUN_TIMEOUT_SEC, DEFAULT_SETUP_TIMEOUT_SEC, ENV_KEY_RE


def render_env_file(env: Mapping[str, str]) -> str:
    lines = [f"export {key}={shell_quote(value)}" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def render_runtime_config_file(*, setup_timeout_sec: int, run_timeout_sec: int) -> str:
    return (
        f"SPARKVM_SETUP_TIMEOUT_SEC={int(setup_timeout_sec)}\n"
        f"SPARKVM_RUN_TIMEOUT_SEC={int(run_timeout_sec)}\n"
    )


def escape_sed_pattern(value: str) -> str:
    escaped = re.escape(value)
    return escaped.replace("/", r"\/")


def render_redact_sed_file(secrets: Sequence[str]) -> str | None:
    rules: list[str] = []
    for secret in secrets:
        if not secret or "\n" in secret or "\x00" in secret:
            continue
        rules.append(f"s/{escape_sed_pattern(secret)}/[REDACTED]/g")

    if not rules:
        return None
    return "\n".join(rules) + "\n"


def redact_text(text: str, secrets: Sequence[str]) -> str:
    redacted = text
    unique_secrets = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    for secret in unique_secrets:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def redact_file_in_place(path: Path, secrets: Sequence[str]) -> None:
    if not path.exists():
        return
    if not any(secret for secret in secrets):
        return
    try:
        raw = read_text(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    redacted = redact_text(raw, secrets)
    if redacted == raw:
        return
    try:
        write_text(path, redacted, encoding="utf-8")
    except OSError:
        return


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
        rel_paths=[".sparkvm/env.sh", ".sparkvm/redact.sed"],
    )


RESULT_FS_PATHS = (
    "/results/network.stdout.log",
    "/results/network.stderr.log",
    "/results/setup.stdout.log",
    "/results/setup.stderr.log",
    "/results/setup.exit_code",
    "/results/run.stdout.log",
    "/results/run.stderr.log",
    "/results/run.exit_code",
    "/results/final_exit_code",
    "/output.log",
    "/error.log",
    "/exit_code",
)

PARTIAL_RESULT_FILES = (
    "setup.stdout.log",
    "setup.stderr.log",
    "setup.exit_code",
    "run.stdout.log",
    "run.stderr.log",
    "run.exit_code",
    "final_exit_code",
    "network.stdout.log",
    "network.stderr.log",
)


@dataclass(frozen=True)
class RunConfig:
    vcpu: int | None = None
    memory: int | str | None = None
    disk: int | str | None = None
    timeout: float | None = None
    runtime: str | None = None
    network: bool | None = None
    env: Mapping[str, str] | None = None


def parse_disk_to_mib(disk: int | str) -> int:
    return parse_memory_to_mib(disk)


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
        keep_rootfs_on_failure: bool = False,
        keep_disk_on_failure: bool = False,
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
        self._setup_timeout_sec = DEFAULT_SETUP_TIMEOUT_SEC
        self._run_timeout_sec = DEFAULT_RUN_TIMEOUT_SEC
        self._keep_rootfs_on_failure = bool(keep_rootfs_on_failure)
        self._keep_disk_on_failure = bool(keep_disk_on_failure)
        self._setup = ManagedSetup(self.config)
        self._images = ManagedImageResolver(self.config)
        self._rollouts = Rollouts(home_dir=self.config.home_dir)
        self._network = NetworkManager(home_dir=self.config.home_dir)

    def __enter__(self) -> SparkVM:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        pass

    def run(self, rollout_id: str, config: RunConfig | None = None) -> VMResult:
        if not isinstance(rollout_id, str):
            raise TypeError("SparkVM.run expects a rollout id string.")
        if config is not None and not isinstance(config, RunConfig):
            raise TypeError("SparkVM.run config must be a RunConfig instance.")

        run_vcpu = self.config.vcpu if config is None or config.vcpu is None else config.vcpu
        run_memory: int | str = self.config.memory_mib if config is None or config.memory is None else config.memory
        run_timeout = self.config.timeout_sec if config is None or config.timeout is None else config.timeout
        run_runtime = self.config.runtime if config is None or config.runtime is None else config.runtime
        run_network = self.config.network_enabled if config is None or config.network is None else config.network
        run_env = self._env if config is None or config.env is None else validate_env_mapping(config.env)
        run_disk = "4G" if config is None or config.disk is None else config.disk
        run_disk_mib = parse_disk_to_mib(run_disk)

        previous_config = self.config
        previous_env = self._env
        previous_override = self._runtime_override
        self.config = build_config(
            vcpu=run_vcpu,
            memory=run_memory,
            timeout=run_timeout,
            runtime=run_runtime,
            network=run_network,
            home_dir=previous_config.home_dir,
        )
        self._env = run_env
        self._runtime_override = True

        try:
            rollout_obj = self.resolve_rollout(rollout_id)
        except Exception:
            self._restore_run_state(config=previous_config, env=previous_env, runtime_override=previous_override)
            raise
        selected_runtime = self.config.runtime
        assert_resource_capacity(
            home_dir=self.config.home_dir,
            vcpu=int(run_vcpu),
            memory_mib=parse_memory_to_mib(run_memory),
            disk_mib=run_disk_mib,
        )
        machine_specs = {
            "vcpu": self.config.vcpu,
            "memory": self.config.memory_mib,
            "disk": run_disk_mib,
            "timeout": self.config.timeout_sec,
            "runtime": self.config.runtime,
            "network": self.config.network_enabled,
        }

        self._setup.ensure_layout()

        vm_id = f"vm-{uuid4().hex[:12]}"
        worker_dir = self.config.workers_dir / vm_id
        ensure_dir(worker_dir, exist_ok=False)

        socket_path = worker_dir / "firecracker.sock"
        firecracker_log_path = worker_dir / "firecracker.log"
        worker_rootfs_path = worker_dir / "rootfs.ext4"
        execution_disk_path = worker_dir / "rollout.ext4"
        mount_base = worker_dir / "mnt"

        execution_disk = ExecutionDisk(
            rollout=rollout_obj,
            path=execution_disk_path,
            size_mb=run_disk_mib,
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
            runtime_image = self.resolve_runtime_image_for_rollout(rollout_obj, selected_runtime)
            selected_runtime = runtime_image.name
            self.assert_runtime_image_permissions(runtime_image)
            create_worker_rootfs(base_rootfs=runtime_image.rootfs_image, worker_rootfs=worker_rootfs_path)

            if self.config.network_enabled:
                network_config = self._network.setup(vm_id)

            runtime_files = self.runtime_execution_files(rollout=rollout_obj, network_config=network_config)
            execution_disk.copy_rollout(runtime_files=runtime_files if runtime_files else None)
            self.assert_worker_image_permissions(
                worker_rootfs=worker_rootfs_path,
                execution_disk_path=execution_disk_path,
            )

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
                worker_rootfs_path=worker_rootfs_path,
                execution_disk_path=execution_disk_path,
            )
            if network_config is not None:
                api.attach_network(host_dev_name=network_config.tap_name, guest_mac=network_config.guest_mac)
            api.put("/actions", {"action_type": "InstanceStart"})

            try:
                firecracker.wait(timeout_sec=self.config.timeout_sec)
            except subprocess.TimeoutExpired:
                duration_ms = int((time.monotonic() - started_at) * 1000)
                failure = JobTimeoutError(
                    f"SparkVM run timed out after {self.config.timeout_sec:.2f}s before guest shutdown."
                )
                self.cleanup_process_socket(
                    firecracker=firecracker,
                    socket_path=socket_path,
                )
            else:
                duration_ms = int((time.monotonic() - started_at) * 1000)
                result = execution_disk.read_result(
                    vm_id=vm_id,
                    duration_ms=duration_ms,
                    firecracker_log_path=firecracker_log_path,
                )
                result = replace(result, runtime=selected_runtime)
                result = self.annotate_oom(result=result, firecracker_log_path=firecracker_log_path)
                if result.passed:
                    self.cleanup_worker_on_completion(
                        worker_dir=worker_dir,
                        socket_path=socket_path,
                        firecracker=firecracker,
                        execution_disk=execution_disk,
                    )
                    if rollout_obj.delete_on_success:
                        try:
                            self._rollouts.delete_by_id(rollout_obj.id)
                        except RolloutNotFoundError:
                            pass
                    final_result = result
                else:
                    self.cleanup_process_socket(
                        firecracker=firecracker,
                        socket_path=socket_path,
                    )
                    final_result = self.preserve_worker_after_failure(
                        worker_dir=worker_dir,
                        env=self._env,
                        vm_id=vm_id,
                        rollout=rollout_obj,
                        runtime=selected_runtime,
                        result=result,
                        error=None,
                        duration_ms=duration_ms,
                        machine_specs=machine_specs,
                    )
        except (FirecrackerAPIError, FirecrackerProcessError) as exc:
            failure = self.classify_infrastructure_error(
                error=exc,
                vm_id=vm_id,
                worker_dir=worker_dir,
                socket_path=socket_path,
                firecracker_log_path=firecracker_log_path,
            )
        except SparkVMError as exc:
            if self.detect_guest_panic(firecracker_log_path):
                failure = GuestPanicError(
                    self.format_boot_failure(
                        vm_id=vm_id,
                        worker_dir=worker_dir,
                        socket_path=socket_path,
                        firecracker_log_path=firecracker_log_path,
                        reason=str(exc),
                    )
                )
            else:
                failure = exc
        except Exception as exc:
            failure = self.classify_infrastructure_error(
                error=exc,
                vm_id=vm_id,
                worker_dir=worker_dir,
                socket_path=socket_path,
                firecracker_log_path=firecracker_log_path,
            )
        if network_config is not None:
            try:
                self._network.cleanup(network_config)
            except Exception as exc:
                if failure is None:
                    failure = exc if isinstance(exc, SparkVMError) else CleanupError(str(exc))
                else:
                    cleanup_failure_message = str(exc)

        if failure is not None:
            self.cleanup_process_socket(
                firecracker=firecracker,
                socket_path=socket_path,
            )

            preserved = self.preserve_worker_after_failure(
                worker_dir=worker_dir,
                env=self._env,
                vm_id=vm_id,
                rollout=rollout_obj,
                runtime=selected_runtime,
                result=None,
                error=failure,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                machine_specs=machine_specs,
            )
            if cleanup_failure_message is not None:
                self.append_worker_note(
                    worker_dir=worker_dir,
                    filename="failure.json",
                    note=f"network_cleanup_error={cleanup_failure_message}",
                )
            del preserved
            self._restore_run_state(config=previous_config, env=previous_env, runtime_override=previous_override)
            raise failure

        assert final_result is not None
        self._restore_run_state(config=previous_config, env=previous_env, runtime_override=previous_override)
        return final_result

    def _restore_run_state(
        self,
        *,
        config: SparkVMConfig,
        env: Mapping[str, str],
        runtime_override: bool,
    ) -> None:
        self.config = config
        self._env = dict(env)
        self._runtime_override = runtime_override

    def runtime_execution_files(self, *, rollout: Rollout, network_config: NetworkConfig | None) -> dict[str, str]:
        runtime_env = render_runtime_config_file(
            setup_timeout_sec=self._setup_timeout_sec,
            run_timeout_sec=self._run_timeout_sec,
        )
        if rollout.setup_cmd:
            runtime_env += "SPARKVM_RUN_SETUP_IN_GUEST=1\n"
        files: dict[str, str] = {
            ".sparkvm/runtime.env": runtime_env,
        }
        if self._env:
            files[".sparkvm/env.sh"] = render_env_file(self._env)
            redact_file = render_redact_sed_file(self._env.values())
            if redact_file is not None:
                files[".sparkvm/redact.sed"] = redact_file
        if network_config is not None:
            files[".sparkvm/network.env"] = render_network_env_file(network_config)
        return files

    def resolve_rollout(self, rollout_id: str) -> Rollout:
        return self._rollouts.get_by_id(rollout_id)

    def resolve_runtime_image_for_rollout(self, rollout: Rollout, selected_runtime: str) -> RuntimeImage:
        runtime_image_path: Path | None = None
        runtime_image_id = rollout.runtime
        if isinstance(rollout.runtime_image, dict):
            path_raw = rollout.runtime_image.get("path")
            if isinstance(path_raw, str) and path_raw.strip():
                runtime_image_path = Path(path_raw).expanduser()
            id_raw = rollout.runtime_image.get("id")
            if isinstance(id_raw, str) and id_raw.strip():
                runtime_image_id = id_raw.strip()
        elif isinstance(rollout.rootfs_path, str) and rollout.rootfs_path.strip():
            runtime_image_path = Path(rollout.rootfs_path).expanduser()

        if runtime_image_path is None:
            return self._images.resolve(selected_runtime)

        kernel_image = self.config.image_dir / "vmlinux"
        if not kernel_image.exists():
            raise KernelImageNotFound("Kernel image not found. Run `sparkvm setup`.")

        metadata_path: Path | None = None
        if isinstance(rollout.runtime_image, dict):
            metadata_raw = rollout.runtime_image.get("metadata_path")
            if isinstance(metadata_raw, str) and metadata_raw.strip():
                metadata_path = Path(metadata_raw).expanduser()

        return RuntimeImage(
            name=runtime_image_id,
            kernel_image=kernel_image,
            rootfs_image=runtime_image_path,
            boot_args=BOOT_ARGS,
            metadata_path=metadata_path,
        )

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

    def detect_guest_panic(self, firecracker_log_path: Path) -> bool:
        text = self.read_log_tail(firecracker_log_path, max_lines=400).lower()
        markers = ("kernel panic", "not syncing", "attempted to kill init")
        return any(marker in text for marker in markers)

    def classify_infrastructure_error(
        self,
        *,
        error: Exception,
        vm_id: str,
        worker_dir: Path,
        socket_path: Path,
        firecracker_log_path: Path,
    ) -> SparkVMError:
        message = self.format_boot_failure(
            vm_id=vm_id,
            worker_dir=worker_dir,
            socket_path=socket_path,
            firecracker_log_path=firecracker_log_path,
            reason=str(error),
        )
        if self.detect_guest_panic(firecracker_log_path):
            return GuestPanicError(message)
        return FirecrackerBootError(message)

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

    def cleanup_process_socket(self, *, firecracker: FirecrackerProcess | None, socket_path: Path) -> None:
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

    def write_worker_json(self, *, path: Path, payload: dict[str, object]) -> None:
        try:
            write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass

    def append_worker_note(self, *, worker_dir: Path, filename: str, note: str) -> None:
        record_path = worker_dir / filename
        if not record_path.exists():
            return
        try:
            payload = json.loads(read_text(record_path, encoding="utf-8"))
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        existing = payload.get("note")
        if isinstance(existing, str) and existing.strip():
            payload["note"] = f"{existing}; {note}"
        else:
            payload["note"] = note
        self.write_worker_json(path=record_path, payload=payload)

    def write_result_record(
        self,
        *,
        worker_dir: Path,
        vm_id: str,
        rollout: Rollout,
        result: VMResult,
        prune: Mapping[str, Any],
        results_path: Path | None,
        partial: Mapping[str, Any],
        machine_specs: Mapping[str, Any],
    ) -> None:
        payload: dict[str, object] = {
            "vm_id": vm_id,
            "rollout_id": rollout.id,
            "rollout_name": rollout.name,
            "rollout_mode": rollout.mode,
            "runtime": result.runtime,
            "status": result.status,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "worker_preserved": True,
            "env_keys": sorted(self._env.keys()),
            "env_values_stored": False,
            "worker_path": str(worker_dir),
            "firecracker_log_path": str(result.firecracker_log_path or (worker_dir / "firecracker.log")),
            "results_path": str(results_path) if results_path is not None else None,
            "rootfs_preserved": bool(prune.get("rootfs_preserved", False)),
            "rootfs_removed_reason": prune.get("rootfs_removed_reason"),
            "execution_disk_path": str(worker_dir / "rollout.ext4") if bool(prune.get("execution_disk_preserved", False)) else None,
            "execution_disk_preserved": bool(prune.get("execution_disk_preserved", False)),
            "execution_disk_removed_reason": prune.get("execution_disk_removed_reason"),
            "secret_scrubbed": bool(prune.get("secret_scrubbed", True)),
            "secret_scrub_failed": bool(prune.get("secret_scrub_failed", False)),
            "partial_results_extracted": bool(partial.get("partial_results_extracted", False)),
            "partial_results_error": partial.get("partial_results_error"),
            "partial_result_files": partial.get("files", []),
            "machine_specs": dict(machine_specs),
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        self.write_worker_json(path=worker_dir / "result.json", payload=payload)

    def write_failure_json(
        self,
        *,
        worker_dir: Path,
        vm_id: str,
        rollout: Rollout,
        runtime: str,
        error: BaseException,
        duration_ms: int,
        firecracker_log_path: Path,
        prune: Mapping[str, Any],
        results_path: Path | None,
        partial: Mapping[str, Any],
        machine_specs: Mapping[str, Any],
    ) -> None:
        if isinstance(error, JobTimeoutError):
            status = "timeout"
        elif isinstance(error, GuestPanicError):
            status = "guest_panic"
        elif isinstance(error, FirecrackerBootError):
            status = "boot_failed"
        else:
            status = "infrastructure_failed"

        payload: dict[str, object] = {
            "vm_id": vm_id,
            "rollout_id": rollout.id,
            "rollout_name": rollout.name,
            "rollout_mode": rollout.mode,
            "runtime": runtime,
            "status": status,
            "exit_code": None,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "duration_ms": duration_ms,
            "worker_preserved": True,
            "env_keys": sorted(self._env.keys()),
            "env_values_stored": False,
            "worker_path": str(worker_dir),
            "firecracker_log_path": str(firecracker_log_path),
            "results_path": str(results_path) if results_path is not None else None,
            "rootfs_preserved": bool(prune.get("rootfs_preserved", False)),
            "rootfs_removed_reason": prune.get("rootfs_removed_reason"),
            "execution_disk_path": str(worker_dir / "rollout.ext4") if bool(prune.get("execution_disk_preserved", False)) else None,
            "execution_disk_preserved": bool(prune.get("execution_disk_preserved", False)),
            "execution_disk_removed_reason": prune.get("execution_disk_removed_reason"),
            "secret_scrubbed": bool(prune.get("secret_scrubbed", True)),
            "secret_scrub_failed": bool(prune.get("secret_scrub_failed", False)),
            "partial_results_extracted": bool(partial.get("partial_results_extracted", False)),
            "partial_results_error": partial.get("partial_results_error"),
            "partial_result_files": partial.get("files", []),
            "machine_specs": dict(machine_specs),
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        self.write_worker_json(path=worker_dir / "failure.json", payload=payload)

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

    def extract_partial_results_from_execution_disk(
        self,
        *,
        execution_disk_path: Path,
        output_results_dir: Path,
        env: Mapping[str, str],
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "partial_results_extracted": False,
            "partial_results_error": None,
            "files": [],
        }
        secrets = list(env.values())
        if not execution_disk_path.exists():
            metadata["partial_results_error"] = "execution disk not present"
            return metadata

        mount_base = output_results_dir.parent / "mnt"
        mount_dir = mount_base / "rollout-partial-results-mount"
        mounted = False
        copied_files: list[str] = []
        try:
            ensure_dir(mount_base, exist_ok=True)
            ensure_dir(output_results_dir, exist_ok=True)
            run_checked(
                ["mount", "-o", "loop,ro", str(execution_disk_path), str(mount_dir)],
                error_factory=ExecutionDiskError,
            )
            mounted = True
            result_roots = [mount_dir / "results", mount_dir / "job" / "results"]
            for filename in PARTIAL_RESULT_FILES:
                source_path: Path | None = None
                for root in result_roots:
                    candidate = root / filename
                    if candidate.exists() and candidate.is_file():
                        source_path = candidate
                        break
                if source_path is None:
                    continue
                try:
                    raw = read_text(source_path, encoding="utf-8", errors="replace")
                except OSError:
                    continue
                sanitized = redact_text(raw, secrets)
                write_text(output_results_dir / filename, sanitized, encoding="utf-8")
                copied_files.append(filename)
        except Exception as exc:
            metadata["partial_results_error"] = str(exc)
        finally:
            if mounted:
                try:
                    unmount_ext4(mount_dir)
                except Exception as exc:
                    if metadata.get("partial_results_error") is None:
                        metadata["partial_results_error"] = f"failed to unmount partial results mount: {exc}"
            if mount_dir.exists():
                try:
                    mount_dir.rmdir()
                except OSError:
                    remove_tree(mount_dir, ignore_errors=True)

        metadata["files"] = copied_files
        metadata["partial_results_extracted"] = bool(copied_files)
        return metadata

    def copy_sanitized_results_from_execution_disk(
        self,
        *,
        execution_disk_path: Path,
        worker_dir: Path,
        env: Mapping[str, str],
    ) -> dict[str, Any]:
        return self.extract_partial_results_from_execution_disk(
            execution_disk_path=execution_disk_path,
            output_results_dir=worker_dir / "results",
            env=env,
        )

    def scrub_and_redact_execution_disk(
        self,
        *,
        execution_disk_path: Path,
        worker_dir: Path,
        secrets: Sequence[str],
    ) -> None:
        mount_base = worker_dir / "mnt"
        ensure_dir(mount_base, exist_ok=True)
        mount_dir = mount_base / "rollout-preserve-scrub-mount"
        mounted = False
        try:
            mount_ext4(execution_disk_path, mount_dir)
            mounted = True

            for rel in (".sparkvm/env.sh", ".sparkvm/redact.sed"):
                try:
                    remove_file(mount_dir / rel, missing_ok=True)
                except OSError:
                    pass

            for rel in (
                "results/network.stdout.log",
                "results/network.stderr.log",
                "results/setup.stdout.log",
                "results/setup.stderr.log",
                "results/run.stdout.log",
                "results/run.stderr.log",
                "output.log",
                "error.log",
            ):
                file_path = mount_dir / rel
                if not file_path.exists():
                    continue
                raw = read_text(file_path, encoding="utf-8", errors="replace")
                sanitized = redact_text(raw, secrets)
                write_text(file_path, sanitized, encoding="utf-8")
        finally:
            if mounted:
                unmount_ext4(mount_dir)
            if mount_dir.exists():
                try:
                    mount_dir.rmdir()
                except OSError:
                    remove_tree(mount_dir, ignore_errors=True)

    def prune_worker_artifacts(
        self,
        *,
        worker_dir: Path,
        keep_rootfs: bool,
        keep_execution_disk: bool,
        env: Mapping[str, str],
    ) -> dict[str, Any]:
        rootfs_path = worker_dir / "rootfs.ext4"
        execution_disk_path = worker_dir / "rollout.ext4"
        secrets = list(env.values())

        rootfs_preserved = rootfs_path.exists()
        rootfs_removed_reason: str | None = None
        if rootfs_preserved and not keep_rootfs:
            try:
                remove_file(rootfs_path, missing_ok=True)
            except OSError:
                rootfs_preserved = rootfs_path.exists()
                rootfs_removed_reason = "failed to remove rootfs.ext4"
            else:
                rootfs_preserved = False
                rootfs_removed_reason = "keep_rootfs_on_failure is false"
        elif not rootfs_preserved:
            rootfs_removed_reason = "rootfs.ext4 not present"

        execution_disk_preserved = execution_disk_path.exists()
        execution_disk_removed_reason: str | None = None
        secret_scrubbed = True
        secret_scrub_failed = False

        if execution_disk_preserved and not keep_execution_disk:
            try:
                remove_file(execution_disk_path, missing_ok=True)
            except OSError:
                execution_disk_preserved = execution_disk_path.exists()
                execution_disk_removed_reason = "failed to remove rollout.ext4"
            else:
                execution_disk_preserved = False
                execution_disk_removed_reason = "keep_disk_on_failure is false"
        elif execution_disk_preserved and keep_execution_disk and secrets:
            try:
                self.scrub_and_redact_execution_disk(
                    execution_disk_path=execution_disk_path,
                    worker_dir=worker_dir,
                    secrets=secrets,
                )
            except Exception:
                try:
                    remove_file(execution_disk_path, missing_ok=True)
                except OSError:
                    pass
                execution_disk_preserved = False
                secret_scrubbed = False
                secret_scrub_failed = True
                execution_disk_removed_reason = "runtime env secrets could not be scrubbed safely"
        elif not execution_disk_preserved:
            execution_disk_removed_reason = "rollout.ext4 not present"

        return {
            "rootfs_preserved": rootfs_preserved,
            "rootfs_removed_reason": rootfs_removed_reason,
            "execution_disk_preserved": execution_disk_preserved,
            "execution_disk_removed_reason": execution_disk_removed_reason,
            "secret_scrubbed": secret_scrubbed,
            "secret_scrub_failed": secret_scrub_failed,
        }

    def configure_microvm(
        self,
        *,
        api: FirecrackerAPIClient,
        runtime_image: RuntimeImage,
        worker_rootfs_path: Path,
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
                "path_on_host": str(worker_rootfs_path),
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

    def assert_runtime_image_permissions(self, runtime_image: RuntimeImage) -> None:
        rootfs = runtime_image.rootfs_image
        if not rootfs.exists():
            return
        if not os.access(rootfs, os.R_OK):
            raise RuntimeImagePermissionError(
                "Runtime image is not readable by current user: "
                f"{rootfs}.\nFix:\n"
                f"  sudo chown {pwd.getpwuid(os.getuid()).pw_name}:{pwd.getpwuid(os.getuid()).pw_name} {rootfs}\n"
                f"  chmod 0644 {rootfs}"
            )

    def assert_worker_image_permissions(self, *, worker_rootfs: Path, execution_disk_path: Path) -> None:
        if not worker_rootfs.exists():
            raise WorkerRootfsError(f"Worker rootfs missing after copy: {worker_rootfs}")
        if not os.access(worker_rootfs, os.R_OK | os.W_OK):
            raise WorkerRootfsError(
                "Worker rootfs is not readable/writable by current user: "
                f"{worker_rootfs}"
            )
        if not execution_disk_path.exists():
            raise CleanupError(f"Execution disk missing after build: {execution_disk_path}")
        if not os.access(execution_disk_path, os.R_OK | os.W_OK):
            raise CleanupError(
                "Execution disk is not readable/writable by current user: "
                f"{execution_disk_path}"
            )

    def preserve_worker_after_failure(
        self,
        *,
        worker_dir: Path,
        env: Mapping[str, str],
        rollout: Rollout,
        runtime: str,
        vm_id: str,
        result: VMResult | None,
        error: BaseException | None,
        duration_ms: int,
        machine_specs: Mapping[str, Any],
    ) -> VMResult | None:
        firecracker_log_path = worker_dir / "firecracker.log"

        redact_file_in_place(firecracker_log_path, list(env.values()))
        partial = self.copy_sanitized_results_from_execution_disk(
            execution_disk_path=worker_dir / "rollout.ext4",
            worker_dir=worker_dir,
            env=env,
        )

        prune = self.prune_worker_artifacts(
            worker_dir=worker_dir,
            keep_rootfs=self._keep_rootfs_on_failure,
            keep_execution_disk=self._keep_disk_on_failure,
            env=env,
        )
        results_dir = worker_dir / "results"
        has_results = bool(partial.get("partial_results_extracted", False)) and results_dir.exists() and any(results_dir.iterdir())
        results_path = results_dir if has_results else None

        if result is not None:
            sanitized_result = replace(
                result,
                worker_path=worker_dir,
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=(worker_dir / "rollout.ext4") if bool(prune.get("execution_disk_preserved")) else None,
            )
            self.write_result_record(
                worker_dir=worker_dir,
                vm_id=vm_id,
                rollout=rollout,
                result=sanitized_result,
                prune=prune,
                results_path=results_path,
                partial=partial,
                machine_specs=machine_specs,
            )
            return sanitized_result

        if error is None:
            return None

        self.write_failure_json(
            worker_dir=worker_dir,
            vm_id=vm_id,
            rollout=rollout,
            runtime=runtime,
            error=error,
            duration_ms=duration_ms,
            firecracker_log_path=firecracker_log_path,
            prune=prune,
            results_path=results_path,
            partial=partial,
            machine_specs=machine_specs,
        )
        return None

    def recycle(
        self,
        *,
        interval: float = 5.0,
        timeout: float | None = None,
        max_parallel: int = 1,
        once: bool = False,
    ) -> int:
        workers = Workers(home_dir=self.config.home_dir)
        started = time.monotonic()
        recovered = 0
        max_parallel = max(1, int(max_parallel))

        while True:
            candidates = [item for item in workers.list() if item.rollout_id]
            reruns = 0

            for worker in candidates:
                if reruns >= max_parallel:
                    break
                if worker.status == "passed":
                    continue

                machine_specs: dict[str, Any] = {}
                if worker.failure_path is not None:
                    try:
                        machine_specs = workers.failure_json(worker.vm_id).get("machine_specs", {})
                    except Exception:
                        machine_specs = {}
                elif worker.result_path is not None:
                    try:
                        machine_specs = workers.result_json(worker.vm_id).get("machine_specs", {})
                    except Exception:
                        machine_specs = {}

                vcpu = int(machine_specs.get("vcpu", self.config.vcpu))
                memory_mib = int(machine_specs.get("memory", self.config.memory_mib))
                disk_mib = int(machine_specs.get("disk", 4096))
                timeout_sec = float(machine_specs.get("timeout", self.config.timeout_sec))
                runtime = str(machine_specs.get("runtime", self.config.runtime))
                network = bool(machine_specs.get("network", self.config.network_enabled))

                capacity = check_resource_capacity(
                    home_dir=self.config.home_dir,
                    vcpu=vcpu,
                    memory_mib=memory_mib,
                    disk_mib=disk_mib,
                )
                if not capacity.allowed:
                    continue

                try:
                    self.run(
                        str(worker.rollout_id),
                        config=RunConfig(
                            vcpu=vcpu,
                            memory=memory_mib,
                            disk=disk_mib,
                            timeout=timeout_sec,
                            runtime=runtime,
                            network=network,
                        ),
                    )
                    recovered += 1
                except Exception:
                    pass
                reruns += 1

            if once:
                break
            if timeout is not None and (time.monotonic() - started) >= timeout:
                break
            time.sleep(max(0.1, float(interval)))

        return recovered


__all__ = [
    "SparkVM",
    "RunConfig",
    "shell_quote",
    "render_env_file",
    "render_redact_sed_file",
    "redact_text",
    "redact_file_in_place",
    "scrub_sensitive_execution_files",
]
