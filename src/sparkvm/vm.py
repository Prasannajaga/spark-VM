"""High-level one-shot SparkVM API."""

from __future__ import annotations

from dataclasses import replace
import json
import logging
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
from .firecracker.client import FirecrackerAPIClient
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
from .fsops import ensure_dir, read_text, remove_file, remove_tree, write_json_atomic, write_text
from .image import ManagedImageResolver, RuntimeImage
from .logger import configure_logging
from .network import NetworkConfig, NetworkManager, render_network_env_file
from .result import VMResult
from .resource_policy import assert_resource_capacity
from .rollouts import Rollout, Rollouts
from .utils import shell_quote
from cli.setup import ManagedSetup

from .constants import BOOT_ARGS, DEFAULT_RUN_TIMEOUT_SEC, DEFAULT_SETUP_TIMEOUT_SEC, ENV_KEY_RE


LOGGER = logging.getLogger("sparkvm.vm")


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


def parse_disk_to_mib(disk: int | str) -> int:
    return parse_memory_to_mib(disk)


class SparkVM:
    def __init__(
        self,
        *,
        vcpu: int = DEFAULT_VCPU,
        memory: int | str = DEFAULT_MEMORY,
        disk: int | str = "4G",
        timeout: float = DEFAULT_TIMEOUT_SEC,
        network: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config: SparkVMConfig = build_config(
            vcpu=vcpu,
            memory=memory,
            timeout=timeout,
            network=network,
        )
        configure_logging(home_dir=self.config.home_dir)
        self._env = validate_env_mapping(env)
        self._disk_mib = parse_disk_to_mib(disk)
        self._setup_timeout_sec = DEFAULT_SETUP_TIMEOUT_SEC
        self._run_timeout_sec = DEFAULT_RUN_TIMEOUT_SEC
        self._keep_rootfs_on_failure = True
        self._keep_disk_on_failure = True
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

    def run(self, rollout_id: str) -> VMResult:
        if not isinstance(rollout_id, str):
            raise TypeError("SparkVM.run expects a rollout id string.")
        run_started_at = time.monotonic()
        rollout_obj = self.resolve_rollout(rollout_id)
        selected_runtime = rollout_obj.runtime
        # assert_resource_capacity(
        #     home_dir=self.config.home_dir,
        #     vcpu=int(self.config.vcpu),
        #     memory_mib=self.config.memory_mib,
        #     disk_mib=self._disk_mib,
        # )
        machine_specs = {
            "vcpu": self.config.vcpu,
            "memory": self.config.memory_mib,
            "disk": self._disk_mib,
            "timeout": self.config.timeout_sec,
            "runtime": rollout_obj.runtime,
            "network": self.config.network_enabled,
        }

        self._setup.ensure_layout()

        vm_id = f"vm-{uuid4().hex[:12]}"
        worker_dir = self.config.workers_dir / vm_id
        ensure_dir(worker_dir, exist_ok=False)
        run_logger = self._create_run_logger(vm_id=vm_id)
        run_logger.info(
            "run_started rollout_id=%s rollout_name=%s runtime=%s worker_dir=%s",
            rollout_obj.id,
            rollout_obj.name,
            rollout_obj.runtime,
            worker_dir,
        )
        run_logger.info(
            "run_config vcpu=%s memory_mib=%s disk_mib=%s timeout_sec=%s network=%s env_keys=%s",
            self.config.vcpu,
            self.config.memory_mib,
            self._disk_mib,
            self.config.timeout_sec,
            self.config.network_enabled,
            sorted(self._env.keys()),
        )

        socket_path = worker_dir / "firecracker.sock"
        firecracker_log_path = worker_dir / "firecracker.log"
        worker_rootfs_path = worker_dir / "rootfs.ext4"
        execution_disk_path = worker_dir / "rollout.ext4"
        mount_base = worker_dir / "mnt"

        execution_disk = ExecutionDisk(
            rollout=rollout_obj,
            path=execution_disk_path,
            size_mb=self._disk_mib,
            mount_base=mount_base,
        )
        started_at = time.monotonic()
        firecracker: FirecrackerProcess | None = None
        network_config: NetworkConfig | None = None

        failure: Exception | None = None
        cleanup_failure_message: str | None = None
        final_result: VMResult | None = None

        try:
            run_logger.info("phase=worker_prepare status=begin")
            firecracker_bin = self._setup.firecracker_binary_path()
            self._setup.assert_kvm_available()
            runtime_image = self.resolve_runtime_image_for_rollout(rollout_obj, selected_runtime)
            selected_runtime = runtime_image.name
            self.assert_runtime_image_permissions(runtime_image)
            create_worker_rootfs(base_rootfs=runtime_image.rootfs_image, worker_rootfs=worker_rootfs_path)
            run_logger.info(
                "phase=worker_prepare status=ok firecracker_bin=%s runtime_image=%s worker_rootfs=%s",
                firecracker_bin,
                runtime_image.rootfs_image,
                worker_rootfs_path,
            )
            self.write_worker_state(
                path=worker_dir / "worker.json",
                vm_id=vm_id,
                rollout=rollout_obj,
                status="running",
                machine_specs=machine_specs,
            )

            if self.config.network_enabled:
                run_logger.info("phase=network_prepare status=begin")
                network_config = self._network.setup(vm_id)
                run_logger.info("phase=network_prepare status=ok tap=%s", network_config.tap_name)

            run_logger.info("phase=disk_prepare status=begin")
            runtime_files = self.runtime_execution_files(rollout=rollout_obj, network_config=network_config)
            execution_disk.copy_rollout(runtime_files=runtime_files if runtime_files else None)
            self.assert_worker_image_permissions(
                worker_rootfs=worker_rootfs_path,
                execution_disk_path=execution_disk_path,
            )
            run_logger.info("phase=disk_prepare status=ok execution_disk=%s", execution_disk_path)

            run_logger.info("phase=firecracker_start status=begin")
            firecracker = FirecrackerProcess(
                firecracker_bin=firecracker_bin,
                socket_path=socket_path,
                log_path=firecracker_log_path,
            )
            firecracker.start(startup_timeout_sec=min(5.0, self.config.timeout_sec))
            run_logger.info("phase=firecracker_start status=ok socket=%s", socket_path)

            api = FirecrackerAPIClient(socket_path)
            self.wait_for_firecracker_socket(api, firecracker, timeout_sec=self.config.timeout_sec)
            run_logger.info("phase=firecracker_config status=begin")
            self.configure_microvm(
                api=api,
                runtime_image=runtime_image,
                worker_rootfs_path=worker_rootfs_path,
                execution_disk_path=execution_disk_path,
            )
            if network_config is not None:
                api.attach_network(host_dev_name=network_config.tap_name, guest_mac=network_config.guest_mac)
            api.put("/actions", {"action_type": "InstanceStart"})
            run_logger.info("phase=firecracker_config status=ok vm_boot=started")

            try:
                firecracker.wait(timeout_sec=self.config.timeout_sec)
            except subprocess.TimeoutExpired:
                duration_ms = int((time.monotonic() - started_at) * 1000)
                failure = JobTimeoutError(
                    f"SparkVM run timed out after {self.config.timeout_sec:.2f}s before guest shutdown."
                )
                run_logger.error("phase=timeout status=failed duration_ms=%s", duration_ms)
                self.cleanup_process_socket(
                    firecracker=firecracker,
                    socket_path=socket_path,
                )
            else:
                duration_ms = int((time.monotonic() - started_at) * 1000)
                run_logger.info("phase=result_extract status=begin duration_ms=%s", duration_ms)
                result = execution_disk.read_result(
                    vm_id=vm_id,
                    duration_ms=duration_ms,
                    firecracker_log_path=firecracker_log_path,
                )
                result = replace(result, runtime=selected_runtime)
                result = self.annotate_oom(result=result, firecracker_log_path=firecracker_log_path)
                if result.passed:
                    run_logger.info("phase=result_extract status=ok result=passed exit_code=%s", result.exit_code)
                    self.write_worker_state(
                        path=worker_dir / "worker.json",
                        vm_id=vm_id,
                        rollout=rollout_obj,
                        status="passed",
                        machine_specs=machine_specs,
                        completed_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    )
                    self.cleanup_worker_on_completion(
                        worker_dir=worker_dir,
                        socket_path=socket_path,
                        firecracker=firecracker,
                        execution_disk=execution_disk,
                    )
                    if rollout_obj.delete_on_success:
                        try:
                            self._rollouts.delete_by_id(rollout_obj.id)
                            run_logger.info("phase=cleanup status=ok delete_on_success=true rollout_deleted=%s", rollout_obj.id)
                        except RolloutNotFoundError:
                            pass
                    final_result = result
                else:
                    run_logger.warning(
                        "phase=result_extract status=failed result_status=%s exit_code=%s",
                        result.status,
                        result.exit_code,
                    )
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
            run_logger.exception("phase=firecracker status=failed error=%s", exc)
            failure = self.classify_infrastructure_error(
                error=exc,
                vm_id=vm_id,
                worker_dir=worker_dir,
                socket_path=socket_path,
                firecracker_log_path=firecracker_log_path,
            )
        except SparkVMError as exc:
            run_logger.exception("phase=run status=failed sparkvm_error=%s", exc)
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
            run_logger.exception("phase=run status=failed unexpected_error=%s", exc)
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
                run_logger.exception("phase=network_cleanup status=failed error=%s", exc)

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
            run_logger.error(
                "run_failed rollout_id=%s vm_id=%s duration_ms=%s error_type=%s error=%s",
                rollout_obj.id,
                vm_id,
                int((time.monotonic() - run_started_at) * 1000),
                type(failure).__name__,
                failure,
            )
            self._close_run_logger(run_logger)
            raise failure

        assert final_result is not None
        run_logger.info(
            "run_finished rollout_id=%s vm_id=%s status=%s exit_code=%s duration_ms=%s",
            rollout_obj.id,
            vm_id,
            final_result.status,
            final_result.exit_code,
            int((time.monotonic() - run_started_at) * 1000),
        )
        self._close_run_logger(run_logger)
        return final_result

    def _create_run_logger(self, *, vm_id: str) -> logging.Logger:
        logger = logging.getLogger(f"sparkvm.vm.run.{vm_id}")
        logger.setLevel(logging.INFO)
        logger.propagate = True
        logger.handlers.clear()
        LOGGER.info("run_logger_ready vm_id=%s", vm_id)
        return logger

    def _close_run_logger(self, logger: logging.Logger) -> None:
        for handler in list(logger.handlers):
            try:
                handler.flush()
                handler.close()
            finally:
                logger.removeHandler(handler)

    def runtime_execution_files(self, *, rollout: Rollout, network_config: NetworkConfig | None) -> dict[str, str]:
        del rollout
        runtime_env = render_runtime_config_file(
            setup_timeout_sec=self._setup_timeout_sec,
            run_timeout_sec=self._run_timeout_sec,
        )
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
        write_json_atomic(path, payload, pretty=True)

    def write_worker_state(
        self,
        *,
        path: Path,
        vm_id: str,
        rollout: Rollout,
        status: str,
        machine_specs: Mapping[str, Any],
        completed_at: str | None = None,
    ) -> None:
        created_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if path.exists():
            try:
                existing_payload = json.loads(read_text(path, encoding="utf-8"))
            except Exception:
                existing_payload = None
            if isinstance(existing_payload, dict):
                existing_created = existing_payload.get("created_at")
                if isinstance(existing_created, str) and existing_created.strip():
                    created_at = existing_created

        payload: dict[str, object] = {
            "id": vm_id,
            "rollout_id": rollout.id,
            "vcpu": int(machine_specs["vcpu"]),
            "memory": int(machine_specs["memory"]),
            "disk": int(machine_specs["disk"]),
            "timeout": float(machine_specs["timeout"]),
            "network": bool(machine_specs["network"]),
            "status": status,
            "created_at": created_at,
            "completed_at": completed_at,
        }
        self.write_worker_json(path=path, payload=payload)

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
        status = "timeout" if isinstance(error, JobTimeoutError) else "failed"

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
            return replace(result, oom_killed=True)
        if result.run is not None and result.run.exit_code == 137:
            return replace(result, oom_killed=True)
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
                secret_scrubbed = False
                secret_scrub_failed = True
                execution_disk_removed_reason = None
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
            terminal_status = "timeout" if result.status == "timeout" else "failed"
            self.write_worker_state(
                path=worker_dir / "worker.json",
                vm_id=vm_id,
                rollout=rollout,
                status=terminal_status,
                machine_specs=machine_specs,
                completed_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
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

        terminal_status = "timeout" if isinstance(error, JobTimeoutError) else "failed"
        self.write_worker_state(
            path=worker_dir / "worker.json",
            vm_id=vm_id,
            rollout=rollout,
            status=terminal_status,
            machine_specs=machine_specs,
            completed_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )
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

__all__ = [
    "SparkVM",
    "shell_quote",
    "render_env_file",
    "render_redact_sed_file",
    "redact_text",
    "redact_file_in_place",
    "scrub_sensitive_execution_files",
]
