"""Managed SparkVM setup helpers and host diagnostics."""

from __future__ import annotations

import json
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
from sparkvm.fsops import ensure_dir, read_json, write_json_atomic, write_text
from sparkvm.runtimes.python import DEBIAN_MINBASE_IMAGE_ID, INIT_TEMPLATE

FIRECRACKER_VERSION = "v1.15.1"
DEBIAN_ROOTFS_FILENAME = "debian-rootfs.ext4"
KERNEL_FILENAME = "vmlinux"
REQUIRED_HOST_TOOLS = ("curl", "tar", "debootstrap", "dd", "mkfs.ext4", "mount", "umount", "chroot", "debugfs")
OPTIONAL_HOST_TOOLS = ("git", "sudo")
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
    rollouts_dir: Path
    firecracker_bin: Path
    kernel_image: Path
    debian_rootfs: Path


@dataclass(frozen=True)
class DoctorStatus:
    paths: SparkVMPaths
    host_os_ok: bool
    arch_ok: bool
    arch_value: str
    firecracker_found: bool
    firecracker_version: str | None
    kvm_accessible: bool
    kernel_found: bool
    debian_rootfs_found: bool
    host_tools: dict[str, bool]
    sudo_required: bool
    image_dir_free_mb: int


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
        debian_rootfs=image_dir / DEBIAN_ROOTFS_FILENAME,
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
        debian_rootfs=config.image_dir / DEBIAN_ROOTFS_FILENAME,
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
    status: dict[str, bool] = {}
    for tool in (*REQUIRED_HOST_TOOLS, *OPTIONAL_HOST_TOOLS):
        status[tool] = shutil.which(tool) is not None
    return status


def require_host_tools() -> None:
    status = host_tool_status()
    missing = [tool for tool in REQUIRED_HOST_TOOLS if not status[tool]]

    if missing:
        missing_list = ", ".join(missing)
        hint = ""
        if "debootstrap" in missing:
            hint = "\nInstall hint (Debian/Ubuntu): sudo apt-get install -y debootstrap"
        raise SparkVMSetupError(f"Missing required host tools: {missing_list}.{hint}")


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


def require_root_for_setup(home_dir: Path, owner: str | None) -> None:
    del owner  # Included for API clarity and future policy extensions.
    if os.geteuid() == 0:
        return

    current_user = pwd.getpwuid(os.getuid()).pw_name
    recommended_home = home_dir if home_dir.as_posix().strip() else Path.home() / ".sparkvm"
    raise SparkVMSetupError(
        "Building the Debian rootfs requires root privileges for debootstrap/mount/umount.\n"
        "Run:\n"
        f"  sudo sparkvm setup --home-dir {recommended_home} --owner {current_user}"
    )


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


def _assert_rootfs_basics(rootfs_dir: Path) -> None:
    checks: list[tuple[str, list[Path]]] = [
        ("/bin/sh", [rootfs_dir / "bin/sh"]),
        ("/init", [rootfs_dir / "init"]),
        ("poweroff", [rootfs_dir / "sbin/poweroff", rootfs_dir / "usr/sbin/poweroff", rootfs_dir / "bin/poweroff"]),
        ("mount", [rootfs_dir / "bin/mount", rootfs_dir / "usr/bin/mount"]),
    ]
    for label, candidates in checks:
        if not any(path.exists() for path in candidates):
            raise SparkVMSetupError(f"Debian rootfs missing required file/tool: {label}")


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


