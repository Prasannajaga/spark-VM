"""Unified filesystem operations for SparkVM."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import IO
from typing import Any


def ensure_dir(path: Path, *, exist_ok: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=exist_ok)


def read_text(path: Path, *, encoding: str = "utf-8", errors: str | None = None) -> str:
    if errors is None:
        return path.read_text(encoding=encoding)
    return path.read_text(encoding=encoding, errors=errors)


def write_text(path: Path, data: str, *, encoding: str = "utf-8") -> None:
    parent = path.parent
    ensure_dir(parent, exist_ok=True)
    path.write_text(data, encoding=encoding)


def open_text_append(path: Path, *, encoding: str = "utf-8") -> IO[str]:
    parent = path.parent
    ensure_dir(parent, exist_ok=True)
    return path.open("a", encoding=encoding)


def write_bytes(path: Path, data: bytes) -> None:
    parent = path.parent
    ensure_dir(parent, exist_ok=True)
    path.write_bytes(data)


def write_text_atomic(
    path: Path,
    data: str,
    *,
    encoding: str = "utf-8",
    sync_file: bool = False,
    sync_dir: bool = False,
) -> None:
    parent = path.parent
    ensure_dir(parent, exist_ok=True)
    tmp_path = parent / f".{path.name}.tmp"
    try:
        with tmp_path.open("w", encoding=encoding) as handle:
            handle.write(data)
            if sync_file:
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if sync_dir:
            try:
                dir_fd = os.open(parent, os.O_DIRECTORY)
            except OSError:
                return
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def read_json(path: Path, *, encoding: str = "utf-8") -> Any:
    return json.loads(read_text(path, encoding=encoding))


def write_json_atomic(
    path: Path,
    payload: Any,
    *,
    encoding: str = "utf-8",
    pretty: bool = True,
    sync_file: bool = False,
    sync_dir: bool = False,
) -> None:
    if pretty:
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    else:
        text = json.dumps(payload)
    write_text_atomic(path, text, encoding=encoding, sync_file=sync_file, sync_dir=sync_dir)


def remove_file(path: Path, *, missing_ok: bool = True) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        if not missing_ok:
            raise


def remove_tree(path: Path, *, ignore_errors: bool = False) -> None:
    shutil.rmtree(path, ignore_errors=ignore_errors)


def list_dirs_with_prefix(base: Path, prefix: str) -> list[Path]:
    if not base.exists():
        return []
    dirs: list[Path] = []
    with os.scandir(base) as entries:
        for entry in entries:
            if not entry.is_dir():
                continue
            if not entry.name.startswith(prefix):
                continue
            dirs.append(Path(entry.path))
    dirs.sort()
    return dirs
