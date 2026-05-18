"""Execution disk helpers for SparkVM rollouts."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .errors import CleanupError, ExecutionDiskError
from .fsops import ensure_dir, read_text, remove_file, remove_tree, write_bytes, write_text
from .result import PhaseResult, VMResult
from .rollouts import Rollout


def run_checked(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ExecutionDiskError(f"Required command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or "command failed"
        raise ExecutionDiskError(f"Command failed: {' '.join(cmd)}\n{detail}") from exc


def validate_relative_path(path: str) -> PurePosixPath:
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
    ensure_dir(image_path.parent, exist_ok=True)
    run_checked(
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
        run_checked(mkfs_cmd)
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
    ensure_dir(mount_path, exist_ok=True)
    run_checked(["mount", "-o", "loop", str(path), str(mount_path)])


def copy_files_into_mount(files: dict[str, str | bytes], mount_dir: Path) -> None:
    target_dir = Path(mount_dir)
    for raw_path, content in files.items():
        safe_path = validate_relative_path(raw_path)
        destination = target_dir / Path(safe_path.as_posix())
        ensure_dir(destination.parent, exist_ok=True)
        if isinstance(content, bytes):
            write_bytes(destination, content)
        elif isinstance(content, str):
            write_text(destination, content, encoding="utf-8")
        else:
            raise ExecutionDiskError(f"Unsupported file content type for {raw_path!r}.")


def unmount_ext4(mount_dir: Path) -> None:
    run_checked(["umount", str(mount_dir)])


def debugfs_dump_file(image_path: Path, fs_path: str, output_path: Path) -> bool:
    try:
        run_checked(["debugfs", "-R", f"dump -p {fs_path} {output_path}", str(image_path)])
        # Some debugfs versions can exit successfully even when no output file is produced.
        # Treat that as a missing file so callers can apply fallback behavior.
        return output_path.exists()
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
        rollout: Rollout,
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
        ensure_dir(self.mount_base, exist_ok=True)
        mount_ext4(self.path, self.mount_dir)
        self._mounted = True

    def copy_rollout(self, runtime_files: dict[str, str] | None = None) -> None:
        if not self.rollout.path.exists():
            raise ExecutionDiskError(f"Rollout path does not exist: {self.rollout.path}")

        with tempfile.TemporaryDirectory(prefix="sparkvm-execution-disk-") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            staged_root = tmp_dir / "job"
            shutil.copytree(self.rollout.path, staged_root, symlinks=True)
            if runtime_files:
                copy_files_into_mount(runtime_files, staged_root)
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
        def read_int_file(path: Path, *, fs_path: str) -> int:
            raw = read_text(path, encoding="utf-8").strip()
            try:
                return int(raw)
            except ValueError as exc:
                raise ExecutionDiskError(f"Guest produced invalid integer value in {fs_path}: {raw!r}.") from exc

        with tempfile.TemporaryDirectory(prefix="sparkvm-execution-read-") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            final_exit_code_path = tmp_dir / "final_exit_code"
            has_phased_results = debugfs_dump_file(self.path, "/results/final_exit_code", final_exit_code_path)

            setup: PhaseResult | None = None
            run: PhaseResult | None = None

            if has_phased_results:
                final_exit_code = read_int_file(final_exit_code_path, fs_path="/results/final_exit_code")

                setup_exit_code_path = tmp_dir / "setup.exit_code"
                if debugfs_dump_file(self.path, "/results/setup.exit_code", setup_exit_code_path):
                    setup_stdout_path = tmp_dir / "setup.stdout.log"
                    setup_stderr_path = tmp_dir / "setup.stderr.log"
                    setup_stdout = ""
                    setup_stderr = ""
                    if debugfs_dump_file(self.path, "/results/setup.stdout.log", setup_stdout_path):
                        setup_stdout = read_text(setup_stdout_path, encoding="utf-8")
                    if debugfs_dump_file(self.path, "/results/setup.stderr.log", setup_stderr_path):
                        setup_stderr = read_text(setup_stderr_path, encoding="utf-8")
                    setup = PhaseResult(
                        name="setup",
                        stdout=setup_stdout,
                        stderr=setup_stderr,
                        exit_code=read_int_file(setup_exit_code_path, fs_path="/results/setup.exit_code"),
                    )

                run_exit_code_path = tmp_dir / "run.exit_code"
                if debugfs_dump_file(self.path, "/results/run.exit_code", run_exit_code_path):
                    run_stdout_path = tmp_dir / "run.stdout.log"
                    run_stderr_path = tmp_dir / "run.stderr.log"
                    run_stdout = ""
                    run_stderr = ""
                    if debugfs_dump_file(self.path, "/results/run.stdout.log", run_stdout_path):
                        run_stdout = read_text(run_stdout_path, encoding="utf-8")
                    if debugfs_dump_file(self.path, "/results/run.stderr.log", run_stderr_path):
                        run_stderr = read_text(run_stderr_path, encoding="utf-8")
                    run = PhaseResult(
                        name="run",
                        stdout=run_stdout,
                        stderr=run_stderr,
                        exit_code=read_int_file(run_exit_code_path, fs_path="/results/run.exit_code"),
                    )

                status = "passed"
                if setup is not None and setup.exit_code != 0:
                    status = "setup_failed"
                elif run is not None and run.exit_code != 0:
                    status = "run_failed"
                elif final_exit_code != 0:
                    status = "run_failed"
            else:
                output_log_path = tmp_dir / "output.log"
                error_log_path = tmp_dir / "error.log"
                exit_code_path = tmp_dir / "exit_code"

                stdout = ""
                stderr = ""
                if debugfs_dump_file(self.path, "/output.log", output_log_path):
                    stdout = read_text(output_log_path, encoding="utf-8")
                if debugfs_dump_file(self.path, "/error.log", error_log_path):
                    stderr = read_text(error_log_path, encoding="utf-8")
                if not debugfs_dump_file(self.path, "/exit_code", exit_code_path):
                    raise ExecutionDiskError(
                        "Guest did not produce result files (/results or /exit_code) on execution disk. "
                        "Execution considered failed."
                    )
                final_exit_code = read_int_file(exit_code_path, fs_path="/exit_code")
                run = PhaseResult(name="run", stdout=stdout, stderr=stderr, exit_code=final_exit_code)
                status = "passed" if final_exit_code == 0 else "run_failed"

            return VMResult(
                rollout_id=self.rollout.id,
                rollout_name=self.rollout.name,
                rollout_mode=self.rollout.mode,
                base_image=self.rollout.base_image,
                vm_id=vm_id,
                status=status,
                exit_code=final_exit_code,
                duration_ms=duration_ms,
                setup=setup,
                run=run,
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
                remove_file(self.path, missing_ok=True)
            except OSError as exc:
                errors.append(exc)

        if self.mount_dir.exists():
            try:
                self.mount_dir.rmdir()
            except OSError:
                try:
                    remove_tree(self.mount_dir, ignore_errors=False)
                except OSError as exc:
                    errors.append(exc)

        if errors:
            raise CleanupError(f"Execution disk cleanup failed: {errors[0]}")


def scrub_files_from_ext4_image(
    *,
    image_path: Path,
    mount_base: Path,
    rel_paths: list[str],
) -> None:
    mount_dir = mount_base / f"{image_path.stem}-scrub-mount"
    mounted = False
    try:
        ensure_dir(mount_base, exist_ok=True)
        mount_ext4(image_path, mount_dir)
        mounted = True
        for rel_path in rel_paths:
            safe_path = validate_relative_path(rel_path)
            remove_file(mount_dir / Path(safe_path.as_posix()), missing_ok=True)
    finally:
        if mounted:
            try:
                unmount_ext4(mount_dir)
            except Exception as exc:
                raise ExecutionDiskError(f"Failed to unmount scrub mount {mount_dir}: {exc}") from exc
        if mount_dir.exists():
            try:
                mount_dir.rmdir()
            except OSError:
                remove_tree(mount_dir, ignore_errors=True)


__all__ = [
    "create_ext4_image",
    "mount_ext4",
    "copy_files_into_mount",
    "unmount_ext4",
    "scrub_files_from_ext4_image",
    "ExecutionDisk",
]
