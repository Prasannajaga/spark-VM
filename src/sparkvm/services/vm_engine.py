from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Protocol

from .._submodules import ensure_crackersdk_importable

ensure_crackersdk_importable()

from crackersdk import Jailer, JailerConfig as SDKJailerConfig, crackerVM
from crackersdk.errors import (
    FirecrackerAPIError as SDKAPIError,
    FirecrackerError as SDKFirecrackerError,
    FirecrackerProcessError as SDKProcessError,
    FirecrackerStateError as SDKStateError,
    FirecrackerTimeoutError as SDKTimeoutError,
)

from ..core.errors import (
    FirecrackerAPIError,
    FirecrackerBootError,
    FirecrackerProcessError,
    JobTimeoutError,
    SparkVMError,
)
from ..core.fsops import read_text


from ..core.constants import JAILED_EXECUTION_DISK_PATH, DEFAULT_JAILER_ROOT

JailerConfig = SDKJailerConfig


class _Launcher(Protocol):
    def build_command(self) -> list[str]:
        ...


@dataclass(frozen=True)
class NetworkInterfaceSpec:
    tap_name: str
    guest_mac: str


@dataclass(frozen=True)
class VMEngineConfig:
    firecracker_bin: Path
    socket_path: Path
    log_path: Path
    kernel_path: Path
    rootfs_path: Path
    execution_disk_path: Path
    vcpu: int
    memory_mib: int
    boot_args: str
    namespace_name: str | None
    network_iface: NetworkInterfaceSpec | None = None
    secure: bool = True
    jailer_config: JailerConfig | None = None


class _NamespaceLauncher:
    def __init__(self, *, namespace_name: str, inner: _Launcher) -> None:
        self._namespace_name = namespace_name
        self._inner = inner

    def build_command(self) -> list[str]:
        return ["ip", "netns", "exec", self._namespace_name, *self._inner.build_command()]


