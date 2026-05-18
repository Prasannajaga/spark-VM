"""Base image resolution for SparkVM-managed assets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_BASE_IMAGE, SparkVMConfig
from .errors import BaseImageNotFound
from .runtimes.python import DEBIAN_MINBASE_IMAGE_ID
from cli.setup import paths_from_config

DEBIAN_BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/init"


@dataclass(frozen=True)
class BaseImage:
    name: str
    kernel_image: Path
    rootfs_image: Path
    boot_args: str


def resolve_base_image(base_image: str, config: SparkVMConfig) -> BaseImage:
    selected = (base_image or config.base_image).strip()
    paths = paths_from_config(config)

    if selected != DEBIAN_MINBASE_IMAGE_ID:
        raise BaseImageNotFound(f"Unsupported base image '{selected}'. Only '{DEFAULT_BASE_IMAGE}' is supported.")

    kernel = paths.kernel_image
    rootfs = paths.debian_rootfs
    if not kernel.exists() or not rootfs.exists():
        raise BaseImageNotFound("Debian base image not found. Run `sparkvm setup`.")

    return BaseImage(
        name=DEBIAN_MINBASE_IMAGE_ID,
        kernel_image=kernel,
        rootfs_image=rootfs,
        boot_args=DEBIAN_BOOT_ARGS,
    )


class ManagedImageResolver:
    """Compatibility wrapper for staged VM orchestration."""

    def __init__(self, config: SparkVMConfig) -> None:
        self._config = config

    def resolve(self, base_image: str | None = None) -> BaseImage:
        return resolve_base_image(base_image or self._config.base_image, self._config)


# Backward compatibility aliases.
RuntimeImage = BaseImage


def resolve_runtime_image(runtime: str, config: SparkVMConfig) -> BaseImage:
    return resolve_base_image(runtime, config)


__all__ = [
    "DEBIAN_BOOT_ARGS",
    "BaseImage",
    "RuntimeImage",
    "resolve_base_image",
    "resolve_runtime_image",
    "ManagedImageResolver",
]