def build_debian_minbase_rootfs(
    *,
    output_path: Path,
    size_mb: int = 2048,
    suite: str = "bookworm",
    mirror: str = "http://deb.debian.org/debian",
    force: bool = False,
) -> Path:
    out = Path(output_path)
    if out.exists() and not force:
        return out

    cache_dir = out.parent.parent / "cache"
    ensure_dir(cache_dir, exist_ok=True)
    ensure_dir(out.parent, exist_ok=True)

    if os.geteuid() != 0:
        raise SparkVMSetupError(
            "Building the Debian rootfs requires root privileges for debootstrap/mount/umount."
        )
    mounted = False

    with tempfile.TemporaryDirectory(prefix="build-debian-", dir=str(cache_dir)) as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        rootfs_dir = tmp_dir / "rootfs"
        mount_dir = tmp_dir / "mnt"
        tmp_image = out.with_suffix(out.suffix + ".tmp")

        ensure_dir(rootfs_dir, exist_ok=True)
        ensure_dir(mount_dir, exist_ok=True)

        try:
            _run_checked(
                [
                    "debootstrap",
                    "--variant=minbase",
                    "--include=ca-certificates,curl,iproute2,procps,coreutils,util-linux",
                    suite,
                    str(rootfs_dir),
                    mirror,
                ]
            )

            init_path = rootfs_dir / "init"
            write_text(init_path, INIT_TEMPLATE, encoding="utf-8")
            init_path.chmod(0o755)
            _assert_rootfs_basics(rootfs_dir)

            if tmp_image.exists():
                tmp_image.unlink()
            _run_checked(["dd", "if=/dev/zero", f"of={tmp_image}", "bs=1M", f"count={size_mb}", "status=none"])
            _run_checked(["mkfs.ext4", "-F", str(tmp_image)])

            _run_checked(["mount", "-o", "loop", str(tmp_image), str(mount_dir)])
            mounted = True
            _run_checked(["cp", "-a", f"{rootfs_dir}/.", str(mount_dir)])
            _run_checked(["sync"])
            _run_checked(["umount", str(mount_dir)])
            mounted = False

            os.replace(tmp_image, out)
            return out
        except Exception as exc:
            if isinstance(exc, SparkVMSetupError):
                raise
            raise SparkVMSetupError(f"Failed to build Debian minbase rootfs at {out}: {exc}") from exc
        finally:
            if mounted:
                try:
                    _run_checked(["umount", str(mount_dir)])
                except Exception as unmount_exc:
                    raise SparkVMSetupError(f"Failed to unmount temporary rootfs mount {mount_dir}: {unmount_exc}")
            if tmp_image.exists():
                try:
                    tmp_image.unlink()
                except OSError:
                    pass


