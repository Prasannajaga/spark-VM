"""Execution disk helpers for SparkVM rollouts."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .errors import CleanupError, ExecutionDiskError
from .result import VMResult
from .rollouts import RolloutItem


def _run_checked(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ExecutionDiskError(f"Required command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or "command failed"
        raise ExecutionDiskError(f"Command failed: {' '.join(cmd)}\n{detail}") from exc


def _validate_relative_path(path: str) -> PurePosixPath:
    if not isinstance(path, str):
        raise ExecutionDiskError("Execution disk file path must be a string.")

    raw = path.strip()
    if not raw:
        raise ExecutionDiskError("Execution disk file path cannot be empty.")
    if raw.startswith("/"):
        raise ExecutionDiskError(f"Execution disk file path must be relative: {path!r}")

    segments = raw.split("/")
    if any(segment == "" for segment in segments):
        raise ExecutionDiskError(f"Execution disk file path has empty segments: {path!r}")
    if any(segment in {".", ".."} for segment in segments):
        raise ExecutionDiskError(f"Execution disk file path cannot contain '.' or '..': {path!r}")

    normalized = PurePosixPath(raw)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ExecutionDiskError(f"Execution disk path escapes mount root: {path!r}")
    return normalized


def create_ext4_image(path: Path, size_mib: int, *, source_dir: Path | None = None) -> Path:
    if size_mib <= 0:
        raise ExecutionDiskError("size_mib must be greater than zero.")

    image_path = Path(path)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    _run_checked(
        [
            "dd",
            "if=/dev/zero",
            f"of={image_path}",
            "bs=1M",
            f"count={size_mib}",
            "status=none",
        ]
    )
    mkfs_cmd = ["mkfs.ext4", "-F"]
    if source_dir is not None:
        mkfs_cmd.extend(["-d", str(source_dir)])
    mkfs_cmd.append(str(image_path))
    try:
        _run_checked(mkfs_cmd)
    except ExecutionDiskError as exc:
        detail = str(exc).lower()
        if source_dir is not None and "invalid option" in detail and "-d" in detail:
            raise ExecutionDiskError(
                "mkfs.ext4 on this host does not support '-d' for populating ext4 images. "
                "Install e2fsprogs with mkfs.ext4 '-d' support."
            ) from exc
        raise
    return image_path


def mount_ext4(path: Path, mount_dir: Path) -> None:
    mount_path = Path(mount_dir)
    mount_path.mkdir(parents=True, exist_ok=True)
    _run_checked(["mount", "-o", "loop", str(path), str(mount_path)])


def copy_files_into_mount(files: dict[str, str | bytes], mount_dir: Path) -> None:
    target_dir = Path(mount_dir)
    for raw_path, content in files.items():
        safe_path = _validate_relative_path(raw_path)
        destination = target_dir / Path(safe_path.as_posix())
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            destination.write_bytes(content)
        elif isinstance(content, str):
            destination.write_text(content, encoding="utf-8")
        else:
            raise ExecutionDiskError(f"Unsupported file content type for {raw_path!r}.")


def unmount_ext4(mount_dir: Path) -> None:
    _run_checked(["umount", str(mount_dir)])


def _debugfs_dump_file(image_path: Path, fs_path: str, output_path: Path) -> bool:
    try:
        _run_checked(["debugfs", "-R", f"dump -p {fs_path} {output_path}", str(image_path)])
        return True
    except ExecutionDiskError as exc:
        detail = str(exc).lower()
        not_found_markers = (
            "not found by ext2_lookup",
            "file not found",
            "no such file or directory",
        )
        if any(marker in detail for marker in not_found_markers):
            return False
        raise


class ExecutionDisk:
    def __init__(
        self,
        *,
        rollout: RolloutItem,
        path: Path,
        size_mb: int,
        mount_base: Path,
    ) -> None:
        self.rollout = rollout
        self.path = Path(path)
        self.size_mb = int(size_mb)
        self.mount_base = Path(mount_base)
        self.mount_dir = self.mount_base / f"{self.path.stem}-mount"
        self._mounted = False

    def create(self) -> None:
        create_ext4_image(self.path, self.size_mb)

    def mount(self) -> None:
        if self._mounted:
            return
        self.mount_base.mkdir(parents=True, exist_ok=True)
        mount_ext4(self.path, self.mount_dir)
        self._mounted = True

    def copy_rollout(self) -> None:
        if not self.rollout.path.exists():
            raise ExecutionDiskError(f"Rollout path does not exist: {self.rollout.path}")

        with tempfile.TemporaryDirectory(prefix="sparkvm-execution-disk-") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            staged_root = tmp_dir / "job"
            shutil.copytree(self.rollout.path, staged_root, symlinks=True)
            create_ext4_image(self.path, self.size_mb, source_dir=staged_root)

    def unmount(self) -> None:
        if not self._mounted:
            return
        unmount_ext4(self.mount_dir)
        self._mounted = False

    def read_result(
        self,
        vm_id: str,
        duration_ms: int,
        firecracker_log_path: Path | None,
    ) -> VMResult:
        with tempfile.TemporaryDirectory(prefix="sparkvm-execution-read-") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            output_log_path = tmp_dir / "output.log"
            error_log_path = tmp_dir / "error.log"
            exit_code_path = tmp_dir / "exit_code"

            stdout = ""
            stderr = ""
            exit_code = 1

            if _debugfs_dump_file(self.path, "/output.log", output_log_path):
                stdout = output_log_path.read_text(encoding="utf-8")
            if _debugfs_dump_file(self.path, "/error.log", error_log_path):
                stderr = error_log_path.read_text(encoding="utf-8")
            if _debugfs_dump_file(self.path, "/exit_code", exit_code_path):
                raw_exit = exit_code_path.read_text(encoding="utf-8").strip()
                try:
                    exit_code = int(raw_exit)
                except ValueError:
                    exit_code = 1

            return VMResult(
                rollout_id=self.rollout.id,
                rollout_name=self.rollout.name,
                vm_id=vm_id,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=duration_ms,
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=self.path,
            )

    def cleanup(self, remove_disk: bool = True) -> None:
        errors: list[Exception] = []

        try:
            self.unmount()
        except Exception as exc:
            errors.append(exc)

        if remove_disk and self.path.exists():
            try:
                self.path.unlink()
            except OSError as exc:
                errors.append(exc)

        if self.mount_dir.exists():
            try:
                self.mount_dir.rmdir()
            except OSError:
                try:
                    shutil.rmtree(self.mount_dir, ignore_errors=False)
                except OSError as exc:
                    errors.append(exc)

        if errors:
            raise CleanupError(f"Execution disk cleanup failed: {errors[0]}")


__all__ = [
    "create_ext4_image",
    "mount_ext4",
    "copy_files_into_mount",
    "unmount_ext4",
    "ExecutionDisk",
]
