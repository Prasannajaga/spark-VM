"""Runtime image resolution for SparkVM-managed assets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import SparkVMConfig
from .errors import RuntimeImageNotFound
from .runtimes.python import PYTHON_RUNTIME_ID
from .setup import paths_from_config

PYTHON_BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/init"


@dataclass(frozen=True)
class RuntimeImage:
    name: str
    kernel_image: Path
    rootfs_image: Path
    boot_args: str


def resolve_runtime_image(runtime: str, config: SparkVMConfig) -> RuntimeImage:
    selected_runtime = runtime or config.runtime
    paths = paths_from_config(config)

    if selected_runtime == PYTHON_RUNTIME_ID:
        kernel = paths.kernel_image
        rootfs = paths.python_rootfs
        if not kernel.exists() or not rootfs.exists():
            raise RuntimeImageNotFound(
                "Python runtime image not found. Run `sparkvm setup python`."
            )
        return RuntimeImage(
            name=PYTHON_RUNTIME_ID,
            kernel_image=kernel,
            rootfs_image=rootfs,
            boot_args=PYTHON_BOOT_ARGS,
        )

    raise RuntimeImageNotFound(
        f"Runtime '{selected_runtime}' is not installed. Run `sparkvm setup {selected_runtime}` if supported."
    )


class ManagedImageResolver:
    """Compatibility wrapper for staged VM orchestration."""

    def __init__(self, config: SparkVMConfig) -> None:
        self._config = config

    def resolve(self, runtime: str | None = None) -> RuntimeImage:
        return resolve_runtime_image(runtime or self._config.runtime, self._config)


__all__ = [
    "PYTHON_BOOT_ARGS",
    "RuntimeImage",
    "resolve_runtime_image",
    "ManagedImageResolver",
]
