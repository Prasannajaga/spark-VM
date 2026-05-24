"""Shared SparkVM logging utilities.

Provides:
- one-time logger configuration for console logs
- scoped flow logger helper with state-style events
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _prefix_for_logger(logger_name: str) -> str:
    if logger_name.startswith("sparkvm.vm"):
        return "[Firecracker vm]"
    if logger_name.startswith("sparkvm.rollouts"):
        return "[Rollout]"
    if logger_name.startswith("sparkvm.image_builder"):
        return "[Docker]"
    if logger_name.startswith("sparkvm"):
        return "[SparkVM]"
    return ""


class SparkVMLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if message.startswith("["):
            return message
        prefix = _prefix_for_logger(record.name)
        if prefix:
            return f"{prefix} {message}"
        return message


def _normalize_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str) and level.strip():
        value = level.strip().upper()
        if value in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
            return int(getattr(logging, value))
    env_level = os.getenv("SPARKVM_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    return int(getattr(logging, env_level, logging.INFO))


def _verbose_enabled() -> bool:
    return os.getenv("SPARKVM_LOG_VERBOSE", "0").strip() in {"1", "true", "True"}


def configure_logging(
    *,
    home_dir: Path | None = None,
    level: str | int | None = None,
    console: bool | None = None,
) -> None:
    """Configure the shared `sparkvm` logger namespace once.

    - Console logging defaults to enabled (SPARKVM_LOG_CONSOLE != 0)
    - Console logging only (no file logs)
    """

    base_logger = logging.getLogger("sparkvm")
    base_logger.setLevel(_normalize_level(level))

    # Ensure SparkVM never writes file logs such as sparkvm.log.
    for handler in list(base_logger.handlers):
        if isinstance(handler, logging.FileHandler):
            try:
                handler.flush()
                handler.close()
            finally:
                base_logger.removeHandler(handler)

    if console is None:
        console = os.getenv("SPARKVM_LOG_CONSOLE", "1").strip() not in {"0", "false", "False"}

    if console:
        has_console = any(getattr(h, "_sparkvm_console", False) for h in base_logger.handlers)
        if not has_console:
            stream_handler = logging.StreamHandler(sys.stderr)
            stream_handler._sparkvm_console = True  # type: ignore[attr-defined]
            stream_handler.setLevel(_normalize_level(level))
            stream_handler.setFormatter(SparkVMLogFormatter())
            base_logger.addHandler(stream_handler)

    # Remove old sparkvm.log artifacts if present.
    paths_to_remove: list[Path] = [Path.cwd() / "sparkvm.log"]
    if home_dir is not None:
        paths_to_remove.append(home_dir / "sparkvm.log")
    for path in paths_to_remove:
        try:
            if path.exists() and path.is_file():
                path.unlink()
        except OSError:
            pass


def _fmt_fields(fields: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    return " ".join(parts)


@dataclass
class FlowLogger:
    logger: logging.Logger
    context: dict[str, Any] = field(default_factory=dict)
    _handler: logging.Handler | None = None

    def _payload(self, fields: dict[str, Any]) -> dict[str, Any]:
        if not _verbose_enabled():
            return {}
        payload: dict[str, Any] = {}
        payload.update(self.context)
        payload.update(fields)
        return payload

    def _message(self, *, state: str, fields: dict[str, Any]) -> str:
        payload = self._payload(fields)
        state_text = state.strip()
        if not state_text.startswith("["):
            state_text = f"[{state_text}]"
        message = state_text
        if payload:
            message += " " + _fmt_fields(payload)
        return message

    def event(self, *, state: str, **fields: Any) -> None:
        self.logger.info(self._message(state=state, fields=fields))

    def warning(self, *, state: str, **fields: Any) -> None:
        self.logger.warning(self._message(state=state, fields=fields))

    def error(self, *, state: str, **fields: Any) -> None:
        self.logger.error(self._message(state=state, fields=fields))

    def exception(self, *, state: str, **fields: Any) -> None:
        self.logger.exception(self._message(state=state, fields=fields))

    def close(self) -> None:
        if self._handler is None:
            return
        try:
            self._handler.flush()
            self._handler.close()
        finally:
            self.logger.removeHandler(self._handler)
            self._handler = None


def create_flow_logger(
    *,
    name: str,
    home_dir: Path | None,
    context: dict[str, Any] | None = None,
    file_path: Path | None = None,
) -> FlowLogger:
    configure_logging(home_dir=home_dir)
    logger = logging.getLogger(name)
    logger.setLevel(_normalize_level(None))
    logger.propagate = True

    del file_path
    return FlowLogger(logger=logger, context=context or {}, _handler=None)


__all__ = ["configure_logging", "FlowLogger", "create_flow_logger"]
