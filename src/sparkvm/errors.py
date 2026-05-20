"""SparkVM exception hierarchy."""


class SparkVMError(Exception):
    """Base SparkVM SDK exception."""


class SparkVMConfigError(SparkVMError):
    """Invalid SparkVM configuration."""


class InvalidMemoryError(SparkVMConfigError):
    """Memory format or value is invalid."""


class InvalidResourceError(SparkVMConfigError):
    """A resource value (vcpu, timeout, etc.) is invalid."""


class SparkVMSetupError(SparkVMError):
    """SparkVM setup failed."""


class FirecrackerBinaryNotInstalled(SparkVMSetupError):
    """Firecracker binary is not installed in the managed location."""


class RuntimeImageNotFound(SparkVMSetupError):
    """Managed runtime image could not be found."""


class KernelImageNotFound(SparkVMSetupError):
    """Managed kernel image could not be found."""


class RuntimeImagePermissionError(SparkVMSetupError):
    """Managed runtime image exists but lacks required permissions."""


class WorkerRootfsError(SparkVMSetupError):
    """Per-worker runtime rootfs copy creation/permission failure."""


class BaseImageNotFound(SparkVMSetupError):
    """Managed base image could not be found."""


class KVMUnavailableError(SparkVMSetupError):
    """KVM is unavailable on the host."""


class FirecrackerProcessError(SparkVMError):
    """Firecracker process lifecycle failure."""


class FirecrackerAPIError(SparkVMError):
    """Firecracker API request/response failure."""


class FirecrackerBootError(SparkVMError):
    """Firecracker VM boot failure."""


class JobDiskError(SparkVMError):
    """Job disk build or IO failure."""


class JobTimeoutError(SparkVMError):
    """Guest job timed out."""


class GuestExecutionError(SparkVMError):
    """Guest job execution failed."""


class GuestOOMError(GuestExecutionError):
    """Guest process was OOM-killed."""


class GuestPanicError(GuestExecutionError):
    """Guest kernel/init panic detected from VM logs."""


class HostDiskPressureError(SparkVMError):
    """Host disk pressure prevented execution."""


class CleanupError(SparkVMError):
    """Cleanup failed after execution."""


class NetworkSetupError(SparkVMError):
    """Host-side VM network setup/teardown failure."""


class RolloutError(SparkVMError):
    """Rollout creation/loading/deletion failure."""


class RolloutConfigError(RolloutError):
    """Rollout configuration is invalid or incomplete."""


class RolloutBuildError(RolloutError):
    """Rollout build step failed (Docker build/export/setup)."""


class RolloutNotFoundError(RolloutError):
    """Requested rollout does not exist."""


class RolloutMetadataError(RolloutError):
    """Rollout metadata file is missing/corrupt/invalid."""


class InvalidRepoError(RolloutError):
    """Repo-mode rollout source is invalid or Git operations failed."""


class InvalidRolloutModeError(RolloutError):
    """Rollout mode is unsupported."""


class WorkerError(SparkVMError):
    """Worker persistence/listing/deletion failure."""


class WorkerNotFoundError(WorkerError):
    """Requested worker does not exist."""


class WorkerMetadataError(WorkerError):
    """Worker metadata file is missing/corrupt/invalid."""


class ExecutionDiskError(JobDiskError):
    """Execution disk build/mount/read failure."""


__all__ = [
    "SparkVMError",
    "SparkVMConfigError",
    "InvalidMemoryError",
    "InvalidResourceError",
    "SparkVMSetupError",
    "FirecrackerBinaryNotInstalled",
    "RuntimeImageNotFound",
    "KernelImageNotFound",
    "RuntimeImagePermissionError",
    "WorkerRootfsError",
    "BaseImageNotFound",
    "KVMUnavailableError",
    "FirecrackerProcessError",
    "FirecrackerAPIError",
    "FirecrackerBootError",
    "JobDiskError",
    "JobTimeoutError",
    "GuestExecutionError",
    "GuestOOMError",
    "GuestPanicError",
    "HostDiskPressureError",
    "CleanupError",
    "NetworkSetupError",
    "RolloutError",
    "RolloutConfigError",
    "RolloutBuildError",
    "RolloutNotFoundError",
    "RolloutMetadataError",
    "InvalidRepoError",
    "InvalidRolloutModeError",
    "WorkerError",
    "WorkerNotFoundError",
    "WorkerMetadataError",
    "ExecutionDiskError",
]
