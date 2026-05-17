import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm import SparkVMError
from sparkvm.errors import (
    CleanupError,
    ExecutionDiskError,
    FirecrackerAPIError,
    FirecrackerBinaryNotInstalled,
    FirecrackerBootError,
    FirecrackerProcessError,
    GuestExecutionError,
    GuestOOMError,
    HostDiskPressureError,
    InvalidMemoryError,
    InvalidResourceError,
    JobDiskError,
    JobTimeoutError,
    KVMUnavailableError,
    RolloutError,
    RolloutMetadataError,
    RolloutNotFoundError,
    SparkVMConfigError,
)


class ErrorInheritanceTest(unittest.TestCase):
    def test_config_hierarchy(self) -> None:
        self.assertTrue(issubclass(SparkVMConfigError, SparkVMError))
        self.assertTrue(issubclass(InvalidMemoryError, SparkVMConfigError))
        self.assertTrue(issubclass(InvalidResourceError, SparkVMConfigError))

    def test_runtime_hierarchy(self) -> None:
        self.assertTrue(issubclass(FirecrackerBinaryNotInstalled, SparkVMError))
        self.assertTrue(issubclass(KVMUnavailableError, SparkVMError))
        self.assertTrue(issubclass(FirecrackerProcessError, SparkVMError))
        self.assertTrue(issubclass(FirecrackerAPIError, SparkVMError))
        self.assertTrue(issubclass(FirecrackerBootError, SparkVMError))
        self.assertTrue(issubclass(JobDiskError, SparkVMError))
        self.assertTrue(issubclass(ExecutionDiskError, JobDiskError))
        self.assertTrue(issubclass(JobTimeoutError, SparkVMError))
        self.assertTrue(issubclass(GuestExecutionError, SparkVMError))
        self.assertTrue(issubclass(GuestOOMError, GuestExecutionError))
        self.assertTrue(issubclass(HostDiskPressureError, SparkVMError))
        self.assertTrue(issubclass(CleanupError, SparkVMError))

    def test_rollout_hierarchy(self) -> None:
        self.assertTrue(issubclass(RolloutError, SparkVMError))
        self.assertTrue(issubclass(RolloutNotFoundError, RolloutError))
        self.assertTrue(issubclass(RolloutMetadataError, RolloutError))


if __name__ == "__main__":
    unittest.main()
