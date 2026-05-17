from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.errors import InvalidMemoryError, InvalidResourceError, RolloutError, RolloutNotFoundError, SparkVMConfigError
from sparkvm.rollouts import RolloutItem
from sparkvm.vm import SparkVM
from sparkvm.config import build_config, parse_memory_to_mib


class ConfigNegativeTest(unittest.TestCase):
    def test_parse_memory_rejects_bool_non_numeric_and_non_positive(self) -> None:
        bad_values = [True, "bad", 0, -1, "0", "-1", object()]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(InvalidMemoryError):
                    parse_memory_to_mib(value)  # type: ignore[arg-type]

    def test_build_config_rejects_invalid_vcpu_timeout_and_runtime(self) -> None:
        with self.assertRaises(InvalidResourceError):
            build_config(vcpu=0, memory="512M", timeout=30, runtime="python-3.12", home_dir=None)

        with self.assertRaises(InvalidResourceError):
            build_config(vcpu=1, memory="512M", timeout=True, runtime="python-3.12", home_dir=None)

        with self.assertRaises(InvalidResourceError):
            build_config(vcpu=1, memory="512M", timeout=0, runtime="python-3.12", home_dir=None)

        with self.assertRaises(SparkVMConfigError):
            build_config(vcpu=1, memory="512M", timeout=30, runtime="   ", home_dir=None)


class SparkVMNegativeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdirs: list[tempfile.TemporaryDirectory[str]] = []

    def tearDown(self) -> None:
        for tmp in self._tmpdirs:
            tmp.cleanup()

    def _new_temp_path(self, *, prefix: str) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix=prefix)
        self._tmpdirs.append(tmp)
        return Path(tmp.name)

    def test_run_rejects_invalid_rollout_type(self) -> None:
        vm = SparkVM(home_dir=self._new_temp_path(prefix="sparkvm-vm-negative-"))
        with self.assertRaises(TypeError):
            vm.run(123)  # type: ignore[arg-type]

    def test_run_rollout_item_with_missing_path_raises(self) -> None:
        vm = SparkVM(home_dir=self._new_temp_path(prefix="sparkvm-vm-negative-"))
        missing_path = self._new_temp_path(prefix="sparkvm-missing-rollout-") / "missing"

        rollout = RolloutItem(
            id="rollout-item-missing-path",
            name="missing",
            runtime="python-3.12",
            path=missing_path,
            command="python3 /job/main.py",
            files=["main.py", "run.sh"],
            created_at="2026-01-01T00:00:00Z",
            updated_at=None,
        )

        with self.assertRaises(RolloutNotFoundError):
            vm.run(rollout)

    def test_run_rollout_item_with_missing_rollout_json_raises(self) -> None:
        vm = SparkVM(home_dir=self._new_temp_path(prefix="sparkvm-vm-negative-"))
        rollout_dir = self._new_temp_path(prefix="sparkvm-rollout-dir-no-json-")

        rollout = RolloutItem(
            id="rollout-item-no-json",
            name="missing-json",
            runtime="python-3.12",
            path=rollout_dir,
            command="python3 /job/main.py",
            files=["main.py", "run.sh"],
            created_at="2026-01-01T00:00:00Z",
            updated_at=None,
        )

        with self.assertRaises(RolloutNotFoundError):
            vm.run(rollout)

    def test_validate_rollout_runtime_rejects_unsupported_runtime(self) -> None:
        vm = SparkVM(home_dir=self._new_temp_path(prefix="sparkvm-vm-negative-"))
        rollout_dir = self._new_temp_path(prefix="sparkvm-rollout-runtime-")
        (rollout_dir / "rollout.json").write_text("{}", encoding="utf-8")

        rollout = RolloutItem(
            id="rollout-bad-runtime",
            name="bad-runtime",
            runtime="node-20",
            path=rollout_dir,
            command="node /job/main.js",
            files=["main.js", "run.sh"],
            created_at="2026-01-01T00:00:00Z",
            updated_at=None,
        )

        with self.assertRaises(RolloutError):
            vm.run(rollout)

    def test_validate_rollout_runtime_rejects_runtime_mismatch(self) -> None:
        vm = SparkVM(runtime="python-3.11", home_dir=self._new_temp_path(prefix="sparkvm-vm-negative-"))
        rollout_dir = self._new_temp_path(prefix="sparkvm-rollout-runtime-mismatch-")
        (rollout_dir / "rollout.json").write_text("{}", encoding="utf-8")

        rollout = RolloutItem(
            id="rollout-runtime-mismatch",
            name="runtime-mismatch",
            runtime="python-3.12",
            path=rollout_dir,
            command="python3 /job/main.py",
            files=["main.py", "run.sh"],
            created_at="2026-01-01T00:00:00Z",
            updated_at=None,
        )

        with self.assertRaises(RolloutError):
            vm.run(rollout)


if __name__ == "__main__":
    unittest.main()