def run_setup(
    paths: SparkVMPaths,
    *,
    force: bool = False,
    owner: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> None:
    require_root_for_setup(paths.home_dir, owner)
    _emit_progress(progress, f"Preparing SparkVM directories under: {paths.home_dir}")
    ensure_directories(paths)
    _emit_progress(progress, "Checking Linux host compatibility")
    check_linux_host()
    arch = normalize_arch()
    _emit_progress(progress, f"Detected architecture: {arch}")
    _emit_progress(progress, "Checking KVM access")
    check_kvm_access()
    _emit_progress(progress, "Checking required host tools")
    require_host_tools()

    firecracker_bin = ensure_firecracker_binary(paths, force=force, progress=progress)
    _emit_progress(progress, f"Firecracker ready: {firecracker_bin}")

    ensure_kernel_image(paths, force=force, progress=progress)
    _emit_progress(progress, f"Kernel ready: {paths.kernel_image}")

    _emit_progress(progress, "Building/verifying Debian minbase rootfs")
    build_debian_minbase_rootfs(output_path=paths.debian_rootfs, force=force)
    _emit_progress(progress, f"Debian rootfs ready: {paths.debian_rootfs}")

    _initialize_rollouts_metadata(paths)

    if owner is not None:
        _emit_progress(progress, f"Adjusting ownership to user: {owner}")
        chown_tree(paths.home_dir, owner)

    _emit_progress(progress, "Base setup finished")


def run_setup_python(
    paths: SparkVMPaths,
    *,
    force: bool = False,
    owner: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    # Compatibility shim for legacy command usage.
    run_setup(paths, force=force, owner=owner, progress=progress)
    return paths.debian_rootfs


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

    disk_usage = shutil.disk_usage(paths.image_dir)
    free_mb = int(disk_usage.free / (1024 * 1024))

    return DoctorStatus(
        paths=paths,
        host_os_ok=platform.system().lower() == "linux",
        arch_ok=arch_ok,
        arch_value=arch_value,
        firecracker_found=firecracker_found,
        firecracker_version=_read_firecracker_version(paths.firecracker_bin),
        kvm_accessible=kvm_ok,
        kernel_found=paths.kernel_image.exists(),
        debian_rootfs_found=paths.debian_rootfs.exists(),
        host_tools=tools,
        sudo_required=os.geteuid() != 0,
        image_dir_free_mb=free_mb,
    )


def _flag(ok: bool) -> str:
    return "OK" if ok else "MISSING"


def format_doctor_report(status: DoctorStatus) -> str:
    lines: list[str] = []
    lines.append(f"SparkVM home: {status.paths.home_dir}")
    lines.append(f"Host OS Linux: {_flag(status.host_os_ok)}")
    lines.append(f"Architecture ({status.arch_value}): {_flag(status.arch_ok)}")
    lines.append(f"KVM accessible: {_flag(status.kvm_accessible)}")
    lines.append(
        f"Firecracker binary: {_flag(status.firecracker_found)} ({status.paths.firecracker_bin})"
    )
    lines.append(f"Firecracker version: {status.firecracker_version or 'unavailable'}")
    lines.append(f"Kernel image: {_flag(status.kernel_found)} ({status.paths.kernel_image})")
    lines.append(f"Debian rootfs: {_flag(status.debian_rootfs_found)} ({status.paths.debian_rootfs})")
    lines.append(f"Free space (images): {status.image_dir_free_mb} MiB")

    lines.append("Host tools:")
    for tool in REQUIRED_HOST_TOOLS:
        marker = _flag(status.host_tools.get(tool, False))
        suffix = ""
        if tool == "debootstrap" and marker != "OK":
            suffix = " (install: sudo apt-get install -y debootstrap)"
        lines.append(f"  - {tool}: {marker}{suffix}")

    lines.append("Optional tools:")
    for tool in OPTIONAL_HOST_TOOLS:
        marker = _flag(status.host_tools.get(tool, False))
        lines.append(f"  - {tool}: {marker}")

    if status.sudo_required and not status.host_tools.get("sudo", False):
        lines.append("sudo requirement: ERROR (sudo is required when running setup as non-root)")
    elif status.sudo_required:
        lines.append("sudo requirement: OK")
    else:
        lines.append("sudo requirement: OK (running as root)")

    return "\n".join(lines)


def run_setup_command(home_dir: str | None, runtime: str | None, force: bool, *, owner: str | None = None) -> int:
    paths = get_sparkvm_paths(home_dir)
    print(f"Using SparkVM home: {paths.home_dir}", flush=True)

    if runtime is not None and runtime.strip().lower() == "python":
        print("Language-specific setup is no longer required. Use rollout setup_cmd instead.", flush=True)

    progress = lambda message: print(f"[setup] {message}", flush=True)
    print("Running base setup checks and managed asset install...", flush=True)
    run_setup(paths, force=force, owner=owner, progress=progress)
    print("SparkVM setup complete.")
    print(f"Firecracker: {paths.firecracker_bin}")
    print(f"Kernel: {paths.kernel_image}")
    print(f"Base image ({DEBIAN_MINBASE_IMAGE_ID}): {paths.debian_rootfs}")
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
    "DEBIAN_MINBASE_IMAGE_ID",
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
    "ensure_kernel_image",
    "require_root_for_setup",
    "chown_tree",
    "build_debian_minbase_rootfs",
    "run_setup",
    "run_setup_python",
    "run_setup_command",
    "doctor_status",
    "format_doctor_report",
    "ManagedSetup",
]
