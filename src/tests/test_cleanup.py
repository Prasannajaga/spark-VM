from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.cleanup import cleanup_all, cleanup_rollouts, cleanup_workers
from sparkvm.cli import main as cli_main
from sparkvm.config import DEFAULT_BASE_IMAGE, DEFAULT_MEMORY, DEFAULT_TIMEOUT_SEC, DEFAULT_VCPU, build_config


class CleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-cleanup-test-")
        self.home = Path(self.tmp.name)
        self.config = build_config(
            vcpu=DEFAULT_VCPU,
            memory=DEFAULT_MEMORY,
            timeout=DEFAULT_TIMEOUT_SEC,
            base_image=DEFAULT_BASE_IMAGE,
            home_dir=self.home,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_rollout_state(self) -> None:
        rollouts_dir = self.home / "rollouts"
        rollout_dir = rollouts_dir / "rollout-example-1"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        (rollout_dir / "main.py").write_text("print('x')\n", encoding="utf-8")
        (rollouts_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "rollouts": [
                        {
                            "id": "rollout-example-1",
                            "name": "example",
                            "base_image": "debian-minbase",
                            "path": str(rollout_dir),
                            "command": "python3 /job/main.py",
                            "run_cmd": "python3 /job/main.py",
                            "files": ["main.py", "run.sh"],
                            "created_at": "2026-01-01T00:00:00Z",
                            "updated_at": None,
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_workers_state(self) -> None:
        workers_dir = self.home / "workers"
        vm_dir = workers_dir / "vm-abc123"
        vm_dir.mkdir(parents=True, exist_ok=True)
        (vm_dir / "rollout.ext4").write_bytes(b"fake-ext4")
        (vm_dir / "firecracker.log").write_text("log\n", encoding="utf-8")
        (vm_dir / "firecracker.sock").write_text("", encoding="utf-8")

    def test_cleanup_rollouts_resets_metadata_json(self) -> None:
        self._write_rollout_state()
        cleanup_rollouts(self.config, force=True, dry_run=False)

        rollouts_dir = self.home / "rollouts"
        self.assertFalse((rollouts_dir / "rollout-example-1").exists())
        metadata = json.loads((rollouts_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual({"version": 1, "rollouts": []}, metadata)

    def test_cleanup_workers_removes_vm_directories(self) -> None:
        self._write_workers_state()
        cleanup_workers(self.config, force=True, dry_run=False)
        self.assertFalse((self.home / "workers" / "vm-abc123").exists())

    def test_cleanup_all_removes_rollouts_and_work_but_preserves_assets(self) -> None:
        self._write_rollout_state()
        self._write_workers_state()

        bin_dir = self.home / "bin"
        image_dir = self.home / "images"
        cache_dir = self.home / "cache"
        bin_dir.mkdir(parents=True, exist_ok=True)
        image_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "firecracker").write_text("binary\n", encoding="utf-8")
        (image_dir / "vmlinux").write_text("kernel\n", encoding="utf-8")
        (image_dir / "debian-rootfs.ext4").write_text("rootfs\n", encoding="utf-8")

        cleanup_all(self.config, force=True, dry_run=False)

        self.assertTrue(bin_dir.exists())
        self.assertTrue(image_dir.exists())
        self.assertTrue(cache_dir.exists())
        self.assertTrue((bin_dir / "firecracker").exists())
        self.assertTrue((image_dir / "vmlinux").exists())
        self.assertTrue((image_dir / "debian-rootfs.ext4").exists())
        self.assertFalse((self.home / "workers" / "vm-abc123").exists())
        self.assertFalse((self.home / "rollouts" / "rollout-example-1").exists())

    def test_cleanup_without_force_declined_prompt_deletes_nothing(self) -> None:
        self._write_rollout_state()
        rollout_dir = self.home / "rollouts" / "rollout-example-1"

        with patch("builtins.input", return_value="n"):
            code = cli_main(["--home-dir", str(self.home), "cleanup", "rollouts"])

        self.assertEqual(0, code)
        self.assertTrue(rollout_dir.exists())

    def test_cleanup_without_force_accepted_prompt_deletes_target(self) -> None:
        self._write_rollout_state()
        rollout_dir = self.home / "rollouts" / "rollout-example-1"

        with patch("builtins.input", return_value="yes"):
            code = cli_main(["--home-dir", str(self.home), "cleanup", "rollouts"])

        self.assertEqual(0, code)
        self.assertFalse(rollout_dir.exists())
        metadata = json.loads((self.home / "rollouts" / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual({"version": 1, "rollouts": []}, metadata)

    def test_cleanup_workers_force(self) -> None:
        self._write_workers_state()
        worker_dir = self.home / "workers" / "vm-abc123"

        code = cli_main(["--home-dir", str(self.home), "cleanup", "workers", "--force"])
        self.assertEqual(0, code)
        self.assertFalse(worker_dir.exists())

    def test_reset_force_deletes_all_home_contents(self) -> None:
        self._write_rollout_state()
        self._write_workers_state()
        (self.home / "images").mkdir(parents=True, exist_ok=True)
        (self.home / "images" / "vmlinux").write_text("kernel\n", encoding="utf-8")
        (self.home / "images" / "debian-rootfs.ext4").write_text("rootfs\n", encoding="utf-8")
        (self.home / "bin").mkdir(parents=True, exist_ok=True)
        (self.home / "bin" / "firecracker").write_text("fc\n", encoding="utf-8")
        (self.home / "cache").mkdir(parents=True, exist_ok=True)
        (self.home / "cache" / "tmp.txt").write_text("tmp\n", encoding="utf-8")

        code = cli_main(["--home-dir", str(self.home), "reset", "--force"])
        self.assertEqual(0, code)
        self.assertTrue(self.home.exists())
        self.assertEqual([], list(self.home.iterdir()))

    def test_reset_without_force_declined_prompt_deletes_nothing(self) -> None:
        self._write_rollout_state()
        rollout_dir = self.home / "rollouts" / "rollout-example-1"

        with patch("builtins.input", return_value="n"):
            code = cli_main(["--home-dir", str(self.home), "reset"])

        self.assertEqual(0, code)
        self.assertTrue(rollout_dir.exists())

    def test_reset_without_force_accepted_prompt_deletes_contents(self) -> None:
        self._write_rollout_state()
        with patch("builtins.input", return_value="yes"):
            code = cli_main(["--home-dir", str(self.home), "reset"])

        self.assertEqual(0, code)
        self.assertTrue(self.home.exists())
        self.assertEqual([], list(self.home.iterdir()))


if __name__ == "__main__":
    unittest.main()
