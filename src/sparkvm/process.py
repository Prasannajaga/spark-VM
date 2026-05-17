"""Firecracker process handling primitives."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from .errors import FirecrackerProcessError


@dataclass
class FirecrackerProcess:
    firecracker_bin: Path
    socket_path: Path
    log_path: Path | None = None
    _proc: subprocess.Popen[str] | None = field(default=None, init=False, repr=False)
    _log_handle: IO[str] | None = field(default=None, init=False, repr=False)

    def start(self, startup_timeout_sec: float = 5.0) -> None:
        if self._proc is not None and self._proc.poll() is None:
            raise FirecrackerProcessError("Firecracker process is already running.")
        if startup_timeout_sec <= 0:
            raise FirecrackerProcessError("startup_timeout_sec must be > 0.")

        if not self.firecracker_bin.exists():
            raise FirecrackerProcessError(f"Firecracker binary does not exist: {self.firecracker_bin}")

        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError as exc:
                raise FirecrackerProcessError(f"Could not remove stale socket: {self.socket_path}") from exc
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        log_path = self.log_path if self.log_path is not None else (self.socket_path.parent / "firecracker.log")
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = log_path.open("a", encoding="utf-8")

        try:
            self._proc = subprocess.Popen(
                [str(self.firecracker_bin), "--api-sock", str(self.socket_path)],
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle,
                stderr=self._log_handle,
                text=True,
            )
        except OSError as exc:
            self._close_log_handle()
            raise FirecrackerProcessError(
                f"Could not start Firecracker process with binary {self.firecracker_bin}"
            ) from exc

        deadline = time.monotonic() + startup_timeout_sec
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                exit_code = self._proc.returncode
                self._proc = None
                detail = (
                    f"Firecracker exited before API socket became ready (exit code {exit_code}). "
                    f"Check Firecracker log: {self.log_path}"
                )
                tail = self._read_log_tail()
                if tail:
                    detail += f"\nFirecracker log tail:\n{tail}"
                self._close_log_handle()
                raise FirecrackerProcessError(detail)

            if self.socket_path.exists():
                return

            time.sleep(0.05)

        self.stop()
        raise FirecrackerProcessError(
            "Timed out waiting for Firecracker API socket to become ready. "
            f"Socket path: {self.socket_path}. Check Firecracker log: {self.log_path}"
        )

    def wait(self, timeout_sec: float | None = None) -> int:
        if self._proc is None:
            raise FirecrackerProcessError("Firecracker process is not running.")
        return self._proc.wait(timeout=timeout_sec)

    def stop(self) -> None:
        if self._proc is None:
            self._close_log_handle()
            return

        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=3)

        self._proc = None
        self._close_log_handle()

    def poll(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.poll()

    def _close_log_handle(self) -> None:
        if self._log_handle is None:
            return
        self._log_handle.close()
        self._log_handle = None

    def _read_log_tail(self, max_lines: int = 40) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-max_lines:])


__all__ = ["FirecrackerProcess"]
