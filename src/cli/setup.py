"""Managed SparkVM setup helpers and host diagnostics."""

from __future__ import annotations

import os
import platform
import pwd
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sparkvm.config import SparkVMConfig, resolve_home_dir
from sparkvm.commands import run_checked
from sparkvm.errors import FirecrackerBinaryNotInstalled, KVMUnavailableError, SparkVMSetupError
from sparkvm.fsops import ensure_dir, read_json, write_json_atomic
from sparkvm.runtime_store import RuntimeRecord, list_runtime_records
from sparkvm.utils import has_cap_net_admin, has_network_privileges as network_privileges_ok

from sparkvm.constants import (
    ARCH_ALIASES as _ARCH_ALIASES,
    DOCTOR_NETWORK_TOOLS as _DOCTOR_NETWORK_TOOLS,
    DOCTOR_TOOLS as _DOCTOR_TOOLS,
    FIRECRACKER_VERSION,
    KERNEL_FILENAME,
    KERNEL_URLS as _KERNEL_URLS,
    REQUIRED_SETUP_TOOLS as _REQUIRED_SETUP_TOOLS,
    SUPPORTED_ARCHES,
)


@dataclass(frozen=True)
class SparkVMPaths:
    home_dir: Path
    bin_dir: Path
    image_dir: Path
    workers_dir: Path
    cache_dir: Path
    rollouts_dir: Path
    firecracker_bin: Path
    kvm_link: Path
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
    network_host_tools_ok: bool
    network_privileges_ok: bool
    available_runtimes: list[RuntimeRecord]
    image_dir_free_mb: int | None


def emit_progress(progress: Callable[[str], None] | None, message: str) -> None:
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
        kvm_link=bin_dir / "kvm",
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
        kvm_link=config.bin_dir / "kvm",
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


def check_kvm_access(kvm_path: Path | None = None) -> None:
    kvm = kvm_path if kvm_path is not None else Path("/dev/kvm")
    if not kvm.exists():
        raise KVMUnavailableError(f"{kvm} was not found. KVM is required for SparkVM.")
    if not os.access(kvm, os.R_OK | os.W_OK):
        raise KVMUnavailableError(
            f"Current user cannot access {kvm}. Add the user to the kvm group or run with proper permissions."
        )


def ensure_kvm_link(paths: SparkVMPaths) -> Path:
    ensure_directories(paths)
    host_kvm = Path("/dev/kvm")
    kvm_link = paths.kvm_link

    if kvm_link.exists() or kvm_link.is_symlink():
        if kvm_link.is_symlink() and kvm_link.resolve() == host_kvm:
            return kvm_link
        kvm_link.unlink()

    os.symlink(host_kvm, kvm_link)
    return kvm_link


def host_tool_status() -> dict[str, bool]:
    tools = set(_REQUIRED_SETUP_TOOLS) | set(_DOCTOR_TOOLS)
    return {tool: shutil.which(tool) is not None for tool in sorted(tools)}





def require_setup_tools() -> None:
    status = host_tool_status()
    missing = [tool for tool in _REQUIRED_SETUP_TOOLS if not status[tool]]
    if missing:
        raise SparkVMSetupError(f"Missing required host tools: {', '.join(missing)}")


def download_with_curl(url: str, out_path: Path) -> None:
    run_checked(["curl", "-fL", url, "-o", str(out_path)], error_factory=SparkVMSetupError)


def firecracker_release_url(arch: str) -> str:
    return (
        "https://github.com/firecracker-microvm/firecracker/releases/download/"
        f"{FIRECRACKER_VERSION}/firecracker-{FIRECRACKER_VERSION}-{arch}.tgz"
    )


