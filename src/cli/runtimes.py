"""Runtime image conversion and management commands."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cli.setup import ensure_directories, get_sparkvm_paths
from sparkvm.errors import SparkVMSetupError
from sparkvm.fsops import ensure_dir, read_json, remove_file, write_json_atomic, write_text
from sparkvm.image import normalize_runtime_name
from sparkvm.runtime_store import list_runtime_records, runtime_paths
from sparkvm.runtimes.debian import SPARKVM_INIT_TEMPLATE


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_checked(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SparkVMSetupError(f"Required command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or "command failed"
        raise SparkVMSetupError(f"Command failed: {' '.join(cmd)}\n{detail}") from exc


def _run_docker_export(container_id: str, rootfs_dir: Path) -> None:
    try:
        export_proc = subprocess.Popen(["docker", "export", container_id], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise SparkVMSetupError("Required command not found: docker") from exc

    try:
        untar = subprocess.run(
            ["tar", "-xf", "-", "-C", str(rootfs_dir)],
            stdin=export_proc.stdout,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        if export_proc.stdout is not None:
            export_proc.stdout.close()

    stderr_bytes = b""
    if export_proc.stderr is not None:
        stderr_bytes = export_proc.stderr.read() or b""
        export_proc.stderr.close()

    export_return = export_proc.wait()
    if export_return != 0:
        detail = stderr_bytes.decode("utf-8", errors="replace").strip() or "docker export failed"
        raise SparkVMSetupError(f"Command failed: docker export {container_id}\n{detail}")

    if untar.returncode != 0:
        detail = (untar.stderr or untar.stdout or "tar extraction failed").strip()
        raise SparkVMSetupError(f"Command failed: tar -xf - -C {rootfs_dir}\n{detail}")


def _assert_rootfs_basics(rootfs_dir: Path) -> None:
    sh_path = rootfs_dir / "bin" / "sh"
    if not sh_path.exists():
        raise SparkVMSetupError(f"Runtime rootfs missing required file/tool: /bin/sh. Checked rootfs: {rootfs_dir}.")

    init_path = rootfs_dir / "init"
    if not init_path.exists():
        raise SparkVMSetupError(f"Runtime rootfs missing required file/tool: /init. Checked rootfs: {rootfs_dir}.")
    if not os.access(init_path, os.X_OK):
        raise SparkVMSetupError(
            f"Runtime rootfs missing required file/tool: /init (not executable). Checked rootfs: {rootfs_dir}."
        )

    mount_candidates = [rootfs_dir / "bin/mount", rootfs_dir / "usr/bin/mount"]
    if not any(path.exists() for path in mount_candidates):
        raise SparkVMSetupError(
            f"Runtime rootfs missing recommended tool: mount command. Checked rootfs: {rootfs_dir}."
        )


def _resolve_owner(owner: str) -> tuple[int, int]:
    try:
        user_info = pwd.getpwnam(owner)
    except KeyError as exc:
        raise SparkVMSetupError(f"Unknown owner user: {owner}") from exc
    return user_info.pw_uid, user_info.pw_gid


def _chown_path(path: Path, owner: str) -> None:
    uid, gid = _resolve_owner(owner)
    os.chown(path, uid, gid)


def _dockify_requires_root_message(image: str, home_dir: Path, owner: str | None) -> str:
    sparkvm_exe = shutil.which("sparkvm") or str(Path(sys.argv[0]).resolve())
    if owner is None:
        owner = pwd.getpwuid(os.getuid()).pw_name
    return (
        "Dockify requires root privileges for mounting ext4 images.\n"
        "Run:\n"
        f"  sudo {sparkvm_exe} dockify {image} --home-dir {home_dir} --owner {owner}"
    )


def _default_owner_for_root() -> str | None:
    sudo_user = os.getenv("SUDO_USER", "").strip()
    if sudo_user and sudo_user != "root":
        return sudo_user
    return None


def _build_ext4_from_rootfs(*, temp_ext4: Path, rootfs_dir: Path, size_mb: int) -> None:
    _run_checked(["dd", "if=/dev/zero", f"of={temp_ext4}", "bs=1M", f"count={size_mb}", "status=none"])
    try:
        _run_checked(["mkfs.ext4", "-d", str(rootfs_dir), "-F", str(temp_ext4)])
        return
    except SparkVMSetupError as exc:
        detail = str(exc).lower()
        mkfs_d_unsupported = ("invalid option" in detail and "-d" in detail) or ("unrecognized option" in detail and "-d" in detail)
        if not mkfs_d_unsupported:
            raise

    if os.geteuid() != 0:
        raise SparkVMSetupError(
            "mkfs.ext4 on this host does not support '-d', which is required for non-root dockify.\n"
            "Options:\n"
            "  1) Install e2fsprogs/mkfs.ext4 with '-d' support\n"
            "  2) Run dockify with sudo to allow mount-based fallback"
        )

    with tempfile.TemporaryDirectory(prefix="sparkvm-dockify-mnt-") as tmp_mount_dir_str:
        mount_dir = Path(tmp_mount_dir_str)
        _run_checked(["mkfs.ext4", "-F", str(temp_ext4)])
        _run_checked(["mount", "-o", "loop", str(temp_ext4), str(mount_dir)])
        mounted = True
        try:
            _run_checked(["cp", "-a", f"{rootfs_dir}/.", str(mount_dir)])
            _run_checked(["sync"])
            _run_checked(["umount", str(mount_dir)])
            mounted = False
        finally:
            if mounted:
                try:
                    _run_checked(["umount", str(mount_dir)])
                except Exception:
                    pass


def run_dockify_command(
    home_dir: str | None,
    image: str,
    *,
    name: str | None,
    size_mb: int,
    force: bool,
    pull: bool,
    owner: str | None,
) -> int:
    if not isinstance(image, str) or not image.strip():
        raise SparkVMSetupError("docker image must be a non-empty string")
    if isinstance(size_mb, bool) or size_mb <= 0:
        raise SparkVMSetupError("--size-mb must be a positive integer")

    paths = get_sparkvm_paths(home_dir)
    ensure_directories(paths)

    runtime_name = normalize_runtime_name(name) if name is not None else normalize_runtime_name(image)
    runtime_file_paths = runtime_paths(paths.image_dir, runtime_name)

    if runtime_file_paths.rootfs.exists() and not force:
        raise SparkVMSetupError(
            f"Runtime image already exists: {runtime_file_paths.rootfs}. Use --force to overwrite."
        )

    if shutil.which("docker") is None:
        raise SparkVMSetupError("Docker CLI not found. Install Docker and retry `sparkvm dockify`.")

    effective_owner = owner
    if effective_owner is None:
        effective_owner = _default_owner_for_root()

    container_id = ""
    final_rootfs = runtime_file_paths.rootfs
    final_metadata = runtime_file_paths.metadata

    ensure_dir(paths.cache_dir, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sparkvm-dockify-", dir=str(paths.cache_dir)) as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        rootfs_dir = tmp_dir / "rootfs"
        temp_ext4 = tmp_dir / f"{runtime_name}.ext4"
        ensure_dir(rootfs_dir, exist_ok=True)

        try:
            if pull:
                print(f"[dockify] Pulling image: {image}", flush=True)
                _run_checked(["docker", "pull", image])

            print(f"[dockify] Creating container from: {image}", flush=True)
            create = _run_checked(["docker", "create", image])
            container_id = create.stdout.strip()
            if not container_id:
                raise SparkVMSetupError("docker create returned empty container id")

            print("[dockify] Exporting container filesystem", flush=True)
            _run_docker_export(container_id, rootfs_dir)

            init_path = rootfs_dir / "init"
            write_text(init_path, SPARKVM_INIT_TEMPLATE, encoding="utf-8")
            init_path.chmod(0o755)

            _assert_rootfs_basics(rootfs_dir)

            print(f"[dockify] Building ext4 image ({size_mb} MiB)", flush=True)
            _build_ext4_from_rootfs(temp_ext4=temp_ext4, rootfs_dir=rootfs_dir, size_mb=size_mb)

            metadata = {
                "runtime": runtime_name,
                "source_image": image,
                "rootfs": str(final_rootfs),
                "size_mb": size_mb,
                "created_at": _utc_now_iso(),
                "init_injected": True,
            }

            remove_file(final_rootfs, missing_ok=True)
            remove_file(final_metadata, missing_ok=True)
            temp_ext4.replace(final_rootfs)
            write_json_atomic(final_metadata, metadata, encoding="utf-8", pretty=True)

            if effective_owner is not None:
                _chown_path(final_rootfs, effective_owner)
                _chown_path(final_metadata, effective_owner)

        except Exception:
            remove_file(temp_ext4, missing_ok=True)
            remove_file(final_rootfs, missing_ok=True)
            remove_file(final_metadata, missing_ok=True)
            raise
        finally:
            if container_id:
                try:
                    _run_checked(["docker", "rm", "-f", container_id])
                except Exception:
                    pass

    print(f"Dockified runtime '{runtime_name}'")
    print(f"Rootfs: {final_rootfs}")
    print(f"Metadata: {final_metadata}")
    return 0


def run_runtimes_list_command(home_dir: str | None) -> int:
    paths = get_sparkvm_paths(home_dir)
    records = list_runtime_records(paths.image_dir)
    if not records:
        print("no runtime images found. Run `sparkvm dockify python:3.12-slim`.")
        return 0

    headers = ["RUNTIME", "SOURCE", "SIZE_MB", "CREATED_AT"]
    rows: list[list[str]] = []
    for record in records:
        rows.append(
            [
                record.name,
                record.source_image or "-",
                str(record.size_mb) if record.size_mb is not None else "-",
                record.created_at or "-",
            ]
        )

    widths = [len(h) for h in headers]
    for row in rows:
        for idx, col in enumerate(row):
            widths[idx] = max(widths[idx], len(col))

    print(" | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("-+-".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print(" | ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return 0


def run_runtimes_inspect_command(home_dir: str | None, runtime: str) -> int:
    paths = get_sparkvm_paths(home_dir)
    runtime_file_paths = runtime_paths(paths.image_dir, runtime)
    if not runtime_file_paths.metadata.exists():
        raise SparkVMSetupError(f"Runtime metadata not found: {runtime_file_paths.metadata}")

    payload = read_json(runtime_file_paths.metadata, encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def run_runtimes_delete_command(home_dir: str | None, runtime: str, *, force: bool) -> int:
    paths = get_sparkvm_paths(home_dir)
    runtime_file_paths = runtime_paths(paths.image_dir, runtime)

    if not force:
        response = input(f"Delete runtime {runtime_file_paths.name}? [y/N] ").strip().lower()
        if response not in {"y", "yes"}:
            print("Aborted.")
            return 0

    remove_file(runtime_file_paths.rootfs, missing_ok=True)
    remove_file(runtime_file_paths.metadata, missing_ok=True)
    print(f"Deleted runtime: {runtime_file_paths.name}")
    return 0


__all__ = [
    "run_dockify_command",
    "run_runtimes_list_command",
    "run_runtimes_inspect_command",
    "run_runtimes_delete_command",
]
