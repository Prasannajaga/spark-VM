"""Shared subprocess helpers and command allow-list for SparkVM."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, IO


# Single source of truth for host binaries SparkVM intentionally invokes.
ALLOWED_COMMANDS = frozenset(
    {
        "curl",
        "cp",
        "dd",
        "debugfs",
        "docker",
        "e2fsck",
        "ip",
        "iptables",
        "mkfs.ext4",
        "mount",
        "sysctl",
        "tar",
        "umount",
        "git",
    }
)


def ensure_allowed_command(cmd: list[str], *, allow_unlisted: bool = False) -> None:
    if not cmd:
        raise ValueError("Command cannot be empty.")
    if not allow_unlisted and cmd[0] not in ALLOWED_COMMANDS:
        raise ValueError(f"Command '{cmd[0]}' is not in ALLOWED_COMMANDS.")


def run_checked(
    cmd: list[str],
    *,
    error_factory: Callable[[str], Exception],
    cwd: Path | None = None,
    stdin: object | None = None,
    check: bool = True,
    allow_unlisted: bool = False,
) -> subprocess.CompletedProcess[str]:
    ensure_allowed_command(cmd, allow_unlisted=allow_unlisted)
    run_kwargs = {
        "cwd": str(cwd) if cwd is not None else None,
        "check": check,
        "capture_output": True,
        "text": True,
    }
    if stdin is not None:
        run_kwargs["stdin"] = stdin
    try:
        return subprocess.run(cmd, **run_kwargs)
    except FileNotFoundError as exc:
        raise error_factory(f"Required command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or "command failed"
        raise error_factory(f"Command failed: {' '.join(cmd)}\n{detail}") from exc


def popen_checked(
    cmd: list[str],
    *,
    error_factory: Callable[[str], Exception],
    cwd: Path | None = None,
    stdin: int | IO[bytes] | IO[str] | None = None,
    stdout: int | IO[bytes] | IO[str] | None = None,
    stderr: int | IO[bytes] | IO[str] | None = None,
    text: bool | None = None,
    allow_unlisted: bool = False,
) -> subprocess.Popen:
    ensure_allowed_command(cmd, allow_unlisted=allow_unlisted)
    try:
        return subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=text,
        )
    except FileNotFoundError as exc:
        raise error_factory(f"Required command not found: {cmd[0]}") from exc
    except OSError as exc:
        raise error_factory(f"Could not start command: {' '.join(cmd)}") from exc


__all__ = ["ALLOWED_COMMANDS", "ensure_allowed_command", "run_checked", "popen_checked"]
