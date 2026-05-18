"""SparkVM configuration and defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import BaseImageNotFound, InvalidMemoryError, InvalidResourceError, SparkVMConfigError

DEFAULT_VCPU = 1
DEFAULT_MEMORY = "512M"
DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_BASE_IMAGE = "debian-minbase"
# Backward compatibility alias.
DEFAULT_RUNTIME = DEFAULT_BASE_IMAGE
DEFAULT_HOME_DIR = Path.home() / ".sparkvm"

_MEMORY_RE = re.compile(r"^(?P<amount>\d+)\s*(?P<unit>m|mb|mib|g|gb|gib)?$", re.IGNORECASE)


@dataclass(frozen=True)
class SparkVMConfig:
    vcpu: int
    memory_mib: int
    timeout_sec: float
    base_image: str
    home_dir: Path
    workers_dir: Path
    bin_dir: Path
    image_dir: Path
    cache_dir: Path


def resolve_home_dir(home_dir: str | Path | None = None) -> Path:
    if home_dir is not None:
        return Path(home_dir).expanduser()

    env_home = os.getenv("SPARKVM_HOME")
    if env_home and env_home.strip():
        return Path(env_home).expanduser()

    return DEFAULT_HOME_DIR


def parse_memory_to_mib(memory: int | str) -> int:
    """Parse memory values into MiB.

    Supported inputs include:
    - 256
    - "256M" / "256MiB"
    - "1G" / "1GiB"
    """
    if isinstance(memory, bool):
        raise InvalidMemoryError("Memory must be an int or memory string, not bool.")

    if isinstance(memory, int):
        if memory <= 0:
            raise InvalidMemoryError("Memory value must be greater than zero.")
        return memory

    if not isinstance(memory, str):
        raise InvalidMemoryError("Memory must be provided as int or str.")

    match = _MEMORY_RE.fullmatch(memory.strip())
    if not match:
        raise InvalidMemoryError(
            "Unsupported memory format. Use values like 256, '256M', '256MiB', '1G', or '1GiB'."
        )

    amount = int(match.group("amount"))
    if amount <= 0:
        raise InvalidMemoryError("Memory value must be greater than zero.")

    unit = (match.group("unit") or "m").lower()
    if unit in {"m", "mb", "mib"}:
        return amount
    if unit in {"g", "gb", "gib"}:
        return amount * 1024

    raise InvalidMemoryError(f"Unsupported memory unit: {unit}")


def _validate_base_image(base_image: str) -> str:
    if not isinstance(base_image, str) or not base_image.strip():
        raise SparkVMConfigError("base_image must be a non-empty string.")
    selected = base_image.strip()
    if selected != DEFAULT_BASE_IMAGE:
        raise BaseImageNotFound(f"Unsupported base image '{selected}'. Only '{DEFAULT_BASE_IMAGE}' is supported.")
    return selected


def build_config(
    *,
    vcpu: int,
    memory: int | str,
    timeout: float,
    base_image: str | None = None,
    runtime: str | None = None,
    home_dir: str | Path | None,
) -> SparkVMConfig:
    if type(vcpu) is not int or vcpu <= 0:
        raise InvalidResourceError("vcpu must be a positive integer.")

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise InvalidResourceError("timeout must be a positive number of seconds.")

    selected_base_image = base_image if base_image is not None else runtime
    if selected_base_image is None:
        selected_base_image = DEFAULT_BASE_IMAGE

    resolved_home = resolve_home_dir(home_dir)

    return SparkVMConfig(
        vcpu=vcpu,
        memory_mib=parse_memory_to_mib(memory),
        timeout_sec=float(timeout),
        base_image=_validate_base_image(selected_base_image),
        home_dir=resolved_home,
        workers_dir=resolved_home / "workers",
        bin_dir=resolved_home / "bin",
        image_dir=resolved_home / "images",
        cache_dir=resolved_home / "cache",
    )


__all__ = [
    "SparkVMConfig",
    "DEFAULT_VCPU",
    "DEFAULT_MEMORY",
    "DEFAULT_TIMEOUT_SEC",
    "DEFAULT_BASE_IMAGE",
    "DEFAULT_RUNTIME",
    "DEFAULT_HOME_DIR",
    "resolve_home_dir",
    "parse_memory_to_mib",
    "build_config",
]
