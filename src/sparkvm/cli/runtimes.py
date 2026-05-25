"""Runtime image conversion and management commands."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sparkvm.cli.setup import ensure_directories, get_sparkvm_paths
from sparkvm.core.commands import popen_checked, run_checked as _run_checked
from sparkvm.core.errors import SparkVMSetupError
from sparkvm.core.fsops import ensure_dir, read_json, remove_file, write_json_atomic, write_text
from sparkvm.machine.image import normalize_runtime_name
from sparkvm.storage.runtime_store import list_runtime_records, runtime_paths
from sparkvm.runtimes.debian import SPARKVM_INIT_TEMPLATE

from sparkvm.core.constants import BUSYBOX_CANDIDATE_PATHS, IP_CANDIDATE_PATHS, SHUTDOWN_FALLBACK_PATHS


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_checked(cmd: list[str], *, cwd: Path | None = None):
    return _run_checked(cmd, error_factory=SparkVMSetupError, cwd=cwd)


def run_docker_export(container_id: str, rootfs_dir: Path) -> None:
    export_proc = popen_checked(
        ["docker", "export", container_id],
        error_factory=SparkVMSetupError,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        untar = _run_checked(
            ["tar", "-xf", "-", "-C", str(rootfs_dir)],
            error_factory=SparkVMSetupError,
            stdin=export_proc.stdout,
            check=False,
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


def assert_rootfs_basics(rootfs_dir: Path) -> None:
    sh_candidates = [rootfs_dir / "bin/sh", rootfs_dir / "usr/bin/sh"]
    if not any(path.exists() for path in sh_candidates):
        raise SparkVMSetupError(
            f"Runtime rootfs missing required file/tool: /bin/sh or /usr/bin/sh. Checked rootfs: {rootfs_dir}."
        )

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
            f"Runtime rootfs missing required file/tool: mount command. Checked rootfs: {rootfs_dir}."
        )

def runtime_validation_metadata(rootfs_dir: Path) -> dict[str, object]:
    ip_present = [candidate for candidate in IP_CANDIDATE_PATHS if (rootfs_dir / candidate.lstrip("/")).exists()]
    shutdown_paths = [
        candidate for candidate in SHUTDOWN_FALLBACK_PATHS if (rootfs_dir / candidate.lstrip("/")).exists()
    ]
    busybox_paths = [
        candidate for candidate in BUSYBOX_CANDIDATE_PATHS if (rootfs_dir / candidate.lstrip("/")).exists()
    ]
    ca_cert_candidates = ["/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem"]
    ca_cert_paths = [candidate for candidate in ca_cert_candidates if (rootfs_dir / candidate.lstrip("/")).exists()]
    curl_candidates = ["/usr/bin/curl", "/bin/curl"]
    wget_candidates = ["/usr/bin/wget", "/bin/wget"]
    curl_paths = [candidate for candidate in curl_candidates if (rootfs_dir / candidate.lstrip("/")).exists()]
    wget_paths = [candidate for candidate in wget_candidates if (rootfs_dir / candidate.lstrip("/")).exists()]

    warnings: list[str] = []
    if not ip_present:
        warnings.append("Runtime image does not contain ip command. network=True will fail unless installed.")
    if not shutdown_paths and not busybox_paths:
        warnings.append(
            "Runtime image does not contain poweroff/halt/reboot/busybox. "
            "Guest shutdown may rely on /proc/sysrq-trigger fallback."
        )
    if not ca_cert_paths:
        warnings.append("Runtime image does not contain CA certificates; HTTPS calls may fail.")
    if not curl_paths and not wget_paths:
        warnings.append("Runtime image does not contain curl/wget. Optional, but useful for debugging.")

    return {
        "ip_command_present": bool(ip_present),
        "ip_command_paths": ip_present,
        "shutdown_command_present": bool(shutdown_paths),
        "shutdown_command_paths": shutdown_paths,
        "busybox_present": bool(busybox_paths),
        "busybox_paths": busybox_paths,
        "ca_certificates_present": bool(ca_cert_paths),
        "ca_certificates_paths": ca_cert_paths,
        "curl_present": bool(curl_paths),
        "curl_paths": curl_paths,
        "wget_present": bool(wget_paths),
        "wget_paths": wget_paths,
        "warnings": warnings,
    }


def resolve_owner(owner: str) -> tuple[int, int]:
    try:
        user_info = pwd.getpwnam(owner)
    except KeyError as exc:
        raise SparkVMSetupError(f"Unknown owner user: {owner}") from exc
    return user_info.pw_uid, user_info.pw_gid


def chown_path(path: Path, owner: str) -> None:
    uid, gid = resolve_owner(owner)
    os.chown(path, uid, gid)


def ensure_runtime_artifact_permissions(*, rootfs: Path, metadata: Path, owner: str | None) -> None:
    targets = (rootfs, metadata)

    for target in targets:
        target.chmod(0o644)

    if owner is not None:
        for target in targets:
            chown_path(target, owner)
            target.chmod(0o644)
        return

    if os.geteuid() != 0:
        uid = os.getuid()
        gid = os.getgid()
        for target in targets:
            st = target.stat()
            if st.st_uid != uid or st.st_gid != gid:
                raise SparkVMSetupError(
                    "Runtime artifact ownership mismatch. Re-run dockify without sudo or pass --owner.\n"
                    f"Path: {target}"
                )


def default_owner_for_root() -> str | None:
    sudo_user = os.getenv("SUDO_USER", "").strip()
    if sudo_user and sudo_user != "root":
        return sudo_user
    return None


def build_ext4_from_rootfs(*, temp_ext4: Path, rootfs_dir: Path, size_mb: int) -> None:
    run_checked(
        ["dd", "if=/dev/zero", f"of={temp_ext4}", "bs=1M", f"count={size_mb}", "status=none"],
    )
    try:
        run_checked(["mkfs.ext4", "-d", str(rootfs_dir), "-F", str(temp_ext4)])
    except SparkVMSetupError as exc:
        detail = str(exc).lower()
        mkfs_d_unsupported = ("invalid option" in detail and "-d" in detail) or (
            "unrecognized option" in detail and "-d" in detail
        )
        if mkfs_d_unsupported:
            raise SparkVMSetupError(
                "mkfs.ext4 on this host does not support '-d'. "
                "Install e2fsprogs/mkfs.ext4 with '-d' support and retry."
            ) from exc
        raise


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
        effective_owner = default_owner_for_root()

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
                run_checked(["docker", "pull", image])

            print(f"[dockify] Creating container from: {image}", flush=True)
            create = run_checked(["docker", "create", image])
            container_id = create.stdout.strip()
            if not container_id:
                raise SparkVMSetupError("docker create returned empty container id")

            print("[dockify] Exporting container filesystem", flush=True)
            run_docker_export(container_id, rootfs_dir)

            # Rootfs is mounted read-only by Firecracker; ensure /job mountpoint exists in-image.
            ensure_dir(rootfs_dir / "job", exist_ok=True)
            ensure_dir(rootfs_dir / "job" / "results", exist_ok=True)

            init_path = rootfs_dir / "init"
            write_text(init_path, SPARKVM_INIT_TEMPLATE, encoding="utf-8")
            init_path.chmod(0o755)

            assert_rootfs_basics(rootfs_dir)
            validation = runtime_validation_metadata(rootfs_dir)

            print(f"[dockify] Building ext4 image ({size_mb} MiB)", flush=True)
            build_ext4_from_rootfs(temp_ext4=temp_ext4, rootfs_dir=rootfs_dir, size_mb=size_mb)

            metadata = {
                "runtime": runtime_name,
                "source_image": image,
                "rootfs": str(final_rootfs),
                "size_mb": size_mb,
                "created_at": utc_now_iso(),
                "init_injected": True,
                "validation": validation,
            }

            remove_file(final_rootfs, missing_ok=True)
            remove_file(final_metadata, missing_ok=True)
            temp_ext4.replace(final_rootfs)
            write_json_atomic(final_metadata, metadata, encoding="utf-8", pretty=True)
            ensure_runtime_artifact_permissions(
                rootfs=final_rootfs,
                metadata=final_metadata,
                owner=effective_owner,
            )

        except Exception:
            remove_file(temp_ext4, missing_ok=True)
            remove_file(final_rootfs, missing_ok=True)
            remove_file(final_metadata, missing_ok=True)
            raise
        finally:
            if container_id:
                try:
                    run_checked(["docker", "rm", "-f", container_id])
                except Exception:
                    pass

    print(f"Dockified runtime '{runtime_name}'")
    print(f"Rootfs: {final_rootfs}")
    print(f"Metadata: {final_metadata}")
    metadata_payload = read_json(final_metadata, encoding="utf-8")
    validation_payload = metadata_payload.get("validation")
    if isinstance(validation_payload, dict):
        warnings = validation_payload.get("warnings")
        if isinstance(warnings, list):
            for warning in warnings:
                if isinstance(warning, str) and warning.strip():
                    print(f"Warning: {warning}")
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
