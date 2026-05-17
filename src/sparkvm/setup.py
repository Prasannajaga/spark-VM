"""Managed SparkVM setup helpers and host diagnostics."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import SparkVMConfig, resolve_home_dir
from .errors import FirecrackerBinaryNotInstalled, KVMUnavailableError, SparkVMSetupError
from .runtimes.python import INIT_TEMPLATE, PYTHON_RUNTIME_ID

FIRECRACKER_VERSION = "v1.15.1"
PYTHON_DOCKER_IMAGE = "python:3.12-slim"
PYTHON_ROOTFS_FILENAME = "python-3.12-rootfs.ext4"
KERNEL_FILENAME = "vmlinux"
REQUIRED_HOST_TOOLS = ("curl", "tar", "dd", "mkfs.ext4", "debugfs")
OPTIONAL_HOST_TOOLS = ("docker",)
SUPPORTED_ARCHES = {"x86_64", "aarch64"}

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
    firecracker_bin: Path
    kernel_image: Path
    python_rootfs: Path


@dataclass(frozen=True)
class DoctorStatus:
    paths: SparkVMPaths
    firecracker_found: bool
    firecracker_version: str | None
    kvm_accessible: bool
    kernel_found: bool
    python_rootfs_found: bool
    docker_available: bool
    host_tools: dict[str, bool]


def _emit_progress(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def get_sparkvm_paths(home_dir: str | Path | None = None) -> SparkVMPaths:
    resolved_home = resolve_home_dir(home_dir)
    bin_dir = resolved_home / "bin"
    image_dir = resolved_home / "images"
    workers_dir = resolved_home / "workers"
    cache_dir = resolved_home / "cache"

    return SparkVMPaths(
        home_dir=resolved_home,
        bin_dir=bin_dir,
        image_dir=image_dir,
        workers_dir=workers_dir,
        cache_dir=cache_dir,
        firecracker_bin=bin_dir / "firecracker",
        kernel_image=image_dir / KERNEL_FILENAME,
        python_rootfs=image_dir / PYTHON_ROOTFS_FILENAME,
    )


def paths_from_config(config: SparkVMConfig) -> SparkVMPaths:
    return SparkVMPaths(
        home_dir=config.home_dir,
        bin_dir=config.bin_dir,
        image_dir=config.image_dir,
        workers_dir=config.workers_dir,
        cache_dir=config.cache_dir,
        firecracker_bin=config.bin_dir / "firecracker",
        kernel_image=config.image_dir / KERNEL_FILENAME,
        python_rootfs=config.image_dir / PYTHON_ROOTFS_FILENAME,
    )


def ensure_directories(paths: SparkVMPaths) -> None:
    for directory in (
        paths.home_dir,
        paths.bin_dir,
        paths.image_dir,
        paths.workers_dir,
        paths.cache_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)


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
    status: dict[str, bool] = {}
    for tool in (*REQUIRED_HOST_TOOLS, *OPTIONAL_HOST_TOOLS):
        status[tool] = shutil.which(tool) is not None
    return status


def require_host_tools(*, require_docker: bool = False) -> None:
    status = host_tool_status()
    missing = [tool for tool in REQUIRED_HOST_TOOLS if not status[tool]]
    if require_docker and not status["docker"]:
        missing.append("docker")

    if missing:
        missing_list = ", ".join(missing)
        raise SparkVMSetupError(f"Missing required host tools: {missing_list}.")


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
        rendered = " ".join(cmd)
        raise SparkVMSetupError(f"Command failed: {rendered}\n{detail}") from exc


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
            _emit_progress(progress, f"Downloading Firecracker archive from: {archive_url}")
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


def ensure_global_firecracker_command(
    firecracker_bin: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> Path | None:
    """Best-effort global command link for `firecracker`."""
    candidates = [
        Path("/usr/local/bin/firecracker"),
        Path.home() / ".local" / "bin" / "firecracker",
    ]

    for target in candidates:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue

        try:
            if target.exists() or target.is_symlink():
                try:
                    if target.resolve() == firecracker_bin.resolve():
                        _emit_progress(progress, f"Global firecracker command already linked: {target}")
                        return target
                except OSError:
                    pass
                target.unlink()

            target.symlink_to(firecracker_bin)
            _emit_progress(progress, f"Global firecracker command linked: {target} -> {firecracker_bin}")
            return target
        except OSError:
            continue

    _emit_progress(
        progress,
        "Could not create global firecracker command link. You may need sudo for /usr/local/bin "
        "or add ~/.local/bin to PATH.",
    )
    return None


def _docker_create(image: str) -> str:
    created = _run_checked(["docker", "create", image, "sh", "-lc", "true"])
    container_id = created.stdout.strip()
    if not container_id:
        raise SparkVMSetupError("Docker create did not return a container id.")
    return container_id


def _docker_export(container_id: str, out_tar: Path) -> None:
    _run_checked(["docker", "export", "-o", str(out_tar), container_id])


def _docker_rm(container_id: str) -> None:
    _run_checked(["docker", "rm", "-f", container_id])


def _extract_tar_to_dir(tar_path: Path, dest_dir: Path) -> None:
    _run_checked(["tar", "-xf", str(tar_path), "-C", str(dest_dir)])


def _verify_python_in_rootfs(rootfs_dir: Path) -> None:
    candidates = [
        rootfs_dir / "usr/bin/python3",
        rootfs_dir / "usr/local/bin/python3",
        rootfs_dir / "bin/python3",
    ]
    if not any(path.exists() for path in candidates):
        raise SparkVMSetupError(
            "Python runtime build failed: python3 was not found in exported root filesystem."
        )


def _write_init_script(rootfs_dir: Path) -> None:
    init_path = rootfs_dir / "init"
    init_path.write_text(INIT_TEMPLATE, encoding="utf-8")
    init_path.chmod(0o755)


def _estimate_rootfs_size_mib(rootfs_dir: Path) -> int:
    total_bytes = 0
    for entry in rootfs_dir.rglob("*"):
        if entry.is_file():
            try:
                total_bytes += entry.stat().st_size
            except OSError:
                continue

    total_mib = (total_bytes + (1024 * 1024 - 1)) // (1024 * 1024)
    # extra headroom for metadata and future small writes
    return max(1024, int(total_mib) + 256)


def _create_ext4_image(image_path: Path, size_mib: int, *, source_dir: Path | None = None) -> None:
    _run_checked([
        "dd",
        "if=/dev/zero",
        f"of={image_path}",
        "bs=1M",
        f"count={size_mib}",
        "status=none",
    ])
    mkfs_cmd = ["mkfs.ext4", "-F"]
    if source_dir is not None:
        mkfs_cmd.extend(["-d", str(source_dir)])
    mkfs_cmd.append(str(image_path))
    _run_checked(mkfs_cmd)


def _build_python_rootfs_image(
    paths: SparkVMPaths,
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    if paths.python_rootfs.exists() and not force:
        _emit_progress(progress, f"Python rootfs already present: {paths.python_rootfs}")
        return paths.python_rootfs

    ensure_directories(paths)
    require_host_tools(require_docker=True)
    _emit_progress(progress, f"Building Python rootfs from Docker image: {PYTHON_DOCKER_IMAGE}")

    container_id = ""
    try:
        with tempfile.TemporaryDirectory(prefix="sparkvm-python-rootfs-") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            export_tar = tmp_dir / "rootfs.tar"
            export_root = tmp_dir / "exported-rootfs"
            export_root.mkdir(parents=True, exist_ok=True)
            _emit_progress(progress, f"Pulling Docker image: {PYTHON_DOCKER_IMAGE}")
            _run_checked(["docker", "pull", PYTHON_DOCKER_IMAGE])
            container_id = _docker_create(PYTHON_DOCKER_IMAGE)
            _emit_progress(progress, f"Exporting container filesystem: {container_id}")
            _docker_export(container_id, export_tar)
            _docker_rm(container_id)
            container_id = ""

            _extract_tar_to_dir(export_tar, export_root)
            _verify_python_in_rootfs(export_root)
            _write_init_script(export_root)

            size_mib = _estimate_rootfs_size_mib(export_root)
            image_tmp = tmp_dir / "python-rootfs.ext4"
            _emit_progress(progress, f"Creating ext4 image ({size_mib} MiB) and populating root filesystem")
            try:
                _create_ext4_image(image_tmp, size_mib, source_dir=export_root)
            except SparkVMSetupError as exc:
                detail = str(exc).lower()
                if "invalid option" in detail and "-d" in detail:
                    raise SparkVMSetupError(
                        "mkfs.ext4 on this host does not support '-d' for populating a filesystem image. "
                        "Install e2fsprogs with mkfs.ext4 '-d' support."
                    ) from exc
                raise

            shutil.move(str(image_tmp), paths.python_rootfs)
            _emit_progress(progress, f"Python rootfs ready: {paths.python_rootfs}")
    finally:
        if container_id:
            try:
                _docker_rm(container_id)
            except SparkVMSetupError:
                pass

    return paths.python_rootfs


def run_setup(
    paths: SparkVMPaths,
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> None:
    _emit_progress(progress, f"Preparing SparkVM directories under: {paths.home_dir}")
    ensure_directories(paths)
    _emit_progress(progress, "Checking Linux host compatibility")
    check_linux_host()
    arch = normalize_arch()
    _emit_progress(progress, f"Detected architecture: {arch}")
    _emit_progress(progress, "Checking KVM access")
    check_kvm_access()
    _emit_progress(progress, "Checking required host tools")
    require_host_tools(require_docker=False)
    firecracker_bin = ensure_firecracker_binary(paths, force=force, progress=progress)
    ensure_global_firecracker_command(firecracker_bin, progress=progress)
    ensure_kernel_image(paths, force=force, progress=progress)
    _emit_progress(progress, "Base setup finished")


def run_setup_python(
    paths: SparkVMPaths,
    *,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
) -> Path:
    run_setup(paths, force=force, progress=progress)
    return _build_python_rootfs_image(paths, force=force, progress=progress)


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

    return DoctorStatus(
        paths=paths,
        firecracker_found=firecracker_found,
        firecracker_version=_read_firecracker_version(paths.firecracker_bin),
        kvm_accessible=kvm_ok,
        kernel_found=paths.kernel_image.exists(),
        python_rootfs_found=paths.python_rootfs.exists(),
        docker_available=tools["docker"],
        host_tools=tools,
    )


def format_doctor_report(status: DoctorStatus) -> str:
    lines: list[str] = []
    lines.append(f"SparkVM home: {status.paths.home_dir}")
    lines.append(
        f"Firecracker binary: {'found' if status.firecracker_found else 'missing'} ({status.paths.firecracker_bin})"
    )
    lines.append(f"Firecracker version: {status.firecracker_version or 'unavailable'}")
    lines.append(f"KVM accessible: {'yes' if status.kvm_accessible else 'no'}")
    lines.append(f"Kernel image: {'found' if status.kernel_found else 'missing'} ({status.paths.kernel_image})")
    lines.append(
        f"Python rootfs: {'found' if status.python_rootfs_found else 'missing'} ({status.paths.python_rootfs})"
    )
    lines.append(f"Docker available: {'yes' if status.docker_available else 'no'}")
    lines.append("Host tools:")
    for tool in (*REQUIRED_HOST_TOOLS, *OPTIONAL_HOST_TOOLS):
        lines.append(f"  - {tool}: {'ok' if status.host_tools.get(tool) else 'missing'}")

    return "\n".join(lines)


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
    "PYTHON_DOCKER_IMAGE",
    "PYTHON_RUNTIME_ID",
    "SparkVMPaths",
    "DoctorStatus",
    "get_sparkvm_paths",
    "paths_from_config",
    "ensure_directories",
    "check_linux_host",
    "normalize_arch",
    "check_kvm_access",
    "host_tool_status",
    "require_host_tools",
    "ensure_firecracker_binary",
    "ensure_global_firecracker_command",
    "ensure_kernel_image",
    "run_setup",
    "run_setup_python",
    "doctor_status",
    "format_doctor_report",
    "ManagedSetup",
]
