"""Shared subprocess helpers and command allow-list for SparkVM."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, IO



# Single source of truth for host binaries SparkVM intentionally invokes.
from .constants import ALLOWED_COMMANDS


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
    import tempfile
    ensure_allowed_command(cmd, allow_unlisted=allow_unlisted)
    
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as out_f, \
         tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as err_f:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd) if cwd is not None else None,
                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
            )
            
            stdin_bytes = None
            if isinstance(stdin, str):
                stdin_bytes = stdin.encode("utf-8")
            elif isinstance(stdin, bytes):
                stdin_bytes = stdin
                
            proc.communicate(input=stdin_bytes)
            
        except FileNotFoundError as exc:
            raise error_factory(f"Required command not found: {cmd[0]}") from exc

        def _read_tail(f) -> str:
            f.seek(0, 2)
            size = f.tell()
            max_read = 2 * 1024 * 1024
            f.seek(max(0, size - max_read))
            return f.read().decode("utf-8", errors="replace")

        stdout_str = _read_tail(out_f)
        stderr_str = _read_tail(err_f)

        if check and proc.returncode != 0:
            detail = stderr_str.strip() or stdout_str.strip() or "command failed"
            raise error_factory(f"Command failed: {' '.join(cmd)}\n{detail}")
            
        return subprocess.CompletedProcess(
            args=cmd, returncode=proc.returncode, stdout=stdout_str, stderr=stderr_str
        )


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
