"""Rollout persistence and management."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .config import resolve_home_dir
from .errors import RolloutError, RolloutMetadataError, RolloutNotFoundError
from .runtimes.python import PYTHON_RUNTIME_ID

_ROLLOUT_ID_RE = re.compile(r"^rollout-[A-Za-z0-9_-]+$")
_METADATA_VERSION = 1


@dataclass(frozen=True)
class RolloutItem:
    id: str
    name: str
    runtime: str
    path: Path
    command: str
    files: list[str]
    created_at: str
    updated_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RolloutItem":
        try:
            return cls(
                id=str(data["id"]),
                name=str(data["name"]),
                runtime=str(data["runtime"]),
                path=Path(str(data["path"])),
                command=str(data["command"]),
                files=[str(item) for item in data["files"]],
                created_at=str(data["created_at"]),
                updated_at=data.get("updated_at"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutMetadataError(f"Invalid rollout metadata entry: {data!r}") from exc

    def to_metadata_entry(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "runtime": self.runtime,
            "path": str(self.path),
            "command": self.command,
            "files": list(self.files),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _now_utc_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_rollout_id(rollout_id: str) -> str:
    if not isinstance(rollout_id, str) or not rollout_id.strip():
        raise RolloutError("rollout_id must be a non-empty string.")
    candidate = rollout_id.strip()
    if not _ROLLOUT_ID_RE.fullmatch(candidate):
        raise RolloutError("Invalid rollout_id format. Expected values like 'rollout-abc123'.")
    return candidate


def _validate_runtime(runtime: str) -> str:
    if not isinstance(runtime, str) or not runtime.strip():
        raise RolloutError("runtime must be a non-empty string.")
    selected = runtime.strip()
    if selected != PYTHON_RUNTIME_ID:
        raise RolloutError(f"Unsupported runtime '{selected}'. Only '{PYTHON_RUNTIME_ID}' is supported.")
    return selected


def _validate_non_empty(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RolloutError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _slugify_rollout_name(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-").lower()
    return slug[:40] or "job"


def _validate_rollout_file_path(path: str) -> PurePosixPath:
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


class Rollout:
    """Rollout manager."""

    def __init__(self, home_dir: str | Path | None = None) -> None:
        self.home_dir = resolve_home_dir(home_dir)
        self.rollouts_dir = self.home_dir / "rollouts"
        self.metadata_path = self.rollouts_dir / "metadata.json"

    def create(
        self,
        *,
        name: str,
        runtime: str = PYTHON_RUNTIME_ID,
        files: dict[str, str | bytes],
        command: str,
    ) -> RolloutItem:
        
        rollout_name = _validate_non_empty(name, field_name="name")
        rollout_runtime = _validate_runtime(runtime)
        rollout_command = _validate_non_empty(command, field_name="command")

        if not isinstance(files, dict) or not files:
            raise RolloutError("files must be a non-empty dict[str, str | bytes].")

        normalized_files: list[tuple[PurePosixPath, str | bytes]] = []
        for rel_path, content in files.items():
            safe_path = _validate_rollout_file_path(rel_path)
            if not isinstance(content, (str, bytes)):
                raise RolloutError(f"Rollout file content for {rel_path!r} must be str or bytes.")
            normalized_files.append((safe_path, content))

        self.rollouts_dir.mkdir(parents=True, exist_ok=True)
        metadata = self._load_metadata()

        rollout_id = self._generate_rollout_id(
            rollout_name=rollout_name,
            existing_ids={entry.get("id") for entry in metadata["rollouts"]},
        )
        rollout_path = self.rollouts_dir / rollout_id
        created_at = _now_utc_iso()

        try:
            rollout_path.mkdir(parents=True, exist_ok=False)

            created_file_names: list[str] = []
            for safe_path, content in normalized_files:
                destination = rollout_path / Path(safe_path.as_posix())
                destination.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    destination.write_bytes(content)
                else:
                    destination.write_text(content, encoding="utf-8")
                created_file_names.append(safe_path.as_posix())

            run_sh_path = rollout_path / "run.sh"
            run_sh_path.write_text(
                f"#!/bin/sh\nset -eu\ncd /job\n{rollout_command}\n",
                encoding="utf-8",
            )
            run_sh_path.chmod(0o755)
            created_file_names.append("run.sh")

            rollout_item = RolloutItem(
                id=rollout_id,
                name=rollout_name,
                runtime=rollout_runtime,
                path=rollout_path.resolve(),
                command=rollout_command,
                files=sorted(set(created_file_names)),
                created_at=created_at,
                updated_at=None,
            )

            rollout_json = {
                "id": rollout_item.id,
                "name": rollout_item.name,
                "runtime": rollout_item.runtime,
                "command": rollout_item.command,
                "files": rollout_item.files,
                "created_at": rollout_item.created_at,
            }
            (rollout_path / "rollout.json").write_text(
                json.dumps(rollout_json, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            metadata["rollouts"].append(rollout_item.to_metadata_entry())
            self._write_metadata(metadata)
            return rollout_item
        except Exception:
            shutil.rmtree(rollout_path, ignore_errors=True)
            raise

    def list(self) -> list[RolloutItem]:
        metadata = self._load_metadata()
        items: list[RolloutItem] = []
        for entry in metadata["rollouts"]:
            rollout_item = RolloutItem.from_dict(entry)
            if rollout_item.path.is_dir():
                items.append(rollout_item)
        return items

    def get_by_id(self, rollout_id: str) -> RolloutItem:
        candidate_id = _validate_rollout_id(rollout_id)
        metadata = self._load_metadata()
        for entry in metadata["rollouts"]:
            if entry.get("id") != candidate_id:
                continue
            rollout_item = RolloutItem.from_dict(entry)
            if not rollout_item.path.is_dir():
                raise RolloutNotFoundError(f"Rollout directory missing for id '{candidate_id}'.")
            if not (rollout_item.path / "rollout.json").is_file():
                raise RolloutNotFoundError(f"rollout.json missing for id '{candidate_id}'.")
            return rollout_item
        raise RolloutNotFoundError(f"Rollout not found: {candidate_id}")

    def delete_by_id(self, rollout_id: str) -> None:
        candidate_id = _validate_rollout_id(rollout_id)
        metadata = self._load_metadata()
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
                shutil.rmtree(rollout_path)
            except OSError as exc:
                raise RolloutError(f"Could not delete rollout directory: {rollout_path}") from exc

        del rollouts[target_index]
        self._write_metadata(metadata)

    def exists(self, rollout_id: str) -> bool:
        try:
            self.get_by_id(rollout_id)
        except RolloutError:
            return False
        return True

    def _generate_rollout_id(self, *, rollout_name: str, existing_ids: set[Any]) -> str:
        name_slug = _slugify_rollout_name(rollout_name)
        for _ in range(64):
            candidate = f"rollout-{name_slug}-{secrets.token_hex(8)}"
            if candidate in existing_ids:
                continue
            if (self.rollouts_dir / candidate).exists():
                continue
            return candidate
        raise RolloutError("Could not allocate a unique rollout id.")

    def _load_metadata(self) -> dict[str, Any]:
        if not self.metadata_path.exists():
            self.rollouts_dir.mkdir(parents=True, exist_ok=True)
            return {"version": _METADATA_VERSION, "rollouts": []}

        try:
            raw = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RolloutMetadataError(f"Corrupt metadata file: {self.metadata_path}") from exc
        except OSError as exc:
            raise RolloutMetadataError(f"Could not read metadata file: {self.metadata_path}") from exc

        if not isinstance(raw, dict):
            raise RolloutMetadataError("metadata.json must contain a JSON object.")

        version = raw.get("version", _METADATA_VERSION)
        rollouts = raw.get("rollouts", [])
        if not isinstance(version, int):
            raise RolloutMetadataError("metadata.json field 'version' must be an integer.")
        if not isinstance(rollouts, list):
            raise RolloutMetadataError("metadata.json field 'rollouts' must be a list.")
        return {"version": version, "rollouts": rollouts}

    def _write_metadata(self, metadata: dict[str, Any]) -> None:
        self.rollouts_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.rollouts_dir / "metadata.json.tmp"
        payload = json.dumps(metadata, indent=2, sort_keys=True) + "\n"

        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.metadata_path)

            try:
                dir_fd = os.open(self.rollouts_dir, os.O_DIRECTORY)
            except OSError:
                return
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise RolloutMetadataError(f"Could not write metadata file: {self.metadata_path}") from exc
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


__all__ = ["Rollout"]
