from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.disk import ExecutionDisk
from sparkvm.rollouts import Rollout


class ResultParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-result-parsing-")
        self.base = Path(self.tmp.name)
        self.rollout_path = self.base / "rollout"
        self.rollout_path.mkdir(parents=True, exist_ok=True)
        self.rollout = Rollout(
            id="rollout-test-1",
            name="test",
            mode="script",
            base_image="debian-minbase",
            path=self.rollout_path,
            command="python3 /job/main.py",
            setup_cmd=None,
            run_cmd="python3 /job/main.py",
            disk_mb=1024,
            files=["main.py", "run.sh"],
            created_at="2026-01-01T00:00:00Z",
        )
        self.execution_disk = ExecutionDisk(
            rollout=self.rollout,
            path=self.base / "disk.ext4",
            size_mb=64,
            mount_base=self.base / "mnt",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _read_with_mapping(self, mapping: dict[str, str]):
        def _fake_dump(_image_path: Path, fs_path: str, output_path: Path) -> bool:
            if fs_path not in mapping:
                return False
            output_path.write_text(mapping[fs_path], encoding="utf-8")
            return True

        with patch("sparkvm.disk.debugfs_dump_file", side_effect=_fake_dump):
            return self.execution_disk.read_result(vm_id="vm-123", duration_ms=10, firecracker_log_path=None)

    def test_setup_failure_status(self) -> None:
        result = self._read_with_mapping(
            {
                "/results/final_exit_code": "2\n",
                "/results/setup.exit_code": "2\n",
                "/results/setup.stdout.log": "setup out\n",
                "/results/setup.stderr.log": "setup err\n",
            }
        )

        self.assertEqual("setup_failed", result.status)
        self.assertEqual(2, result.exit_code)
        self.assertIsNotNone(result.setup)
        self.assertEqual(2, result.setup.exit_code if result.setup else -1)

    def test_run_failure_status(self) -> None:
        result = self._read_with_mapping(
            {
                "/results/final_exit_code": "1\n",
                "/results/run.exit_code": "1\n",
                "/results/run.stdout.log": "run out\n",
                "/results/run.stderr.log": "run err\n",
            }
        )

        self.assertEqual("run_failed", result.status)
        self.assertEqual(1, result.exit_code)
        self.assertIsNotNone(result.run)
        self.assertEqual("run out\n", result.stdout)
        self.assertEqual("run err\n", result.stderr)

    def test_success_status(self) -> None:
        result = self._read_with_mapping(
            {
                "/results/final_exit_code": "0\n",
                "/results/run.exit_code": "0\n",
                "/results/run.stdout.log": "ok\n",
                "/results/run.stderr.log": "",
            }
        )

        self.assertEqual("passed", result.status)
        self.assertEqual(0, result.exit_code)
        self.assertTrue(result.passed)
        self.assertEqual("ok\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
