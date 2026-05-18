"""Job models for SparkVM."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterator

from .errors import InvalidResourceError


@dataclass(frozen=True)
class VMJob:
    files: dict[str, str | bytes]
    command: str
    timeout_sec: float | None = None
    env: dict[str, str] = field(default_factory=dict)
    name: str = "job"

    def __post_init__(self) -> None:
        if not isinstance(self.files, dict):
            raise TypeError("files must be a dict[str, str | bytes].")

        for path, content in self.files.items():
            validate_job_file_path(path)
            if not isinstance(content, (str, bytes)):
                raise TypeError("Job file content must be str or bytes.")

        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("command must be a non-empty string.")

        if self.timeout_sec is not None:
            if isinstance(self.timeout_sec, bool) or not isinstance(self.timeout_sec, (int, float)):
                raise InvalidResourceError("timeout_sec must be a positive number when provided.")
            if self.timeout_sec <= 0:
                raise InvalidResourceError("timeout_sec must be greater than zero.")

        if not isinstance(self.env, dict):
            raise TypeError("env must be a dict[str, str].")
        for key, value in self.env.items():
            if not isinstance(key, str) or not key:
                raise ValueError("Environment variable keys must be non-empty strings.")
            if not isinstance(value, str):
                raise TypeError("Environment variable values must be strings.")

        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be a non-empty string.")

    @classmethod
    def python(
        cls,
        code: str,
        filename: str = "main.py",
        timeout_sec: float | None = None,
    ) -> "VMJob":
        if not isinstance(code, str):
            raise TypeError("code must be a string.")

        validate_job_file_path(filename)
        return cls(
            files={filename: code},
            command=f"python3 /job/{filename}",
            timeout_sec=timeout_sec,
        )


def validate_job_file_path(path: str) -> None:
    if not isinstance(path, str):
        raise TypeError("Job file path must be a string.")

    raw = path.strip()
    if not raw:
        raise ValueError("Job file path cannot be empty.")

    normalized = PurePosixPath(raw)
    if normalized.is_absolute():
        raise ValueError("Job file path must be relative, not absolute.")

    if ".." in normalized.parts:
        raise ValueError("Job file path cannot contain '..'.")

    if normalized.name in {"", ".", ".."}:
        raise ValueError("Job file path must include a file name.")


@dataclass
class VMJobs:
    jobs: list[VMJob] = field(default_factory=list)

    def add(self, job: VMJob) -> None:
        if not isinstance(job, VMJob):
            raise TypeError("add expects a VMJob instance.")
        self.jobs.append(job)

    def add_python(
        self,
        code: str,
        filename: str = "main.py",
        timeout_sec: float | None = None,
    ) -> VMJob:
        job = VMJob.python(code=code, filename=filename, timeout_sec=timeout_sec)
        self.jobs.append(job)
        return job

    def __iter__(self) -> Iterator[VMJob]:
        return iter(self.jobs)

    def __len__(self) -> int:
        return len(self.jobs)


__all__ = ["VMJob", "VMJobs"]
