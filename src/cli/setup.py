"""Managed SparkVM setup helpers and host diagnostics."""

from __future__ import annotations

import os
import platform
import pwd
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sparkvm.config import SparkVMConfig, resolve_home_dir
from sparkvm.errors import FirecrackerBinaryNotInstalled, KVMUnavailableError, SparkVMSetupError
from sparkvm.fsops import ensure_dir, read_json, write_json_atomic
from sparkvm.runtime_store import RuntimeRecord, list_runtime_records

FIRECRACKER_VERSION = "v1.15.1"
KERNEL_FILENAME = "vmlinux"
SUPPORTED_ARCHES = {"x86_64", "aarch64"}
_REQUIRED_SETUP_TOOLS = ("curl", "tar")
_DOCTOR_TOOLS = ("docker", "dd", "mkfs.ext4", "mount", "umount", "debugfs")

_ARCH_ALIASES = {
    "amd64": "x86_64",
    "arm64": "aarch64",
}

_KERNEL_URLS = {
    "x86_64": "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/kernels/vmlinux.bin",
    "aarch64": "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/aarch64/kernels/vmlinux.bin",
}


@dataclass(frozen=True)
class SparkVMPaths:
    home_dir: Path
    bin_dir: Path
    image_dir: Path
    workers_dir: Path
    cache_dir: Path
    rollouts_dir: Path
    firecracker_bin: Path
    kernel_image: Path


@dataclass(frozen=True)
class DoctorStatus:
    paths: SparkVMPaths
    home_exists: bool
    images_dir_exists: bool
    host_os_ok: bool
    arch_ok: bool
    arch_value: str
    firecracker_found: bool
    firecracker_version: str | None
    kvm_accessible: bool
    kernel_found: bool
    host_tools: dict[str, bool]
    available_runtimes: list[RuntimeRecord]
    image_dir_free_mb: int | None


