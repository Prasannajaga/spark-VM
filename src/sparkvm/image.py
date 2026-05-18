"""Runtime image resolution for SparkVM-managed assets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import SparkVMConfig
from .errors import KernelImageNotFound, RuntimeImageNotFound

BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/init"


def normalize_runtime_name(image_name: str) -> str:
    if not isinstance(image_name, str) or not image_name.strip():
        raise ValueError("runtime image name must be a non-empty string")

    value = image_name.strip()
    value = re.sub(r"[/:@\s]+", "-", value)
    value = re.sub(r"-+", "-", value)
    value = value.strip("-")
    if not value:
        raise ValueError("runtime image name is empty after normalization")
    return value


def _suggest_docker_image(raw_runtime: str, normalized_runtime: str) -> str:
    candidate = raw_runtime.strip()
    if any(char in candidate for char in "/:@"):
        return candidate
    if "-" in normalized_runtime:
        name, tag = normalized_runtime.split("-", 1)
        if name and tag:
            return f"{name}:{tag}"
    return normalized_runtime


@dataclass(frozen=True)
class RuntimeImage:
    name: str
    kernel_image: Path
    rootfs_image: Path
    boot_args: str = BOOT_ARGS
    metadata_path: Path | None = None

    @property
    def base_image(self) -> str:
        """Backward compatibility alias for older callers."""
        return self.name


def resolve_runtime_image(runtime: str, config: SparkVMConfig) -> RuntimeImage:
    raw_runtime = runtime or config.runtime
    normalized_runtime = normalize_runtime_name(raw_runtime)

    kernel = config.image_dir / "vmlinux"
    rootfs = config.image_dir / f"{normalized_runtime}.ext4"
    metadata = config.image_dir / f"{normalized_runtime}.json"

    if not kernel.exists():
        raise KernelImageNotFound("Kernel image not found. Run `sparkvm setup`.")

    if not rootfs.exists():
        suggestion = _suggest_docker_image(raw_runtime, normalized_runtime)
        raise RuntimeImageNotFound(
            f"Runtime image '{normalized_runtime}' not found. Run `sparkvm dockify {suggestion}`."
        )

    return RuntimeImage(
        name=normalized_runtime,
        kernel_image=kernel,
        rootfs_image=rootfs,
        metadata_path=metadata,
        boot_args=BOOT_ARGS,
    )


class ManagedImageResolver:
    """Compatibility wrapper for staged VM orchestration."""

    def __init__(self, config: SparkVMConfig) -> None:
        self._config = config

    def resolve(self, runtime: str | None = None) -> RuntimeImage:
        return resolve_runtime_image(runtime or self._config.runtime, self._config)


# Backward compatibility aliases.
BaseImage = RuntimeImage
resolve_base_image = resolve_runtime_image
DEBIAN_BOOT_ARGS = BOOT_ARGS


__all__ = [
    "BOOT_ARGS",
    "DEBIAN_BOOT_ARGS",
    "RuntimeImage",
    "BaseImage",
    "normalize_runtime_name",
    "resolve_runtime_image",
    "resolve_base_image",
    "ManagedImageResolver",
]
