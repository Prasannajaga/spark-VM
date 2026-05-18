"""Helpers for managed runtime image metadata under ~/.sparkvm/images."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .fsops import read_json
from .image import normalize_runtime_name


@dataclass(frozen=True)
class RuntimePaths:
    name: str
    rootfs: Path
    metadata: Path


@dataclass(frozen=True)
class RuntimeRecord:
    name: str
    source_image: str | None
    rootfs: Path
    metadata: Path
    size_mb: int | None
    created_at: str | None


def runtime_paths(images_dir: Path, runtime: str) -> RuntimePaths:
    name = normalize_runtime_name(runtime)
    return RuntimePaths(
        name=name,
        rootfs=images_dir / f"{name}.ext4",
        metadata=images_dir / f"{name}.json",
    )


def list_runtime_records(images_dir: Path) -> list[RuntimeRecord]:
    if not images_dir.exists():
        return []

    records: list[RuntimeRecord] = []
    for metadata_path in sorted(images_dir.glob("*.json")):
        try:
            payload = read_json(metadata_path, encoding="utf-8")
        except Exception:
            continue

        if not isinstance(payload, dict):
            continue

        runtime_name_raw = payload.get("runtime")
        if not isinstance(runtime_name_raw, str) or not runtime_name_raw.strip():
            continue

        runtime_name = normalize_runtime_name(runtime_name_raw)
        rootfs = images_dir / f"{runtime_name}.ext4"
        size_mb = payload.get("size_mb")
        created_at = payload.get("created_at")
        source_image = payload.get("source_image")

        records.append(
            RuntimeRecord(
                name=runtime_name,
                source_image=source_image if isinstance(source_image, str) else None,
                rootfs=rootfs,
                metadata=metadata_path,
                size_mb=size_mb if isinstance(size_mb, int) else None,
                created_at=created_at if isinstance(created_at, str) else None,
            )
        )
    records.sort(key=lambda item: item.name)
    return records


__all__ = ["RuntimePaths", "RuntimeRecord", "runtime_paths", "list_runtime_records"]