def _emit_progress(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def get_sparkvm_paths(home_dir: str | Path | None = None) -> SparkVMPaths:
    resolved_home = resolve_home_dir(home_dir)
    bin_dir = resolved_home / "bin"
    image_dir = resolved_home / "images"
    workers_dir = resolved_home / "workers"
    cache_dir = resolved_home / "cache"
    rollouts_dir = resolved_home / "rollouts"

    return SparkVMPaths(
        home_dir=resolved_home,
        bin_dir=bin_dir,
        image_dir=image_dir,
        workers_dir=workers_dir,
        cache_dir=cache_dir,
        rollouts_dir=rollouts_dir,
        firecracker_bin=bin_dir / "firecracker",
        kernel_image=image_dir / KERNEL_FILENAME,
    )


def paths_from_config(config: SparkVMConfig) -> SparkVMPaths:
    return SparkVMPaths(
        home_dir=config.home_dir,
        bin_dir=config.bin_dir,
        image_dir=config.image_dir,
        workers_dir=config.workers_dir,
        cache_dir=config.cache_dir,
        rollouts_dir=config.home_dir / "rollouts",
        firecracker_bin=config.bin_dir / "firecracker",
        kernel_image=config.image_dir / KERNEL_FILENAME,
    )


def ensure_directories(paths: SparkVMPaths) -> None:
    for directory in (
        paths.home_dir,
        paths.bin_dir,
        paths.image_dir,
        paths.rollouts_dir,
        paths.workers_dir,
        paths.cache_dir,
    ):
        ensure_dir(directory, exist_ok=True)


def check_linux_host() -> None:
    if platform.system().lower() != "linux":
        raise SparkVMSetupError("SparkVM setup requires Linux host OS.")


def normalize_arch(machine: str | None = None) -> str:
    raw = (machine or platform.machine()).strip().lower()
    normalized = _ARCH_ALIASES.get(raw, raw)
    if normalized not in SUPPORTED_ARCHES:
        raise SparkVMSetupError(
            f"Unsupported architecture: {raw or '<unknown>'}. Supported architectures: x86_64, aarch64."
        )
    return normalized


def check_kvm_access() -> None:
    kvm = Path("/dev/kvm")
    if not kvm.exists():
        raise KVMUnavailableError("/dev/kvm was not found. KVM is required for SparkVM.")
    if not os.access(kvm, os.R_OK | os.W_OK):
        raise KVMUnavailableError(
            "Current user cannot access /dev/kvm. Add the user to the kvm group or run with proper permissions."
        )


def host_tool_status() -> dict[str, bool]:
    tools = set(_REQUIRED_SETUP_TOOLS) | set(_DOCTOR_TOOLS)
    return {tool: shutil.which(tool) is not None for tool in sorted(tools)}


def require_setup_tools() -> None:
    status = host_tool_status()
    missing = [tool for tool in _REQUIRED_SETUP_TOOLS if not status[tool]]
    if missing:
        raise SparkVMSetupError(f"Missing required host tools: {', '.join(missing)}")


def _run_checked(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            check=True,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise SparkVMSetupError(f"Required command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or "command failed"
        raise SparkVMSetupError(f"Command failed: {' '.join(cmd)}\n{detail}") from exc


def _download_with_curl(url: str, out_path: Path) -> None:
    _run_checked(["curl", "-fL", url, "-o", str(out_path)])


def _firecracker_release_url(arch: str) -> str:
    return (
        "https://github.com/firecracker-microvm/firecracker/releases/download/"
        f"{FIRECRACKER_VERSION}/firecracker-{FIRECRACKER_VERSION}-{arch}.tgz"
    )


def _extract_firecracker_binary(extracted_dir: Path) -> Path:
    for candidate in extracted_dir.rglob("firecracker-*"):
        if candidate.is_file() and candidate.name.endswith(("x86_64", "aarch64")):
            return candidate
    raise FirecrackerBinaryNotInstalled(
        "Downloaded Firecracker archive did not contain an expected firecracker binary."
    )


def ensure_firecracker_binary(
    paths: SparkVMPaths,
    force: bool = False,
    *,
    progress: Callable[[str], None] | None = None,
) -> Path:
    existing = paths.firecracker_bin
    if existing.exists() and os.access(existing, os.X_OK) and not force:
        _emit_progress(progress, f"Firecracker binary already present: {existing}")
        return existing

    ensure_directories(paths)
    arch = normalize_arch()
    archive_url = _firecracker_release_url(arch)
    _emit_progress(progress, f"Installing Firecracker {FIRECRACKER_VERSION} for {arch}...")

    try:
        with tempfile.TemporaryDirectory(prefix="sparkvm-firecracker-") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            archive_path = tmp_dir / "firecracker.tgz"
            _download_with_curl(archive_url, archive_path)
            _run_checked(["tar", "-xzf", str(archive_path), "-C", str(tmp_dir)])
            extracted = _extract_firecracker_binary(tmp_dir)
            shutil.copy2(extracted, existing)
            existing.chmod(0o755)
    except FirecrackerBinaryNotInstalled:
        raise
    except Exception as exc:
        raise FirecrackerBinaryNotInstalled(
            "Could not install Firecracker binary. Run `sparkvm setup` after verifying network access and host tools."
        ) from exc

    if not existing.exists() or not os.access(existing, os.X_OK):
        raise FirecrackerBinaryNotInstalled(
            "Firecracker install finished but binary is missing or not executable. Run `sparkvm setup`."
        )

    _emit_progress(progress, f"Firecracker installed: {existing}")
    return existing


def ensure_kernel_image(
    paths: SparkVMPaths,
    force: bool = False,
    *,
    progress: Callable[[str], None] | None = None,
) -> Path:
    if paths.kernel_image.exists() and not force:
        _emit_progress(progress, f"Kernel image already present: {paths.kernel_image}")
        return paths.kernel_image

    ensure_directories(paths)
    arch = normalize_arch()
    url = _KERNEL_URLS[arch]
    _emit_progress(progress, f"Downloading kernel image for {arch}...")

    try:
        _download_with_curl(url, paths.kernel_image)
    except Exception as exc:
        raise SparkVMSetupError(
            "Could not download the managed kernel image. Run `sparkvm setup` after checking connectivity."
        ) from exc

    _emit_progress(progress, f"Kernel image ready: {paths.kernel_image}")
    return paths.kernel_image


def chown_tree(path: Path, owner: str) -> None:
    user_info = pwd.getpwnam(owner)
    uid = user_info.pw_uid
    gid = user_info.pw_gid

    os.chown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        root_path = Path(root)
        os.chown(root_path, uid, gid)
        for dname in dirs:
            os.chown(root_path / dname, uid, gid)
        for fname in files:
            os.chown(root_path / fname, uid, gid)


def _initialize_rollouts_metadata(paths: SparkVMPaths) -> None:
    ensure_dir(paths.rollouts_dir, exist_ok=True)
    metadata_path = paths.rollouts_dir / "metadata.json"
    if metadata_path.exists():
        try:
            payload = read_json(metadata_path, encoding="utf-8")
            if isinstance(payload, dict) and isinstance(payload.get("rollouts", []), list):
                return
        except Exception:
            pass

    write_json_atomic(metadata_path, {"version": 1, "rollouts": []}, encoding="utf-8", pretty=True)


def run_setup(
    paths: SparkVMPaths,
    *,
    force: bool = False,
    owner: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> None:
    _emit_progress(progress, f"Preparing SparkVM directories under: {paths.home_dir}")
    ensure_directories(paths)
    _emit_progress(progress, "Checking Linux host compatibility")
    check_linux_host()
    arch = normalize_arch()
    _emit_progress(progress, f"Detected architecture: {arch}")
    _emit_progress(progress, "Checking required host tools")
    require_setup_tools()

    firecracker_bin = ensure_firecracker_binary(paths, force=force, progress=progress)
    _emit_progress(progress, f"Firecracker ready: {firecracker_bin}")

    ensure_kernel_image(paths, force=force, progress=progress)
    _emit_progress(progress, f"Kernel ready: {paths.kernel_image}")

    _initialize_rollouts_metadata(paths)

    if owner is not None:
        if os.geteuid() != 0:
            raise SparkVMSetupError("--owner can only be used when running as root.")
        _emit_progress(progress, f"Adjusting ownership to user: {owner}")
        chown_tree(paths.home_dir, owner)

    _emit_progress(progress, "Setup finished")


def _read_firecracker_version(firecracker_bin: Path) -> str | None:
    if not firecracker_bin.exists() or not os.access(firecracker_bin, os.X_OK):
        return None
    try:
        result = subprocess.run(
            [str(firecracker_bin), "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None

    line = (result.stdout or result.stderr).strip()
    return line or None


def doctor_status(paths: SparkVMPaths) -> DoctorStatus:
    tools = host_tool_status()
    firecracker_found = paths.firecracker_bin.exists() and os.access(paths.firecracker_bin, os.X_OK)

    try:
        check_kvm_access()
        kvm_ok = True
    except KVMUnavailableError:
        kvm_ok = False

    try:
        arch_value = normalize_arch()
        arch_ok = True
    except SparkVMSetupError:
        arch_value = platform.machine().strip().lower() or "<unknown>"
        arch_ok = False

    free_mb: int | None
    if paths.image_dir.exists():
        disk_usage = shutil.disk_usage(paths.image_dir)
        free_mb = int(disk_usage.free / (1024 * 1024))
    else:
        free_mb = None

    return DoctorStatus(
        paths=paths,
        home_exists=paths.home_dir.exists(),
        images_dir_exists=paths.image_dir.exists(),
        host_os_ok=platform.system().lower() == "linux",
        arch_ok=arch_ok,
        arch_value=arch_value,
        firecracker_found=firecracker_found,
        firecracker_version=_read_firecracker_version(paths.firecracker_bin),
        kvm_accessible=kvm_ok,
        kernel_found=paths.kernel_image.exists(),
        host_tools=tools,
        available_runtimes=list_runtime_records(paths.image_dir),
        image_dir_free_mb=free_mb,
    )


def _flag(ok: bool) -> str:
    return "OK" if ok else "MISSING"


def format_doctor_report(status: DoctorStatus) -> str:
    lines: list[str] = []
    lines.append(f"SparkVM home: {status.paths.home_dir}")
    lines.append(f"SparkVM home exists: {_flag(status.home_exists)}")
    lines.append(f"Host OS Linux: {_flag(status.host_os_ok)}")
    lines.append(f"Architecture ({status.arch_value}): {_flag(status.arch_ok)}")
    lines.append(f"KVM accessible: {_flag(status.kvm_accessible)}")
    lines.append(f"Firecracker binary: {_flag(status.firecracker_found)} ({status.paths.firecracker_bin})")
    lines.append(f"Firecracker version: {status.firecracker_version or 'unavailable'}")
    lines.append(f"Kernel image: {_flag(status.kernel_found)} ({status.paths.kernel_image})")
    lines.append(f"Images directory: {_flag(status.images_dir_exists)} ({status.paths.image_dir})")
    if status.image_dir_free_mb is not None:
        lines.append(f"Free space (images): {status.image_dir_free_mb} MiB")

    lines.append("Host tools:")
    for tool in _DOCTOR_TOOLS:
        marker = _flag(status.host_tools.get(tool, False))
        lines.append(f"  - {tool}: {marker}")

    lines.append("Available runtimes:")
    if not status.available_runtimes:
        lines.append("  no runtime images found. Run `sparkvm dockify python:3.12-slim`.")
    else:
        for runtime in status.available_runtimes:
            source = runtime.source_image or "unknown"
            lines.append(f"  - {runtime.name}  source={source}")

    return "\n".join(lines)


def run_setup_command(home_dir: str | None, runtime: str | None, force: bool, *, owner: str | None = None) -> int:
    paths = get_sparkvm_paths(home_dir)
    print(f"Using SparkVM home: {paths.home_dir}", flush=True)

    if runtime is not None and runtime.strip():
        print("Language-specific setup is no longer required. Use `sparkvm dockify <docker-image>`.", flush=True)

    progress = lambda message: print(f"[setup] {message}", flush=True)
    print("Running base setup checks and managed asset install...", flush=True)
    run_setup(paths, force=force, owner=owner, progress=progress)
    print("SparkVM setup complete.")
    print(f"Firecracker: {paths.firecracker_bin}")
    print(f"Kernel: {paths.kernel_image}")
    print(
        "Note: VM networking is not implemented yet; internet-dependent setup_cmd commands may fail inside the guest."
    )
    return 0


class ManagedSetup:
    """Compatibility wrapper used by SparkVM runtime orchestration."""

    def __init__(self, config: SparkVMConfig) -> None:
        self.config = config
        self.paths = paths_from_config(config)

    def ensure_layout(self) -> None:
        ensure_directories(self.paths)

    def firecracker_binary_path(self) -> Path:
        if self.paths.firecracker_bin.exists() and os.access(self.paths.firecracker_bin, os.X_OK):
            return self.paths.firecracker_bin
        raise FirecrackerBinaryNotInstalled(
            "Managed Firecracker binary not found. Run `sparkvm setup`."
        )

    def assert_kvm_available(self) -> None:
        check_kvm_access()


__all__ = [
    "FIRECRACKER_VERSION",
    "SparkVMPaths",
    "DoctorStatus",
    "get_sparkvm_paths",
    "paths_from_config",
    "ensure_directories",
    "check_linux_host",
    "normalize_arch",
    "check_kvm_access",
    "host_tool_status",
    "require_setup_tools",
    "ensure_firecracker_binary",
    "ensure_kernel_image",
    "chown_tree",
    "run_setup",
    "run_setup_command",
    "doctor_status",
    "format_doctor_report",
    "ManagedSetup",
]