def extract_firecracker_binary(extracted_dir: Path) -> Path:
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
        emit_progress(progress, f"Firecracker binary already present: {existing}")
        return existing

    ensure_directories(paths)
    arch = normalize_arch()
    archive_url = firecracker_release_url(arch)
    emit_progress(progress, f"Installing Firecracker {FIRECRACKER_VERSION} for {arch}...")

    try:
        with tempfile.TemporaryDirectory(prefix="sparkvm-firecracker-") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            archive_path = tmp_dir / "firecracker.tgz"
            download_with_curl(archive_url, archive_path)
            run_checked(["tar", "-xzf", str(archive_path), "-C", str(tmp_dir)], error_factory=SparkVMSetupError)
            extracted = extract_firecracker_binary(tmp_dir)
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

    emit_progress(progress, f"Firecracker installed: {existing}")
    return existing


def ensure_kernel_image(
    paths: SparkVMPaths,
    force: bool = False,
    *,
    progress: Callable[[str], None] | None = None,
) -> Path:
    if paths.kernel_image.exists() and not force:
        emit_progress(progress, f"Kernel image already present: {paths.kernel_image}")
        return paths.kernel_image

    ensure_directories(paths)
    arch = normalize_arch()
    url = _KERNEL_URLS[arch]
    emit_progress(progress, f"Downloading kernel image for {arch}...")

    try:
        download_with_curl(url, paths.kernel_image)
    except Exception as exc:
        raise SparkVMSetupError(
            "Could not download the managed kernel image. Run `sparkvm setup` after checking connectivity."
        ) from exc

    emit_progress(progress, f"Kernel image ready: {paths.kernel_image}")
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


def initialize_rollouts_metadata(paths: SparkVMPaths) -> None:
    ensure_dir(paths.rollouts_dir, exist_ok=True)
    metadata_path = paths.rollouts_dir / "metadata.json"
    if metadata_path.exists():
        try:
            payload = read_json(metadata_path, encoding="utf-8")
            if isinstance(payload, dict) and isinstance(payload.get("rollouts", {}), (dict, list)):
                return
        except Exception:
            pass

    write_json_atomic(metadata_path, {"version": 1, "rollouts": {}}, encoding="utf-8", pretty=True)


