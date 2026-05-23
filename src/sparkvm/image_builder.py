"""Dockerfile-backed rollout image builder."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .commands import run_checked
from .errors import RolloutBuildError, RolloutConfigError
from .fsops import ensure_dir, read_json, remove_file, write_json_atomic, write_text

INIT_TEMPLATE_VERSION = "sparkvm-init-template-v1"


from .utils import (
    ResolvedCommand as ResolvedRunCommand,
    now_utc_iso,
    resolve_container_command as resolve_run_command,
)


@dataclass(frozen=True)
class BuiltImage:
    id: str
    rollout_id: str
    path: Path
    metadata_path: Path
    docker_image_tag: str
    resolved_run_command: ResolvedRunCommand
    size_mb: int
    created_at: str


def image_id_for_rollout(rollout_id: str) -> str:
    return f"image-{rollout_id}"


def chown_owner(path: Path, owner: str | None) -> None:
    if owner is None or not owner.strip():
        return
    user: str | None = owner
    group: str | None = None
    if ":" in owner:
        user_part, group_part = owner.split(":", 1)
        user = user_part or None
        group = group_part or None
    shutil.chown(path, user=user, group=group)


def append_log(path: Path, text: str) -> None:
    ensure_dir(path.parent, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(text)
        if text and not text.endswith("\n"):
            handle.write("\n")


def run_checked_with_logs(cmd: list[str], *, stdout_log: Path, stderr_log: Path) -> subprocess.CompletedProcess[str]:
    try:
        completed = run_checked(cmd, error_factory=RolloutBuildError)
    except RolloutBuildError as exc:
        append_log(stderr_log, str(exc))
        raise
    append_log(stdout_log, completed.stdout or "")
    append_log(stderr_log, completed.stderr or "")
    return completed


def run_streamed_to_logs(cmd: list[str], *, stdout_log: Path, stderr_log: Path) -> None:
    ensure_dir(stdout_log.parent, exist_ok=True)
    with stdout_log.open("ab") as stdout_handle, stderr_log.open("ab") as stderr_handle:
        try:
            completed = subprocess.run(cmd, stdout=stdout_handle, stderr=stderr_handle, check=False)
        except FileNotFoundError as exc:
            raise RolloutBuildError(f"Required command not found: {cmd[0]}") from exc
    if completed.returncode != 0:
        raise RolloutBuildError(f"Command failed: {' '.join(cmd)}. See build logs in {stdout_log.parent}.")


def inspect_image(image_tag: str, *, stdout_log: Path, stderr_log: Path) -> dict[str, Any]:
    inspect_result = run_checked_with_logs(
        ["docker", "image", "inspect", image_tag], stdout_log=stdout_log, stderr_log=stderr_log
    )
    try:
        payload = json.loads(inspect_result.stdout)
    except json.JSONDecodeError as exc:
        raise RolloutBuildError("docker image inspect returned invalid JSON.") from exc
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RolloutBuildError("docker image inspect returned unexpected payload.")
    return payload[0]


def tar_contains_workspace(tar_path: Path) -> bool:
    for candidate in ("workspace", "workspace/", "./workspace", "./workspace/"):
        try:
            completed = subprocess.run(["tar", "-tf", str(tar_path), candidate], capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise RolloutBuildError("Required command not found: tar") from exc
        if completed.returncode == 0:
            return True
    return False


def validate_exported_rootfs(rootfs_dir: Path) -> None:
    init_path = rootfs_dir / "init"
    if not init_path.exists():
        raise RolloutBuildError("Exported rootfs is missing /init after injection.")
    if not os.access(init_path, os.X_OK):
        raise RolloutBuildError("Exported rootfs /init is not executable after injection.")
    if not ((rootfs_dir / "bin/sh").exists() or (rootfs_dir / "usr/bin/sh").exists()):
        raise RolloutBuildError("Exported rootfs is missing /bin/sh or /usr/bin/sh.")
    if not ((rootfs_dir / "bin/mount").exists() or (rootfs_dir / "usr/bin/mount").exists()):
        raise RolloutBuildError("Exported rootfs is missing mount command.")


def convert_docker_export_to_ext4(
    *,
    tar_path: Path,
    output_path: Path,
    disk_mb: int,
    init_template: str,
    build_log_stdout: Path,
    build_log_stderr: Path,
    owner: str | None = None,
) -> None:
    if disk_mb <= 0:
        raise RolloutConfigError("disk_mb must be a positive integer.")

    cache_dir = output_path.parent.parent / "cache"
    ensure_dir(cache_dir, exist_ok=True)
    ensure_dir(output_path.parent, exist_ok=True)
    tmp_image = output_path.with_name(f"{output_path.name}.tmp")
    mounted = False

    try:
        with tempfile.TemporaryDirectory(prefix="sparkvm-image-build-", dir=str(cache_dir)) as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            rootfs_dir = tmp_dir / "rootfs"
            mount_dir = tmp_dir / "mnt"
            ensure_dir(rootfs_dir, exist_ok=True)
            ensure_dir(mount_dir, exist_ok=True)

            try:
                run_checked_with_logs(
                    ["tar", "-xf", str(tar_path), "-C", str(rootfs_dir)],
                    stdout_log=build_log_stdout,
                    stderr_log=build_log_stderr,
                )

                init_path = rootfs_dir / "init"
                write_text(init_path, init_template, encoding="utf-8")
                init_path.chmod(0o755)
                ensure_dir(rootfs_dir / "job", exist_ok=True)
                ensure_dir(rootfs_dir / "job" / "results", exist_ok=True)
                validate_exported_rootfs(rootfs_dir)

                remove_file(tmp_image, missing_ok=True)
                run_checked_with_logs(
                    ["dd", "if=/dev/zero", f"of={tmp_image}", "bs=1M", f"count={disk_mb}", "status=none"],
                    stdout_log=build_log_stdout,
                    stderr_log=build_log_stderr,
                )
                run_checked_with_logs(
                    ["mkfs.ext4", "-F", str(tmp_image)],
                    stdout_log=build_log_stdout,
                    stderr_log=build_log_stderr,
                )
                run_checked_with_logs(
                    ["mount", "-o", "loop", str(tmp_image), str(mount_dir)],
                    stdout_log=build_log_stdout,
                    stderr_log=build_log_stderr,
                )
                mounted = True

                try:
                    run_checked_with_logs(
                        ["rsync", "-aHAX", "--numeric-ids", f"{rootfs_dir}/", str(mount_dir) + "/"],
                        stdout_log=build_log_stdout,
                        stderr_log=build_log_stderr,
                    )
                except (ValueError, RolloutBuildError):
                    run_checked_with_logs(
                        ["cp", "-a", f"{rootfs_dir}/.", str(mount_dir)],
                        stdout_log=build_log_stdout,
                        stderr_log=build_log_stderr,
                    )
                run_checked_with_logs(["sync"], stdout_log=build_log_stdout, stderr_log=build_log_stderr)
            finally:
                if mounted:
                    try:
                        run_checked_with_logs(["umount", str(mount_dir)], stdout_log=build_log_stdout, stderr_log=build_log_stderr)
                    except Exception:
                        run_checked_with_logs(["umount", "-l", str(mount_dir)], stdout_log=build_log_stdout, stderr_log=build_log_stderr)
                    finally:
                        mounted = False

        try:
            os.replace(tmp_image, output_path)
            output_path.chmod(0o644)
            chown_owner(output_path, owner)
        except Exception:
            remove_file(tmp_image, missing_ok=True)
            raise

    except Exception:
        remove_file(tmp_image, missing_ok=True)
        raise


def load_images_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "images": []}
    try:
        payload = read_json(path, encoding="utf-8")
    except Exception:
        return {"version": 1, "images": []}
    if not isinstance(payload, dict):
        return {"version": 1, "images": []}
    images = payload.get("images")
    if not isinstance(images, list):
        images = []
    return {"version": payload.get("version", 1) if isinstance(payload.get("version", 1), int) else 1, "images": images}


def update_images_metadata(images_dir: Path, image_payload: dict[str, Any]) -> None:
    metadata_path = images_dir / "metadata.json"
    metadata = load_images_metadata(metadata_path)
    image_id = image_payload.get("id")
    kept = [entry for entry in metadata["images"] if not (isinstance(entry, dict) and entry.get("id") == image_id)]
    kept.append(
        {
            "id": image_payload["id"],
            "rollout_id": image_payload["rollout_id"],
            "kind": image_payload["kind"],
            "rootfs_path": image_payload["rootfs_path"],
            "metadata_path": str(images_dir / f"{image_payload['id']}.json"),
            "size_mb": image_payload["size_mb"],
            "created_at": image_payload["created_at"],
        }
    )
    metadata["images"] = kept
    write_json_atomic(metadata_path, metadata, pretty=True)


class RolloutImageBuilder:
    def build_from_dockerfile(
        self,
        *,
        rollout_id: str,
        source_dir: Path,
        dockerfile_path: Path,
        disk_mb: int,
        image_path: Path,
        image_metadata_path: Path,
        build_dir: Path,
        owner: str | None = None,
        force: bool = False,
    ) -> BuiltImage:
        ensure_dir(build_dir, exist_ok=True)
        ensure_dir(image_path.parent, exist_ok=True)
        if image_path.exists() and not force:
            raise RolloutConfigError(f"Rollout image already exists: {image_path}")

        stdout_log = build_dir / "build.stdout.log"
        stderr_log = build_dir / "build.stderr.log"
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")
        build_json_path = build_dir / "build.json"
        image_id = image_id_for_rollout(rollout_id)
        docker_tag = f"sparkvm-rollout:{rollout_id}"
        container_name = f"sparkvm-build-{rollout_id}"
        cache_dir = image_path.parent.parent / "cache"
        ensure_dir(cache_dir, exist_ok=True)
        rootfs_tar = cache_dir / f"rootfs-{rollout_id}.tar"
        created_at = now_utc_iso()

        if not dockerfile_path.is_file():
            raise RolloutConfigError(f"Dockerfile not found: {dockerfile_path}")

        container_created = False
        try:
            build_cmd = ["docker", "build", "-f", str(dockerfile_path), "-t", docker_tag, str(source_dir)]
            run_streamed_to_logs(build_cmd, stdout_log=stdout_log, stderr_log=stderr_log)

            inspect_payload = inspect_image(docker_tag, stdout_log=stdout_log, stderr_log=stderr_log)
            config = inspect_payload.get("Config", {})
            if not isinstance(config, dict):
                config = {}

            run_checked_with_logs(
                ["docker", "create", "--name", container_name, docker_tag],
                stdout_log=stdout_log,
                stderr_log=stderr_log,
            )
            container_created = True
            run_checked_with_logs(
                ["docker", "export", "-o", str(rootfs_tar), container_name],
                stdout_log=stdout_log,
                stderr_log=stderr_log,
            )

            docker_working_dir = config.get("WorkingDir") if isinstance(config.get("WorkingDir"), str) else None
            if not (isinstance(docker_working_dir, str) and docker_working_dir.strip()):
                docker_working_dir = "/workspace" if tar_contains_workspace(rootfs_tar) else "/"
            resolved = resolve_run_command(
                working_dir=docker_working_dir,
                docker_entrypoint=config.get("Entrypoint"),
                docker_cmd=config.get("Cmd"),
            )

            from sparkvm.runtimes.debian import SPARKVM_INIT_TEMPLATE

            convert_docker_export_to_ext4(
                tar_path=rootfs_tar,
                output_path=image_path,
                disk_mb=disk_mb,
                init_template=SPARKVM_INIT_TEMPLATE,
                build_log_stdout=stdout_log,
                build_log_stderr=stderr_log,
                owner=owner,
            )

            try:
                dockerfile_value = str(dockerfile_path.resolve().relative_to(source_dir.resolve()))
            except ValueError:
                dockerfile_value = str(dockerfile_path.resolve())

            image_payload = {
                "id": image_id,
                "rollout_id": rollout_id,
                "kind": "prepared-rollout-image",
                "dockerfile": dockerfile_value,
                "docker_image_tag": docker_tag,
                "rootfs_path": str(image_path),
                "size_mb": disk_mb,
                "resolved_run_command": asdict(resolved),
                "docker_config": {
                    "working_dir": config.get("WorkingDir"),
                    "entrypoint": config.get("Entrypoint"),
                    "cmd": config.get("Cmd"),
                    "env": config.get("Env"),
                },
                "init_template_version": INIT_TEMPLATE_VERSION,
                "created_at": created_at,
            }
            write_json_atomic(image_metadata_path, image_payload, pretty=True)
            update_images_metadata(image_path.parent, image_payload)
            write_json_atomic(
                build_json_path,
                {
                    "rollout_id": rollout_id,
                    "image_id": image_id,
                    "docker_image_tag": docker_tag,
                    "dockerfile": dockerfile_value,
                    "source_dir": str(source_dir),
                    "image_path": str(image_path),
                    "metadata_path": str(image_metadata_path),
                    "created_at": created_at,
                },
                pretty=True,
            )

            return BuiltImage(
                id=image_id,
                rollout_id=rollout_id,
                path=image_path,
                metadata_path=image_metadata_path,
                docker_image_tag=docker_tag,
                resolved_run_command=resolved,
                size_mb=disk_mb,
                created_at=created_at,
            )
        finally:
            if container_created:
                try:
                    run_checked_with_logs(
                        ["docker", "rm", "-f", container_name],
                        stdout_log=stdout_log,
                        stderr_log=stderr_log,
                    )
                except Exception:
                    pass
            try:
                remove_file(rootfs_tar, missing_ok=True)
            except OSError:
                pass
            if image_path.with_name(f"{image_path.name}.tmp").exists():
                try:
                    remove_file(image_path.with_name(f"{image_path.name}.tmp"), missing_ok=True)
                except OSError:
                    pass


__all__ = [
    "BuiltImage",
    "ResolvedRunCommand",
    "RolloutImageBuilder",
    "convert_docker_export_to_ext4",
    "image_id_for_rollout",
    "resolve_run_command",
]
