"""Rollout persistence and management."""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .config import DEFAULT_RUNTIME, resolve_home_dir
from .commands import run_checked
from .errors import (
    InvalidRepoError,
    InvalidRolloutModeError,
    RolloutConfigError,
    RolloutError,
    RolloutMetadataError,
    RolloutNotFoundError,
)
from .fsops import ensure_dir, read_json, remove_tree, write_bytes, write_text
from .image import normalize_runtime_name
from .image_builder import (
    RolloutImageBuilder,
    image_id_for_rollout,
)

from .constants import (
    COPYTREE_IGNORE,
    GIT_URL_PREFIXES,
    METADATA_VERSION,
    REPO_DEFAULT_DISK_MB,
    ROLLOUT_ID_RE,
    SCRIPT_DEFAULT_DISK_MB,
    SUPPORTED_MODES,
)


@dataclass(frozen=True)
class Rollout:
    id: str
    name: str
    mode: str
    path: Path
    command: str | None
    setup_cmd: str | None
    run_cmd: str | None
    disk_mb: int
    files: list[str]
    created_at: str
    updated_at: str | None = None
    runtime: str = DEFAULT_RUNTIME
    base_image: str | None = None
    image: str | None = None
    dockerfile: str | None = None
    resolved_run_command: dict[str, Any] | None = None
    rootfs_path: str | None = None
    runtime_image: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        runtime_candidate: str | None = self.runtime if isinstance(self.runtime, str) and self.runtime.strip() else None
        if isinstance(self.base_image, str) and self.base_image.strip():
            if runtime_candidate is None or runtime_candidate == DEFAULT_RUNTIME:
                runtime_candidate = self.base_image
        if runtime_candidate is None:
            runtime_candidate = DEFAULT_RUNTIME
        normalized = normalize_runtime_name(runtime_candidate)
        object.__setattr__(self, "runtime", normalized)
        object.__setattr__(self, "base_image", normalized)

    @classmethod
    def from_rollout_json(
        cls,
        data: dict[str, Any],
        *,
        rollout_path: Path,
    ) -> "Rollout":
        if not isinstance(data, dict):
            raise RolloutMetadataError("rollout.json must contain a JSON object.")

        mode = str(data.get("mode") or "script")
        if mode not in SUPPORTED_MODES:
            raise RolloutMetadataError(f"rollout.json has unsupported mode: {mode!r}")

        command_raw = data.get("command")
        command = str(command_raw) if isinstance(command_raw, str) else None
        setup_raw = data.get("setup_cmd")
        setup_cmd = str(setup_raw) if isinstance(setup_raw, str) and setup_raw.strip() else None
        run_raw = data.get("run_cmd")
        run_cmd = str(run_raw) if isinstance(run_raw, str) and run_raw.strip() else None
        image_raw = data.get("image")
        image = str(image_raw).strip() if isinstance(image_raw, str) and image_raw.strip() else None
        dockerfile_raw = data.get("dockerfile")
        dockerfile = str(dockerfile_raw).strip() if isinstance(dockerfile_raw, str) and dockerfile_raw.strip() else None
        resolved_raw = data.get("resolved_run_command")
        resolved_run_command = resolved_raw if isinstance(resolved_raw, dict) else None
        rootfs_path_raw = data.get("rootfs_path")
        rootfs_path = str(rootfs_path_raw) if isinstance(rootfs_path_raw, str) and rootfs_path_raw.strip() else None
        runtime_image_raw = data.get("runtime_image")
        runtime_image = runtime_image_raw if isinstance(runtime_image_raw, dict) else None
        if rootfs_path is None and isinstance(runtime_image, dict):
            runtime_image_path_raw = runtime_image.get("path")
            if isinstance(runtime_image_path_raw, str) and runtime_image_path_raw.strip():
                rootfs_path = runtime_image_path_raw

        if run_cmd is None and command is not None:
            run_cmd = command
        if run_cmd is None and isinstance(resolved_run_command, dict):
            resolved_command_raw = resolved_run_command.get("command")
            if isinstance(resolved_command_raw, str) and resolved_command_raw.strip():
                run_cmd = resolved_command_raw.strip()

        disk_mb_raw = data.get("disk_mb")
        if isinstance(disk_mb_raw, int):
            disk_mb = disk_mb_raw
        else:
            disk_mb = REPO_DEFAULT_DISK_MB if mode == "repo" else SCRIPT_DEFAULT_DISK_MB

        files_raw = data.get("files")
        if isinstance(files_raw, list):
            files = [str(item) for item in files_raw]
        else:
            files = []

        runtime_raw = data.get("runtime")
        if not isinstance(runtime_raw, str) or not runtime_raw.strip():
            runtime_raw = str(data.get("base_image") or DEFAULT_RUNTIME)

        try:
            return cls(
                id=str(data["id"]),
                name=str(data["name"]),
                mode=mode,
                runtime=normalize_runtime_name(runtime_raw),
                path=rollout_path.resolve(),
                command=command,
                setup_cmd=setup_cmd,
                run_cmd=run_cmd,
                disk_mb=disk_mb,
                files=files,
                created_at=str(data["created_at"]),
                updated_at=data.get("updated_at"),
                image=image,
                dockerfile=dockerfile,
                resolved_run_command=resolved_run_command,
                rootfs_path=rootfs_path,
                runtime_image=runtime_image,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutMetadataError(f"Invalid rollout.json content: {data!r}") from exc

    @classmethod
    def from_metadata_entry(cls, data: dict[str, Any]) -> "Rollout":
        try:
            mode = str(data.get("mode") or "script")
            command_raw = data.get("command")
            command = str(command_raw) if isinstance(command_raw, str) else None
            run_cmd_raw = data.get("run_cmd")
            run_cmd = str(run_cmd_raw) if isinstance(run_cmd_raw, str) else command
            setup_cmd_raw = data.get("setup_cmd")
            setup_cmd = str(setup_cmd_raw) if isinstance(setup_cmd_raw, str) else None
            image_raw = data.get("image")
            image = str(image_raw).strip() if isinstance(image_raw, str) and image_raw.strip() else None
            dockerfile_raw = data.get("dockerfile")
            dockerfile = str(dockerfile_raw).strip() if isinstance(dockerfile_raw, str) and dockerfile_raw.strip() else None
            resolved_raw = data.get("resolved_run_command")
            resolved_run_command = resolved_raw if isinstance(resolved_raw, dict) else None
            rootfs_path_raw = data.get("rootfs_path")
            rootfs_path = str(rootfs_path_raw) if isinstance(rootfs_path_raw, str) and rootfs_path_raw.strip() else None
            runtime_image_raw = data.get("runtime_image")
            runtime_image = runtime_image_raw if isinstance(runtime_image_raw, dict) else None
            if rootfs_path is None and isinstance(runtime_image, dict):
                runtime_image_path_raw = runtime_image.get("path")
                if isinstance(runtime_image_path_raw, str) and runtime_image_path_raw.strip():
                    rootfs_path = runtime_image_path_raw
            if run_cmd is None and isinstance(resolved_run_command, dict):
                resolved_command_raw = resolved_run_command.get("command")
                if isinstance(resolved_command_raw, str) and resolved_command_raw.strip():
                    run_cmd = resolved_command_raw.strip()
            disk_mb_raw = data.get("disk_mb")
            if isinstance(disk_mb_raw, int):
                disk_mb = disk_mb_raw
            else:
                disk_mb = REPO_DEFAULT_DISK_MB if mode == "repo" else SCRIPT_DEFAULT_DISK_MB

            runtime_raw = data.get("runtime")
            if not isinstance(runtime_raw, str) or not runtime_raw.strip():
                runtime_raw = str(data.get("base_image") or DEFAULT_RUNTIME)

            return cls(
                id=str(data["id"]),
                name=str(data["name"]),
                mode=mode,
                runtime=normalize_runtime_name(runtime_raw),
                path=Path(str(data["path"])),
                command=command,
                setup_cmd=setup_cmd,
                run_cmd=run_cmd,
                disk_mb=disk_mb,
                files=[str(item) for item in data.get("files", [])],
                created_at=str(data["created_at"]),
                updated_at=data.get("updated_at"),
                image=image,
                dockerfile=dockerfile,
                resolved_run_command=resolved_run_command,
                rootfs_path=rootfs_path,
                runtime_image=runtime_image,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutMetadataError(f"Invalid rollout metadata entry: {data!r}") from exc

    def to_metadata_entry(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "mode": self.mode,
            "runtime": self.runtime,
            "path": str(self.path),
            "command": self.command,
            "setup_cmd": self.setup_cmd,
            "run_cmd": self.run_cmd,
            "image": self.image,
            "dockerfile": self.dockerfile,
            "resolved_run_command": self.resolved_run_command,
            "rootfs_path": self.rootfs_path,
            "runtime_image": self.runtime_image,
            "disk_mb": self.disk_mb,
            "files": list(self.files),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def now_utc_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_rollout_id(rollout_id: str) -> str:
    if not isinstance(rollout_id, str) or not rollout_id.strip():
        raise RolloutError("rollout_id must be a non-empty string.")
    candidate = rollout_id.strip()
    if not ROLLOUT_ID_RE.fullmatch(candidate):
        raise RolloutError("Invalid rollout_id format. Expected values like 'rollout-abc123'.")
    return candidate


def validate_runtime(runtime: str) -> str:
    if not isinstance(runtime, str) or not runtime.strip():
        raise RolloutError("runtime must be a non-empty string.")
    return normalize_runtime_name(runtime)


def validate_non_empty(value: str | None, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RolloutError(f"{field_name} must be a non-empty string.")
    return value.strip()


def validate_rollout_mode(mode: str) -> str:
    if not isinstance(mode, str) or not mode.strip():
        raise InvalidRolloutModeError("mode must be a non-empty string.")
    selected = mode.strip().lower()
    if selected not in SUPPORTED_MODES:
        raise InvalidRolloutModeError(f"Unsupported rollout mode '{selected}'. Supported modes: script, repo.")
    return selected


def slugify_rollout_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-").lower()
    return slug[:40] or "job"


def validate_rollout_file_path(path: str) -> PurePosixPath:
    if not isinstance(path, str):
        raise RolloutError("Rollout file paths must be strings.")

    raw = path.strip()
    if not raw:
        raise RolloutError("Rollout file path cannot be empty.")
    if raw.startswith("/"):
        raise RolloutError(f"Rollout file path must be relative: {path!r}")

    segments = raw.split("/")
    if any(segment == "" for segment in segments):
        raise RolloutError(f"Rollout file path contains empty path segments: {path!r}")
    if any(segment in {".", ".."} for segment in segments):
        raise RolloutError(f"Rollout file path cannot contain '.' or '..': {path!r}")

    normalized = PurePosixPath(raw)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise RolloutError(f"Rollout file path escapes rollout directory: {path!r}")
    if normalized.name in {"", ".", ".."}:
        raise RolloutError(f"Rollout file path must include a file name: {path!r}")
    return normalized


def is_git_url(source: str) -> bool:
    return source.startswith(GIT_URL_PREFIXES) or source.endswith(".git")


def run_git_checked(cmd: list[str], *, cwd: Path | None = None, error_cls: type[RolloutError] = RolloutError) -> str:
    completed = run_checked(cmd, error_factory=error_cls, cwd=cwd)
    return completed.stdout.strip()


@dataclass(frozen=True)
class ResolvedCommand:
    source: str
    working_dir: str
    command: str
    entrypoint: list[str] | str | None
    cmd: list[str] | str | None


def shell_quote(arg: str) -> str:
    return shlex.quote(arg)


def command_value_to_shell(value: list[str] | tuple[str, ...] | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    if isinstance(value, (list, tuple)):
        parts = [str(part) for part in value if str(part).strip()]
        if not parts:
            return None
        return " ".join(shell_quote(part) for part in parts)
    raise RolloutConfigError(f"Unsupported Docker command value type: {type(value).__name__}")


def normalize_command_value(value: object) -> list[str] | str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    if isinstance(value, (list, tuple)):
        parts = [str(part) for part in value if str(part).strip()]
        return parts if parts else None
    raise RolloutConfigError(f"Unsupported Docker command value type: {type(value).__name__}")


def resolve_container_command(
    *,
    run_cmd: str | None,
    docker_entrypoint: list[str] | str | None,
    docker_cmd: list[str] | str | None,
    working_dir: str | None,
) -> ResolvedCommand:
    resolved_working_dir = working_dir.strip() if isinstance(working_dir, str) and working_dir.strip() else "/workspace"
    normalized_entrypoint = normalize_command_value(docker_entrypoint)
    normalized_cmd = normalize_command_value(docker_cmd)

    if isinstance(run_cmd, str) and run_cmd.strip():
        return ResolvedCommand(
            source="run_cmd",
            working_dir=resolved_working_dir,
            command=run_cmd.strip(),
            entrypoint=normalized_entrypoint,
            cmd=normalized_cmd,
        )

    entrypoint_shell = command_value_to_shell(normalized_entrypoint)
    cmd_shell = command_value_to_shell(normalized_cmd)
    if entrypoint_shell is None and cmd_shell is None:
        raise RolloutConfigError("Dockerfile rollout requires either run_cmd or Dockerfile CMD/ENTRYPOINT.")

    if entrypoint_shell and cmd_shell:
        command = f"{entrypoint_shell} {cmd_shell}"
    else:
        command = entrypoint_shell or cmd_shell or ""

    return ResolvedCommand(
        source="docker_config",
        working_dir=resolved_working_dir,
        command=command,
        entrypoint=normalized_entrypoint,
        cmd=normalized_cmd,
    )


class Rollouts:
    """Rollout manager."""

    def __init__(self, home_dir: str | Path | None = None) -> None:
        self.home_dir = resolve_home_dir(home_dir)
        self.rollouts_dir = self.home_dir / "rollouts"
        self.metadata_path = self.rollouts_dir / "metadata.json"

    def create(
        self,
        *,
        name: str,
        mode: str = "repo",
        runtime: str = DEFAULT_RUNTIME,
        files: dict[str, str | bytes] | None = None,
        source: str | Path | None = None,
        ref: str | None = None,
        dockerfile: str | Path = "Dockerfile",
        run_cmd: str | None = None,
        disk_mb: int | None = None,
        command: str | None = None,
        base_image: str | None = None,
        **kwargs: Any,
    ) -> Rollout:
        if "image" in kwargs:
            raise RolloutConfigError("image= is not supported in Dockerfile-only mode. Provide dockerfile='Dockerfile'.")
        if "setup_cmd" in kwargs:
            raise RolloutConfigError(
                "setup_cmd is not supported in Dockerfile-only mode. Put dependency installation in the Dockerfile."
            )
        if kwargs:
            unexpected = ", ".join(sorted(str(key) for key in kwargs))
            raise RolloutConfigError(f"Unsupported rollout create parameter(s): {unexpected}")

        rollout_name = validate_non_empty(name, field_name="name")
        rollout_mode = validate_rollout_mode(mode)
        rollout_runtime = validate_runtime(base_image if base_image is not None else runtime)

        rollout_dockerfile = str(dockerfile).strip() if dockerfile is not None else ""
        if rollout_mode == "repo" and not rollout_dockerfile:
            raise RolloutConfigError("dockerfile is required for mode='repo'.")

        resolved_run_cmd = run_cmd.strip() if isinstance(run_cmd, str) and run_cmd.strip() else None
        if resolved_run_cmd is None and isinstance(command, str) and command.strip():
            resolved_run_cmd = command.strip()
        rollout_run_cmd = resolved_run_cmd

        if disk_mb is None:
            rollout_disk_mb = REPO_DEFAULT_DISK_MB if rollout_mode == "repo" else SCRIPT_DEFAULT_DISK_MB
        elif isinstance(disk_mb, bool) or not isinstance(disk_mb, int) or disk_mb <= 0:
            raise RolloutError("disk_mb must be a positive integer.")
        else:
            rollout_disk_mb = disk_mb

        ensure_dir(self.rollouts_dir, exist_ok=True)
        metadata = self.load_metadata()
        metadata = self.drop_existing_rollouts_with_same_name(metadata=metadata, rollout_name=rollout_name)

        rollout_id = self.generate_rollout_id(
            rollout_name=rollout_name,
            existing_ids={entry.get("id") for entry in metadata["rollouts"]},
        )
        rollout_path = self.rollouts_dir / rollout_id
        created_at = now_utc_iso()

        try:
            ensure_dir(rollout_path, exist_ok=False)

            if rollout_mode == "script":
                rollout_item = self.create_script_rollout(
                    rollout_id=rollout_id,
                    rollout_name=rollout_name,
                    rollout_runtime=rollout_runtime,
                    rollout_path=rollout_path,
                    files=files,
                    setup_cmd=None,
                    run_cmd=validate_non_empty(rollout_run_cmd, field_name="run_cmd"),
                    disk_mb=rollout_disk_mb,
                    created_at=created_at,
                )
            else:
                rollout_item = self.create_repo_rollout(
                    rollout_id=rollout_id,
                    rollout_name=rollout_name,
                    rollout_runtime=rollout_runtime,
                    rollout_path=rollout_path,
                    source=source,
                    dockerfile=rollout_dockerfile,
                    run_cmd=rollout_run_cmd,
                    ref=ref,
                    disk_mb=rollout_disk_mb,
                    created_at=created_at,
                )

            metadata["rollouts"].append(rollout_item.to_metadata_entry())
            self.write_metadata(metadata)
            return rollout_item
        except Exception:
            remove_tree(rollout_path, ignore_errors=True)
            raise

    def drop_existing_rollouts_with_same_name(self, *, metadata: dict[str, Any], rollout_name: str) -> dict[str, Any]:
        rollouts_raw = metadata.get("rollouts", [])
        if not isinstance(rollouts_raw, list):
            return metadata

        kept: list[dict[str, Any]] = []
        removed_any = False

        for entry in rollouts_raw:
            entry_name = entry.get("name") if isinstance(entry, dict) else None
            if entry_name != rollout_name:
                if isinstance(entry, dict):
                    kept.append(entry)
                continue

            removed_any = True
            rollout_path_raw = entry.get("path") if isinstance(entry, dict) else None
            if isinstance(rollout_path_raw, str) and rollout_path_raw.strip():
                rollout_path = Path(rollout_path_raw)
                if rollout_path.exists():
                    try:
                        remove_tree(rollout_path, ignore_errors=False)
                    except OSError as exc:
                        raise RolloutError(f"Could not replace existing rollout directory: {rollout_path}") from exc

        if not removed_any:
            return metadata

        updated = dict(metadata)
        updated["rollouts"] = kept
        self.write_metadata(updated)
        return updated

    def create_script_rollout(
        self,
        *,
        rollout_id: str,
        rollout_name: str,
        rollout_runtime: str,
        rollout_path: Path,
        files: dict[str, str | bytes] | None,
        setup_cmd: str | None,
        run_cmd: str,
        disk_mb: int,
        created_at: str,
    ) -> Rollout:
        if not isinstance(files, dict) or not files:
            raise RolloutError("files must be a non-empty dict[str, str | bytes].")

        normalized_files: list[tuple[PurePosixPath, str | bytes]] = []
        for rel_path, content in files.items():
            safe_path = validate_rollout_file_path(rel_path)
            if not isinstance(content, (str, bytes)):
                raise RolloutError(f"Rollout file content for {rel_path!r} must be str or bytes.")
            normalized_files.append((safe_path, content))

        created_file_names: list[str] = []
        for safe_path, content in normalized_files:
            destination = rollout_path / Path(safe_path.as_posix())
            ensure_dir(destination.parent, exist_ok=True)
            if isinstance(content, bytes):
                write_bytes(destination, content)
            else:
                write_text(destination, content, encoding="utf-8")
            created_file_names.append(safe_path.as_posix())

        setup_script = setup_cmd.strip() if isinstance(setup_cmd, str) and setup_cmd.strip() else None
        if setup_script is not None:
            setup_sh_path = rollout_path / "setup.sh"
            write_text(setup_sh_path, f"#!/bin/sh\nset -eu\ncd /job\n{setup_script}\n", encoding="utf-8")
            setup_sh_path.chmod(0o755)
            created_file_names.append("setup.sh")

        run_sh_path = rollout_path / "run.sh"
        write_text(run_sh_path, f"#!/bin/sh\nset -eu\ncd /job\n{run_cmd}\n", encoding="utf-8")
        run_sh_path.chmod(0o755)
        created_file_names.append("run.sh")

        files_list = sorted(set(created_file_names))
        rollout_json = {
            "id": rollout_id,
            "name": rollout_name,
            "mode": "script",
            "runtime": rollout_runtime,
            "command": run_cmd,
            "setup_cmd": setup_script,
            "run_cmd": run_cmd,
            "files": files_list,
            "disk_mb": disk_mb,
            "created_at": created_at,
        }
        self.write_rollout_json(rollout_path / "rollout.json", rollout_json)

        return Rollout(
            id=rollout_id,
            name=rollout_name,
            mode="script",
            runtime=rollout_runtime,
            path=rollout_path.resolve(),
            command=run_cmd,
            setup_cmd=setup_script,
            run_cmd=run_cmd,
            disk_mb=disk_mb,
            files=files_list,
            created_at=created_at,
            updated_at=None,
        )

    def create_repo_rollout(
        self,
        *,
        rollout_id: str,
        rollout_name: str,
        rollout_runtime: str,
        rollout_path: Path,
        source: str | Path | None,
        dockerfile: str | None,
        run_cmd: str | None,
        ref: str | None,
        disk_mb: int,
        created_at: str,
    ) -> Rollout:
        if source is None:
            raise InvalidRepoError("source is required for mode='repo'.")
        raw_source = str(source).strip()
        if not raw_source:
            raise InvalidRepoError("source must be a non-empty path or git URL for mode='repo'.")
        if dockerfile is None or not str(dockerfile).strip():
            raise RolloutConfigError("dockerfile is required for mode='repo'.")

        source_dir = rollout_path / "source"
        source_payload: dict[str, Any]
        if is_git_url(raw_source):
            source_payload = self.prepare_repo_from_git_url(repo_dir=source_dir, source=raw_source, ref=ref)
        else:
            source_payload = self.prepare_repo_from_local_path(repo_dir=source_dir, source=Path(raw_source), ref=ref)

        return self.create_repo_containerized_rollout(
            rollout_id=rollout_id,
            rollout_name=rollout_name,
            rollout_runtime=rollout_runtime,
            rollout_path=rollout_path,
            source_dir=source_dir,
            source_payload=source_payload,
            dockerfile=str(dockerfile),
            run_cmd=run_cmd,
            disk_mb=disk_mb,
            created_at=created_at,
        )

    def create_repo_containerized_rollout(
        self,
        *,
        rollout_id: str,
        rollout_name: str,
        rollout_runtime: str,
        rollout_path: Path,
        source_dir: Path,
        source_payload: dict[str, Any],
        dockerfile: str,
        run_cmd: str | None,
        disk_mb: int,
        created_at: str,
    ) -> Rollout:
        source_prefix = "source/"
        created_files: list[str] = [source_prefix, "run.sh", "rollout.json", "build/"]

        dockerfile_path = self.resolve_dockerfile_path(dockerfile=dockerfile, source_dir=source_dir)
        image_id = image_id_for_rollout(rollout_id)
        image_path = self.home_dir / "images" / f"{image_id}.ext4"
        image_metadata_path = self.home_dir / "images" / f"{image_id}.json"
        build_dir = rollout_path / "build"
        built_image = RolloutImageBuilder().build_from_dockerfile(
            rollout_id=rollout_id,
            source_dir=source_dir,
            dockerfile_path=dockerfile_path,
            run_cmd=run_cmd,
            disk_mb=disk_mb,
            image_path=image_path,
            image_metadata_path=image_metadata_path,
            build_dir=build_dir,
        )
        resolved = built_image.resolved_run_command

        run_sh_path = rollout_path / "run.sh"
        self.write_run_script(
            run_sh_path=run_sh_path,
            working_dir=resolved.working_dir,
            command=resolved.command,
        )

        try:
            dockerfile_value = str(dockerfile_path.resolve().relative_to(source_dir.resolve()))
        except ValueError:
            dockerfile_value = str(dockerfile_path.resolve())

        resolved_payload = {
            "source": resolved.source,
            "working_dir": resolved.working_dir,
            "command": resolved.command,
            "entrypoint": resolved.entrypoint,
            "cmd": resolved.cmd,
        }
        runtime_image_payload = {
            "id": built_image.id,
            "path": str(built_image.path),
            "metadata_path": str(built_image.metadata_path),
        }
        rollout_json = {
            "id": rollout_id,
            "name": rollout_name,
            "mode": "repo",
            "runtime": rollout_runtime,
            "source": source_payload,
            "dockerfile": dockerfile_value,
            "run_cmd": run_cmd,
            "resolved_run_command": resolved_payload,
            "runtime_image": runtime_image_payload,
            "rootfs_path": str(built_image.path),
            "files": created_files,
            "disk_mb": disk_mb,
            "created_at": created_at,
        }
        self.write_rollout_json(rollout_path / "rollout.json", rollout_json)

        return Rollout(
            id=rollout_id,
            name=rollout_name,
            mode="repo",
            runtime=rollout_runtime,
            path=rollout_path.resolve(),
            command=None,
            setup_cmd=None,
            run_cmd=resolved.command,
            disk_mb=disk_mb,
            files=created_files,
            created_at=created_at,
            updated_at=None,
            image=None,
            dockerfile=dockerfile_value,
            resolved_run_command=resolved_payload,
            rootfs_path=str(built_image.path),
            runtime_image=runtime_image_payload,
        )

    def resolve_dockerfile_path(self, *, dockerfile: str, source_dir: Path) -> Path:
        candidate = Path(dockerfile).expanduser()
        path_candidate = candidate if candidate.is_absolute() else source_dir / candidate
        if path_candidate.is_file():
            return path_candidate.resolve()

        raise RolloutConfigError(f"Dockerfile not found: {path_candidate}")

    def write_run_script(self, *, run_sh_path: Path, working_dir: str, command: str) -> None:
        if not command.strip():
            raise RolloutConfigError("Resolved run command is empty.")
        write_text(
            run_sh_path,
            f"#!/bin/sh\nset -eu\ncd {shell_quote(working_dir)}\nexec {command}\n",
            encoding="utf-8",
        )
        run_sh_path.chmod(0o755)

    def prepare_repo_from_local_path(self, *, repo_dir: Path, source: Path, ref: str | None = None) -> dict[str, Any]:
        source_path = source.expanduser().resolve()
        if not source_path.exists():
            raise InvalidRepoError(f"Local repo source does not exist: {source_path}")
        if not source_path.is_dir():
            raise InvalidRepoError(f"Local repo source must be a directory: {source_path}")
        git_dir = source_path / ".git"
        if not git_dir.is_dir():
            raise InvalidRepoError("Local repo source must be a Git repository containing a .git directory.")

        payload: dict[str, Any] = {
            "type": "local",
            "path": str(source_path),
        }
        if isinstance(ref, str) and ref.strip():
            clean_ref = ref.strip()
            run_git_checked(["git", "clone", str(source_path), str(repo_dir)], error_cls=InvalidRepoError)
            run_git_checked(["git", "checkout", clean_ref], cwd=repo_dir, error_cls=InvalidRepoError)
            payload["ref"] = clean_ref
            commit = run_git_checked(["git", "rev-parse", "HEAD"], cwd=repo_dir, error_cls=InvalidRepoError)
            cloned_git_dir = repo_dir / ".git"
            if cloned_git_dir.exists():
                remove_tree(cloned_git_dir, ignore_errors=True)
        else:
            shutil.copytree(source_path, repo_dir, symlinks=True, ignore=COPYTREE_IGNORE)
            commit = run_git_checked(["git", "rev-parse", "HEAD"], cwd=source_path, error_cls=InvalidRepoError)

        payload["commit"] = commit
        return payload

    def prepare_repo_from_git_url(self, *, repo_dir: Path, source: str, ref: str | None) -> dict[str, Any]:
        run_git_checked(["git", "clone", source, str(repo_dir)], error_cls=InvalidRepoError)

        source_payload: dict[str, Any] = {
            "type": "git",
            "url": source,
        }

        if isinstance(ref, str) and ref.strip():
            clean_ref = ref.strip()
            run_git_checked(["git", "checkout", clean_ref], cwd=repo_dir, error_cls=InvalidRepoError)
            source_payload["ref"] = clean_ref

        commit = run_git_checked(["git", "rev-parse", "HEAD"], cwd=repo_dir, error_cls=InvalidRepoError)
        source_payload["commit"] = commit

        git_dir = repo_dir / ".git"
        if git_dir.exists():
            remove_tree(git_dir, ignore_errors=True)

        return source_payload

    def list(self) -> list[Rollout]:
        metadata = self.load_metadata()
        items: list[Rollout] = []
        for entry in metadata["rollouts"]:
            rollout_stub = Rollout.from_metadata_entry(entry)
            if not rollout_stub.path.is_dir():
                continue
            rollout_json_path = rollout_stub.path / "rollout.json"
            if not rollout_json_path.is_file():
                continue
            rollout_obj = self.load_rollout_json(rollout_json_path)
            items.append(rollout_obj)
        return items

    def get_by_id(self, rollout_id: str) -> Rollout:
        candidate_id = validate_rollout_id(rollout_id)
        metadata = self.load_metadata()
        for entry in metadata["rollouts"]:
            if entry.get("id") != candidate_id:
                continue
            rollout_item = Rollout.from_metadata_entry(entry)
            if not rollout_item.path.is_dir():
                raise RolloutNotFoundError(f"Rollout directory missing for id '{candidate_id}'.")
            rollout_json_path = rollout_item.path / "rollout.json"
            if not rollout_json_path.is_file():
                raise RolloutNotFoundError(f"rollout.json missing for id '{candidate_id}'.")
            return self.load_rollout_json(rollout_json_path)
        raise RolloutNotFoundError(f"Rollout not found: {candidate_id}")

    def delete_by_id(self, rollout_id: str) -> None:
        candidate_id = validate_rollout_id(rollout_id)
        metadata = self.load_metadata()
        rollouts = metadata["rollouts"]

        target_index = -1
        target_entry: dict[str, Any] | None = None
        for index, entry in enumerate(rollouts):
            if entry.get("id") == candidate_id:
                target_index = index
                target_entry = entry
                break

        if target_index < 0 or target_entry is None:
            raise RolloutNotFoundError(f"Rollout not found: {candidate_id}")

        rollout_path = Path(str(target_entry["path"]))
        if rollout_path.exists():
            try:
                remove_tree(rollout_path, ignore_errors=False)
            except OSError as exc:
                raise RolloutError(f"Could not delete rollout directory: {rollout_path}") from exc

        del rollouts[target_index]
        self.write_metadata(metadata)

    def exists(self, rollout_id: str) -> bool:
        try:
            self.get_by_id(rollout_id)
        except RolloutError:
            return False
        return True

    def generate_rollout_id(self, *, rollout_name: str, existing_ids: set[Any]) -> str:
        name_slug = slugify_rollout_name(rollout_name)
        for _ in range(64):
            candidate = f"rollout-{name_slug}-{secrets.token_hex(8)}"
            if candidate in existing_ids:
                continue
            if (self.rollouts_dir / candidate).exists():
                continue
            return candidate
        raise RolloutError("Could not allocate a unique rollout id.")

    def load_rollout_json(self, rollout_json_path: Path) -> Rollout:
        try:
            payload = read_json(rollout_json_path, encoding="utf-8")
        except json.JSONDecodeError as exc:
            raise RolloutMetadataError(f"Corrupt rollout file: {rollout_json_path}") from exc
        except OSError as exc:
            raise RolloutMetadataError(f"Could not read rollout file: {rollout_json_path}") from exc
        return Rollout.from_rollout_json(payload, rollout_path=rollout_json_path.parent)

    def load_metadata(self) -> dict[str, Any]:
        if not self.metadata_path.exists():
            ensure_dir(self.rollouts_dir, exist_ok=True)
            return {"version": METADATA_VERSION, "rollouts": []}

        try:
            raw = read_json(self.metadata_path, encoding="utf-8")
        except json.JSONDecodeError as exc:
            raise RolloutMetadataError(f"Corrupt metadata file: {self.metadata_path}") from exc
        except OSError as exc:
            raise RolloutMetadataError(f"Could not read metadata file: {self.metadata_path}") from exc

        if not isinstance(raw, dict):
            raise RolloutMetadataError("metadata.json must contain a JSON object.")

        version = raw.get("version", METADATA_VERSION)
        rollouts = raw.get("rollouts", [])
        if not isinstance(version, int):
            raise RolloutMetadataError("metadata.json field 'version' must be an integer.")
        if not isinstance(rollouts, list):
            raise RolloutMetadataError("metadata.json field 'rollouts' must be a list.")
        return {"version": version, "rollouts": rollouts}

    def write_rollout_json(self, path: Path, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        write_text(path, text, encoding="utf-8")

    def write_metadata(self, metadata: dict[str, Any]) -> None:
        ensure_dir(self.rollouts_dir, exist_ok=True)
        tmp_path = self.rollouts_dir / "metadata.json.tmp"
        final_path = self.metadata_path
        text = json.dumps(metadata, indent=2, sort_keys=True) + "\n"

        try:
            write_text(tmp_path, text, encoding="utf-8")
            os.replace(tmp_path, final_path)
        except OSError as exc:
            raise RolloutMetadataError(f"Could not write metadata file: {self.metadata_path}") from exc
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


# Backward compatibility alias.
RolloutManager = Rollouts

__all__ = [
    "Rollout",
    "ResolvedCommand",
    "resolve_container_command",
    "Rollouts",
    "RolloutManager",
]
