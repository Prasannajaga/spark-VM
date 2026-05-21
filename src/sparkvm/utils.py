"""Shared utility helpers for SparkVM CLI and SDK."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .commands import run_checked
from .errors import CleanupError, RolloutConfigError


@dataclass(frozen=True)
class ResolvedCommand:
    source: str
    working_dir: str
    command: str
    entrypoint: list[str] | str | None
    cmd: list[str] | str | None


def path_within(base: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(base)
        return True
    except ValueError:
        return False


def unescape_mount_path(raw: str) -> str:
    return (
        raw.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def mount_points_under(base_dir: Path) -> list[Path]:
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return []

    points: list[Path] = []
    try:
        from .fsops import read_text
        lines = read_text(mountinfo, encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue
        mount_path = Path(unescape_mount_path(parts[4]))
        if mount_path == base_dir or path_within(base_dir, mount_path):
            points.append(mount_path)

    return sorted(points, key=lambda path: len(path.parts), reverse=True)


def unmount_under(base_dir: Path) -> None:
    for mount_path in mount_points_under(base_dir):
        try:
            run_checked(["umount", str(mount_path)], error_factory=CleanupError)
        except CleanupError as exc:
            raise CleanupError(
                f"Could not unmount active mount '{mount_path}' while cleaning '{base_dir}'. "
                "Resolve mounts and try again."
            ) from exc


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def now_utc_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_command_value(value: Any) -> list[str] | str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    if isinstance(value, (list, tuple)):
        parts = [str(part) for part in value if str(part).strip()]
        return parts if parts else None
    raise RolloutConfigError(f"Unsupported Docker command value type: {type(value).__name__}")


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


def resolve_container_command(
    *,
    run_cmd: str | None,
    docker_entrypoint: Any,
    docker_cmd: Any,
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


def has_cap_net_admin() -> bool:
    status_path = Path("/proc/self/status")
    if not status_path.exists():
        return False

    try:
        from .fsops import read_text
        lines = status_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False

    cap_eff_raw = None
    for line in lines:
        if line.startswith("CapEff:"):
            cap_eff_raw = line.split(":", 1)[1].strip()
            break
    if cap_eff_raw is None:
        return False

    try:
        cap_eff = int(cap_eff_raw, 16)
    except ValueError:
        return False

    cap_net_admin_bit = 12
    return bool(cap_eff & (1 << cap_net_admin_bit))


def has_network_privileges() -> bool:
    if os.geteuid() == 0:
        return True
    return has_cap_net_admin()