def run_setup(
    paths: SparkVMPaths,
    *,
    force: bool = False,
    owner: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> None:
    emit_progress(progress, f"Preparing SparkVM directories under: {paths.home_dir}")
    ensure_directories(paths)
    emit_progress(progress, "Checking Linux host compatibility")
    check_linux_host()
    arch = normalize_arch()
    emit_progress(progress, f"Detected architecture: {arch}")
    emit_progress(progress, "Checking required host tools")
    require_setup_tools()

    firecracker_bin = ensure_firecracker_binary(paths, force=force, progress=progress)
    emit_progress(progress, f"Firecracker ready: {firecracker_bin}")

    ensure_kvm_link(paths)
    emit_progress(progress, f"KVM link ready: {paths.kvm_link}")

    ensure_kernel_image(paths, force=force, progress=progress)
    emit_progress(progress, f"Kernel ready: {paths.kernel_image}")

    initialize_rollouts_metadata(paths)

    if owner is not None:
        if os.geteuid() != 0:
            raise SparkVMSetupError("--owner can only be used when running as root.")
        emit_progress(progress, f"Adjusting ownership to user: {owner}")
        chown_tree(paths.home_dir, owner)

    emit_progress(progress, "Setup finished")


def read_firecracker_version(firecracker_bin: Path) -> str | None:
    if not firecracker_bin.exists() or not os.access(firecracker_bin, os.X_OK):
        return None
    try:
        result = run_checked(
            [str(firecracker_bin), "--version"],
            error_factory=SparkVMSetupError,
            allow_unlisted=True,
        )
    except Exception:
        return None

    line = (result.stdout or result.stderr).strip()
    return line or None


def doctor_status(paths: SparkVMPaths) -> DoctorStatus:
    tools = host_tool_status()
    firecracker_found = paths.firecracker_bin.exists() and os.access(paths.firecracker_bin, os.X_OK)

    try:
        kvm_probe = paths.kvm_link if paths.kvm_link.exists() else Path("/dev/kvm")
        check_kvm_access(kvm_probe)
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

    network_tools_ok = all(tools.get(tool, False) for tool in _DOCTOR_NETWORK_TOOLS)
    network_privileges_ok = network_privileges_ok()

    return DoctorStatus(
        paths=paths,
        home_exists=paths.home_dir.exists(),
        images_dir_exists=paths.image_dir.exists(),
        host_os_ok=platform.system().lower() == "linux",
        arch_ok=arch_ok,
        arch_value=arch_value,
        firecracker_found=firecracker_found,
        firecracker_version=read_firecracker_version(paths.firecracker_bin),
        kvm_accessible=kvm_ok,
        kernel_found=paths.kernel_image.exists(),
        host_tools=tools,
        network_host_tools_ok=network_tools_ok,
        network_privileges_ok=network_privileges_ok,
        available_runtimes=list_runtime_records(paths.image_dir),
        image_dir_free_mb=free_mb,
    )


def flag(ok: bool) -> str:
    return "OK" if ok else "MISSING"


def format_doctor_report(status: DoctorStatus) -> str:
    lines: list[str] = []
    lines.append(f"SparkVM home: {status.paths.home_dir}")
    lines.append(f"SparkVM home exists: {flag(status.home_exists)}")
    lines.append(f"Host OS Linux: {flag(status.host_os_ok)}")
    lines.append(f"Architecture ({status.arch_value}): {flag(status.arch_ok)}")
    lines.append(f"KVM accessible: {flag(status.kvm_accessible)} ({status.paths.kvm_link} -> /dev/kvm)")
    lines.append(f"Firecracker binary: {flag(status.firecracker_found)} ({status.paths.firecracker_bin})")
    lines.append(f"Firecracker version: {status.firecracker_version or 'unavailable'}")
    lines.append(f"Kernel image: {flag(status.kernel_found)} ({status.paths.kernel_image})")
    lines.append(f"Images directory: {flag(status.images_dir_exists)} ({status.paths.image_dir})")
    if status.image_dir_free_mb is not None:
        lines.append(f"Free space (images): {status.image_dir_free_mb} MiB")

    lines.append("Host tools:")
    for tool in _DOCTOR_TOOLS:
        marker = flag(status.host_tools.get(tool, False))
        lines.append(f"  - {tool}: {marker}")

    lines.append("Network support:")
    lines.append(f"  host tools: {'OK' if status.network_host_tools_ok else 'MISSING'}")
    lines.append(f"  privileges: {'OK' if status.network_privileges_ok else 'NEEDS ROOT'}")

    lines.append("Available runtimes:")
    if not status.available_runtimes:
        lines.append("  no runtime images found. Create a Dockerfile rollout first.")
    else:
        for runtime in status.available_runtimes:
            source = runtime.source_image or "unknown"
            runtime_line = f"  - {runtime.name}  source={source}"
            if runtime.ip_command_present is False:
                runtime_line += "  warning=missing ip command (network=True may fail)"
            lines.append(runtime_line)
            if not os.access(runtime.rootfs, os.R_OK):
                lines.append(f"    Runtime image is not readable by current user: {runtime.rootfs}.")
                lines.append("    Fix:")
                lines.append(f"      sudo chown $USER:$USER {runtime.rootfs}")
                lines.append(f"      chmod 0644 {runtime.rootfs}")

    return "\n".join(lines)


def run_setup_command(home_dir: str | None, force: bool, *, owner: str | None = None) -> int:
    paths = get_sparkvm_paths(home_dir)
    print(f"Using SparkVM home: {paths.home_dir}", flush=True)

    progress = lambda message: print(f"[setup] {message}", flush=True)
    print("Running base setup checks and managed asset install...", flush=True)
    run_setup(paths, force=force, owner=owner, progress=progress)
    print("SparkVM setup complete.")
    print(f"Firecracker: {paths.firecracker_bin}")
    print(f"KVM link: {paths.kvm_link}")
    print(f"Kernel: {paths.kernel_image}")
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
        probe = self.paths.kvm_link if self.paths.kvm_link.exists() else Path("/dev/kvm")
        check_kvm_access(probe)


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
