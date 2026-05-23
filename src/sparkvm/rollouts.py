"""Dockerfile-only rollout persistence and management."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_RUNTIME, resolve_home_dir
from .constants import METADATA_VERSION, ROLLOUT_ID_RE
from .errors import RolloutConfigError, RolloutError, RolloutMetadataError, RolloutNotFoundError
from .fsops import ensure_dir, read_json, remove_file, remove_tree, write_json_atomic, write_text
from .image_builder import RolloutImageBuilder, image_id_for_rollout
from .logger import create_flow_logger, configure_logging
from .utils import now_utc_iso, shell_quote


@dataclass(frozen=True)
class Rollout:
    id: str
    name: str
    runtime: str
    path: Path
    image_path: str
    delete_on_success: bool
    created_at: str
    runtime_image: dict[str, Any]
    dockerfile: str
    resolved_run_command: dict[str, Any]

    @property
    def mode(self) -> str:
        return "dockerfile"

    @property
    def base_image(self) -> str:
        return self.runtime

    @property
    def rootfs_path(self) -> str:
        return self.image_path

    def to_metadata_entry(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "runtime": self.runtime,
            "path": str(self.path),
            "image_path": self.image_path,
            "deleteOnSuccess": self.delete_on_success,
            "created_at": self.created_at,
            "runtime_image": dict(self.runtime_image),
            "dockerfile": self.dockerfile,
            "resolved_run_command": dict(self.resolved_run_command),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, rollout_path: Path) -> "Rollout":
        if not isinstance(payload, dict):
            raise RolloutMetadataError("rollout metadata payload must be a JSON object.")

        runtime = payload.get("runtime", DEFAULT_RUNTIME)
        if not isinstance(runtime, str) or runtime.strip().lower() != "dockerfile":
            raise RolloutMetadataError("rollout runtime must be 'Dockerfile'.")

        image_path = payload.get("image_path")
        if not isinstance(image_path, str) or not image_path.strip():
            raise RolloutMetadataError("rollout metadata missing image_path.")

        runtime_image = payload.get("runtime_image")
        if not isinstance(runtime_image, dict):
            raise RolloutMetadataError("rollout metadata missing runtime_image object.")

        dockerfile = payload.get("dockerfile")
        if not isinstance(dockerfile, str) or not dockerfile.strip():
            raise RolloutMetadataError("rollout metadata missing dockerfile value.")

        resolved_run_command = payload.get("resolved_run_command")
        if not isinstance(resolved_run_command, dict):
            raise RolloutMetadataError("rollout metadata missing resolved_run_command object.")

        try:
            return cls(
                id=str(payload["id"]),
                name=str(payload["name"]),
                runtime="Dockerfile",
                path=rollout_path.resolve(),
                image_path=image_path,
                delete_on_success=bool(payload.get("deleteOnSuccess", False)),
                created_at=str(payload["created_at"]),
                runtime_image=runtime_image,
                dockerfile=dockerfile,
                resolved_run_command=resolved_run_command,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RolloutMetadataError("rollout metadata is invalid.") from exc


def validate_rollout_id(rollout_id: str) -> str:
    if not isinstance(rollout_id, str) or not rollout_id.strip():
        raise RolloutError("rollout_id must be a non-empty string.")
    candidate = rollout_id.strip()
    if not ROLLOUT_ID_RE.fullmatch(candidate):
        raise RolloutError("Invalid rollout_id format. Expected values like 'rollout-abc123'.")
    return candidate


class Rollouts:
    """Dockerfile-only rollout manager."""

    def __init__(self, home_dir: str | Path | None = None) -> None:
        self.home_dir = resolve_home_dir(home_dir)
        configure_logging(home_dir=self.home_dir)
        self.rollouts_dir = self.home_dir / "rollouts"
        self.metadata_path = self.rollouts_dir / "metadata.json"

    def create(
        self,
        *,
        name: str,
        runtime: str = DEFAULT_RUNTIME,
        deleteOnSuccess: bool = False,
        dockerfile: str | Path = "Dockerfile",
        **kwargs: Any,
    ) -> Rollout:
        create_started_at = time.monotonic()
        flow = create_flow_logger(
            name="sparkvm.rollouts.create",
            home_dir=self.home_dir,
            context={"op": "rollout_create"},
        )
        if kwargs:
            unexpected = ", ".join(sorted(str(key) for key in kwargs))
            raise RolloutConfigError(f"Unsupported rollout create parameter(s): {unexpected}")

        if not isinstance(name, str) or not name.strip():
            raise RolloutConfigError("name must be a non-empty string.")
        rollout_name = name.strip()
        flow.event(state=f"[Rollout] create started name={rollout_name} runtime={runtime}")

        if not isinstance(runtime, str) or runtime.strip().lower() != "dockerfile":
            raise RolloutConfigError("runtime must be 'Dockerfile'.")

        if not isinstance(deleteOnSuccess, bool):
            raise RolloutConfigError("deleteOnSuccess must be a boolean.")

        source_dir = Path.cwd().resolve()
        dockerfile_candidate = Path(dockerfile).expanduser()
        if dockerfile_candidate.is_absolute():
            dockerfile_path = dockerfile_candidate.resolve()
        else:
            dockerfile_path = (source_dir / dockerfile_candidate).resolve()
        if not dockerfile_path.is_file():
            raise RolloutConfigError(f"Dockerfile not found at: {dockerfile_path}")
        flow.event(state=f"[Rollout] validated dockerfile={dockerfile_path}")

        ensure_dir(self.rollouts_dir, exist_ok=True)
        metadata = self.load_metadata()
        existing = self.find_existing_rollout_by_name(metadata=metadata, rollout_name=rollout_name)
        if existing is not None:
            flow.event(state=f"[Rollout] reused existing rollout_id={existing.id}")
            flow.close()
            return existing
        metadata = self.drop_existing_rollouts_with_same_name(metadata=metadata, rollout_name=rollout_name)

        rollout_id = self.generate_rollout_id(
            rollout_name=rollout_name,
            existing_ids={entry.get("id") for entry in metadata["rollouts"] if isinstance(entry, dict)},
        )
        rollout_path = self.rollouts_dir / rollout_id
        created_at = now_utc_iso()

        image_id = image_id_for_rollout(rollout_id)
        image_path = self.home_dir / "images" / f"{image_id}.ext4"
        image_metadata_path = self.home_dir / "images" / f"{image_id}.json"
        build_dir = rollout_path / "build"
        create_flow = None

        try:
            ensure_dir(rollout_path, exist_ok=False)
            create_flow = create_flow_logger(
                name=f"sparkvm.rollouts.create.{rollout_id}",
                home_dir=self.home_dir,
                context={"op": "rollout_create", "rollout_id": rollout_id},
                file_path=rollout_path / "create.log",
            )
            create_flow.event(state="[Rollout] prepare complete")
            create_flow.event(state="[Rollout] docker build started")
            built_image = RolloutImageBuilder().build_from_dockerfile(
                rollout_id=rollout_id,
                source_dir=source_dir,
                dockerfile_path=dockerfile_path,
                disk_mb=4096,
                image_path=image_path,
                image_metadata_path=image_metadata_path,
                build_dir=build_dir,
            )
            create_flow.event(state=f"[Rollout] image processed path={built_image.path}")

            resolved_payload = {
                "source": built_image.resolved_run_command.source,
                "working_dir": built_image.resolved_run_command.working_dir,
                "command": built_image.resolved_run_command.command,
                "entrypoint": built_image.resolved_run_command.entrypoint,
                "cmd": built_image.resolved_run_command.cmd,
            }
            runtime_image_payload = {
                "id": built_image.id,
                "path": str(built_image.path),
                "metadata_path": str(built_image.metadata_path),
            }

            write_text(
                rollout_path / "run.sh",
                (
                    "#!/bin/sh\n"
                    "set -eu\n"
                    f"cd {shell_quote(built_image.resolved_run_command.working_dir)}\n"
                    f"exec {built_image.resolved_run_command.command}\n"
                ),
                encoding="utf-8",
            )
            (rollout_path / "run.sh").chmod(0o755)

            rollout_json = {
                "id": rollout_id,
                "name": rollout_name,
                "runtime": "Dockerfile",
                "image_path": str(built_image.path),
                "deleteOnSuccess": deleteOnSuccess,
                "created_at": created_at,
                "dockerfile": str(dockerfile_path),
                "runtime_image": runtime_image_payload,
                "resolved_run_command": resolved_payload,
            }
            self.write_rollout_json(rollout_path / "rollout.json", rollout_json)
            rollout = Rollout.from_payload(rollout_json, rollout_path=rollout_path)

            metadata["rollouts"].append(rollout.to_metadata_entry())
            self.write_metadata(metadata)
            create_flow.event(state=f"[Rollout] created duration_ms={int((time.monotonic() - create_started_at) * 1000)}")
            create_flow.close()
            flow.close()
            return rollout
        except Exception as exc:
            if create_flow is not None:
                create_flow.exception(state=f"[Rollout] create failed error={exc}")
                create_flow.close()
            else:
                flow.exception(state=f"[Rollout] create failed name={rollout_name} error={exc}")
            flow.close()
            remove_tree(rollout_path, ignore_errors=True)
            raise

    def drop_existing_rollouts_with_same_name(self, *, metadata: dict[str, Any], rollout_name: str) -> dict[str, Any]:
        rollouts_raw = metadata.get("rollouts", [])
        if not isinstance(rollouts_raw, list):
            return metadata

        kept: list[dict[str, Any]] = []
        removed_any = False

        for entry in rollouts_raw:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") != rollout_name:
                kept.append(entry)
                continue

            removed_any = True
            try:
                self._delete_rollout_artifacts(entry)
            except OSError as exc:
                raise RolloutError("Could not replace existing rollout directory.") from exc

        if not removed_any:
            return metadata

        updated = dict(metadata)
        updated["rollouts"] = kept
        self.write_metadata(updated)
        return updated

    def find_existing_rollout_by_name(self, *, metadata: dict[str, Any], rollout_name: str) -> Rollout | None:
        rollouts_raw = metadata.get("rollouts", [])
        if not isinstance(rollouts_raw, list):
            return None

        for entry in rollouts_raw:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") != rollout_name:
                continue

            path_raw = entry.get("path")
            if not isinstance(path_raw, str) or not path_raw.strip():
                continue

            rollout_json_path = Path(path_raw) / "rollout.json"
            if not rollout_json_path.is_file():
                continue
            try:
                return self.load_rollout_json(rollout_json_path)
            except RolloutMetadataError:
                continue

        return None

    def _delete_rollout_artifacts(self, entry: dict[str, Any]) -> None:
        rollout_path_raw = entry.get("path")
        if isinstance(rollout_path_raw, str) and rollout_path_raw.strip():
            rollout_path = Path(rollout_path_raw)
            if rollout_path.exists():
                delete_flow = create_flow_logger(
                    name="sparkvm.rollouts.delete",
                    home_dir=self.home_dir,
                    context={"op": "rollout_delete"},
                )
                delete_flow.event(state=f"[Rollout] cleanup started path={rollout_path}")
                remove_tree(rollout_path, ignore_errors=False)
                delete_flow.event(state=f"[Rollout] cleanup finished path={rollout_path}")
                delete_flow.close()

        image_path_raw = entry.get("image_path")
        if isinstance(image_path_raw, str) and image_path_raw.strip():
            remove_file(Path(image_path_raw), missing_ok=True)

        runtime_image = entry.get("runtime_image")
        if isinstance(runtime_image, dict):
            metadata_path_raw = runtime_image.get("metadata_path")
            if isinstance(metadata_path_raw, str) and metadata_path_raw.strip():
                remove_file(Path(metadata_path_raw), missing_ok=True)

    def list(self) -> list[Rollout]:
        metadata = self.load_metadata()
        items: list[Rollout] = []
        for entry in metadata["rollouts"]:
            if not isinstance(entry, dict):
                continue
            path_raw = entry.get("path")
            if not isinstance(path_raw, str) or not path_raw.strip():
                continue
            rollout_path = Path(path_raw)
            rollout_json_path = rollout_path / "rollout.json"
            if not rollout_json_path.is_file():
                continue
            items.append(self.load_rollout_json(rollout_json_path))
        return items

    def get_by_id(self, rollout_id: str) -> Rollout:
        candidate_id = validate_rollout_id(rollout_id)
        metadata = self.load_metadata()
        for entry in metadata["rollouts"]:
            if not isinstance(entry, dict):
                continue
            if entry.get("id") != candidate_id:
                continue

            path_raw = entry.get("path")
            if not isinstance(path_raw, str) or not path_raw.strip():
                break
            rollout_path = Path(path_raw)
            rollout_json_path = rollout_path / "rollout.json"
            if not rollout_json_path.is_file():
                raise RolloutNotFoundError(f"rollout.json missing for id '{candidate_id}'.")
            return self.load_rollout_json(rollout_json_path)

        raise RolloutNotFoundError(f"Rollout not found: {candidate_id}")

    def delete_by_id(self, rollout_id: str) -> None:
        candidate_id = validate_rollout_id(rollout_id)
        flow = create_flow_logger(
            name="sparkvm.rollouts.delete",
            home_dir=self.home_dir,
            context={"op": "rollout_delete", "rollout_id": candidate_id},
        )
        flow.event(state=f"[Rollout] delete started rollout_id={candidate_id}")
        metadata = self.load_metadata()

        target_index = -1
        target_entry: dict[str, Any] | None = None
        for index, entry in enumerate(metadata["rollouts"]):
            if isinstance(entry, dict) and entry.get("id") == candidate_id:
                target_index = index
                target_entry = entry
                break

        if target_index < 0 or target_entry is None:
            flow.error(state=f"[Rollout] delete failed rollout_id={candidate_id} reason=not_found")
            flow.close()
            raise RolloutNotFoundError(f"Rollout not found: {candidate_id}")

        self._delete_rollout_artifacts(target_entry)
        del metadata["rollouts"][target_index]
        self.write_metadata(metadata)
        flow.event(state=f"[Rollout] delete finished rollout_id={candidate_id}")
        flow.close()

    def exists(self, rollout_id: str) -> bool:
        try:
            self.get_by_id(rollout_id)
        except RolloutError:
            return False
        return True

    def generate_rollout_id(self, *, rollout_name: str, existing_ids: set[Any]) -> str:
        slug = "-".join(part for part in rollout_name.lower().split() if part)[:32] or "rollout"
        for _ in range(64):
            candidate = f"rollout-{slug}-{secrets.token_hex(8)}"
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
        return Rollout.from_payload(payload, rollout_path=rollout_json_path.parent)

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
        write_json_atomic(path, payload, pretty=True)

    def write_metadata(self, metadata: dict[str, Any]) -> None:
        ensure_dir(self.rollouts_dir, exist_ok=True)
        write_json_atomic(self.metadata_path, metadata, pretty=True)


__all__ = [
    "Rollout",
    "Rollouts",
    "validate_rollout_id",
]