class VMEngine:
    def __init__(self, config: VMEngineConfig) -> None:
        self._config = config
        self._vm = crackerVM(
            binary=str(config.firecracker_bin),
            socket_path=str(config.socket_path),
            log_path=str(config.log_path),
            kernel_path=str(config.kernel_path),
            rootfs_path=str(config.rootfs_path),
            boot_args=config.boot_args,
            namespace_name=config.namespace_name,
            workdir=str(config.socket_path.parent),
        )
        try:
            self._jailer_config = self._resolve_jailer_config(config) if config.secure else None
            self._jailer = self._create_jailer(self._jailer_config)
        except (FileNotFoundError, ValueError) as exc:
            raise FirecrackerProcessError(
                "Secure SparkVM requires a valid Firecracker jailer binary. "
                "Install jailer next to the managed firecracker binary, put it on PATH, "
                "or pass secure=False."
            ) from exc
        self._secure_host_execution_disk_path: Path | None = None
        self._secure_artifacts_synced = False

    def start(self, startup_timeout_sec: float = 5.0) -> None:
        try:
            control = self._control()
            self._prepare_secure_runtime()
            control.start()
            control.wait_until_ready(
                timeout=startup_timeout_sec,
                poll_interval=0.05,
            )
        except SDKFirecrackerError as exc:
            raise self._translate_error(exc) from exc

    def configure(self) -> None:
        try:
            control = self._control()
            control.configure_logger(
                log_path=self._firecracker_process_log_path(),
            )
            control.boot_source(
                kernel_image_path=self._kernel_image_path(),
                boot_args=self._config.boot_args,
            )
            control.machine(
                vcpu_count=self._config.vcpu,
                mem_size_mib=self._config.memory_mib,
                smt=False,
            )
            entropy = control.entropy()
            if not entropy.success:
                raise SDKAPIError(entropy.error or "Failed to configure entropy")
            control.root_drive(path=self._rootfs_path())
            control.drive(
                drive_id="job",
                path=self._execution_disk_path(),
            )
            if self._config.network_iface is not None:
                network = control.network(
                    iface_id="eth0",
                    host_dev_name=self._config.network_iface.tap_name,
                    guest_mac=self._config.network_iface.guest_mac,
                )
                if not network.success:
                    raise SDKAPIError(network.error or "Failed to configure network interface")
        except SDKFirecrackerError as exc:
            raise self._translate_error(exc) from exc

    def boot(self) -> None:
        try:
            self._control().boot()
        except SDKFirecrackerError as exc:
            raise self._translate_error(exc) from exc

    def wait(self, timeout_sec: float) -> int:
        try:
            exit_code = self._control().wait(timeout=timeout_sec)
            self._sync_secure_artifacts()
            self._cleanup_secure_jail()
            return exit_code
        except SDKTimeoutError as exc:
            raise JobTimeoutError(str(exc)) from exc
        except SDKFirecrackerError as exc:
            raise self._translate_error(exc) from exc

    def stop(self) -> None:
        try:
            self._control().stop()
        except Exception:
            pass
        self._sync_secure_artifacts()
        self._cleanup_secure_jail()

    def is_running(self) -> bool:
        return self._control().is_running()

    @property
    def socket_path(self) -> Path:
        if self._jailer is not None:
            try:
                return Path(self._jailer.host_socket_path)
            except Exception:
                pass
        return self._config.socket_path

    @property
    def log_path(self) -> Path:
        return self._config.log_path

    def read_log_tail(self, max_lines: int = 40) -> str:
        log_path = self._config.log_path
        if not log_path.exists():
            return ""
        try:
            lines = read_text(log_path, encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-max_lines:])

    def format_diagnostic(self, *, reason: str) -> str:
        parts = [reason, f"Socket path: {self.socket_path}"]
        parts.append(f"Check Firecracker log: {self._config.log_path}")
        tail = self.read_log_tail()
        if tail:
            parts.append("Firecracker log tail:")
            parts.append(tail)
        return "\n".join(parts)

    def _control(self):
        return self._jailer if self._jailer is not None else self._vm

    def _create_jailer(self, config: JailerConfig | None) -> Jailer | None:
        if config is None:
            return None
        return Jailer(self._vm, config=config)

    def _prepare_secure_runtime(self) -> None:
        if self._jailer is None or self._jailer_config is None:
            return

        self._jailer.kernel_path
        self._prepare_secure_execution_disk(self._jailer_config)
        self._wrap_secure_launcher_for_namespace()

    def _prepare_secure_execution_disk(self, jailer_config: JailerConfig) -> None:
        if self._jailer is None:
            return
        root_dir = Path(self._jailer.host_log_path).parent.parent
        target = root_dir / JAILED_EXECUTION_DISK_PATH.removeprefix("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(self._config.execution_disk_path, target)
        except OSError:
            shutil.copy2(self._config.execution_disk_path, target)
        os.chown(target.parent, jailer_config.uid, jailer_config.gid)
        os.chown(target, jailer_config.uid, jailer_config.gid)
        self._secure_host_execution_disk_path = target
        self._secure_artifacts_synced = False

    def _wrap_secure_launcher_for_namespace(self) -> None:
        if self._jailer is None or self._config.namespace_name is None:
            return
        launcher = self._vm._proc.launcher
        if launcher is None or isinstance(launcher, _NamespaceLauncher):
            return
        self._vm._set_launcher(
            _NamespaceLauncher(namespace_name=self._config.namespace_name, inner=launcher)
        )

    def _firecracker_process_log_path(self) -> str:
        if self._jailer is not None:
            return self._jailer.log_path
        return str(self._config.log_path.resolve())

    def _kernel_image_path(self) -> str:
        if self._jailer is not None:
            return self._jailer.kernel_path
        return str(self._config.kernel_path)

    def _rootfs_path(self) -> str:
        if self._jailer is not None:
            return self._jailer.rootfs_path
        return str(self._config.rootfs_path)

    def _execution_disk_path(self) -> str:
        if self._jailer is not None:
            return JAILED_EXECUTION_DISK_PATH
        return str(self._config.execution_disk_path)

    def _sync_secure_artifacts(self) -> None:
        if self._jailer is None or self._secure_artifacts_synced:
            return
        try:
            host_log_path = Path(self._jailer.host_log_path)
            if host_log_path.exists() and host_log_path != self._config.log_path:
                shutil.copy2(host_log_path, self._config.log_path)
        except Exception:
            pass
        try:
            host_disk_path = self._secure_host_execution_disk_path
            if host_disk_path is not None and host_disk_path.exists():
                if host_disk_path.stat().st_ino != self._config.execution_disk_path.stat().st_ino:
                    shutil.copy2(host_disk_path, self._config.execution_disk_path)
        except Exception:
            pass
        self._secure_artifacts_synced = True

    def _cleanup_secure_jail(self) -> None:
        if self._jailer is None or self._jailer_config is None:
            return
        if not self._jailer_config.cleanup_on_exit:
            return
        try:
            self._jailer.cleanup()
        except Exception:
            pass

    @classmethod
    def _resolve_jailer_config(cls, config: VMEngineConfig) -> JailerConfig:
        if config.jailer_config is not None:
            return config.jailer_config

        uid, gid = cls._default_jailer_identity()
        chroot_base_dir = cls._default_jailer_root()
        return JailerConfig(
            jailer_binary=str(cls._default_jailer_binary(config.firecracker_bin)),
            jail_id=cls._default_jail_id(config.socket_path),
            uid=uid,
            gid=gid,
            chroot_base_dir=str(chroot_base_dir),
            cleanup_on_exit=True,
        )

    @staticmethod
    def _default_jailer_root() -> Path:
        env_root = os.getenv("JAILER_ROOT")
        if env_root is not None and env_root.strip():
            return Path(env_root).expanduser()
        return Path(DEFAULT_JAILER_ROOT)

    @staticmethod
    def _default_jailer_binary(firecracker_bin: Path) -> Path:
        managed = firecracker_bin.with_name("jailer")
        if managed.exists() and os.access(managed, os.X_OK):
            return managed
        discovered = shutil.which("jailer")
        if discovered is not None:
            return Path(discovered)
        return managed

    @staticmethod
    def _default_jail_id(socket_path: Path) -> str:
        raw = socket_path.parent.name or socket_path.stem or "sparkvm"
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in raw)
        return safe.strip(".-_") or "sparkvm"

    @staticmethod
    def _default_jailer_identity() -> tuple[int, int]:
        uid_raw = os.getenv("SUDO_UID")
        gid_raw = os.getenv("SUDO_GID")
        if os.geteuid() == 0 and uid_raw and gid_raw:
            try:
                return int(uid_raw), int(gid_raw)
            except ValueError:
                pass
        return os.getuid(), os.getgid()

    def _translate_error(self, exc: SDKFirecrackerError) -> SparkVMError:
        if isinstance(exc, SDKTimeoutError):
            return JobTimeoutError(str(exc))
        if isinstance(exc, SDKProcessError):
            return FirecrackerProcessError(str(exc))
        if isinstance(exc, SDKAPIError):
            return FirecrackerAPIError(str(exc))
        if isinstance(exc, SDKStateError):
            return FirecrackerBootError(str(exc))
        return SparkVMError(str(exc))


__all__ = ["JailerConfig", "NetworkInterfaceSpec", "VMEngine", "VMEngineConfig"]
