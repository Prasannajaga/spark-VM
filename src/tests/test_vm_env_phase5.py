from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import MethodType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.disk import ExecutionDisk
from sparkvm.errors import FirecrackerBinaryNotInstalled
from sparkvm.image import BaseImage
from sparkvm.rollouts import Rollout
from sparkvm.vm import SparkVM, render_env_file, shell_quote


class EnvValidationAndRenderingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-env-phase5-")
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_valid_env_accepted_invalid_env_rejected(self) -> None:
        SparkVM(home_dir=self.home, env={"OPENAI_API_KEY": "abc", "A1_B2": "ok"})

        with self.assertRaises(ValueError):
            SparkVM(home_dir=self.home, env={"1BAD": "x"})
        with self.assertRaises(ValueError):
            SparkVM(home_dir=self.home, env={"": "x"})
        with self.assertRaises(TypeError):
            SparkVM(home_dir=self.home, env={"GOOD": 1})  # type: ignore[arg-type]

    def test_shell_quote_and_render_env_file(self) -> None:
        self.assertEqual("'a b'", shell_quote("a b"))
        rendered = render_env_file({"A": "a b", "B": "x'\"y"})
        self.assertIn("export A='a b'", rendered)
        self.assertIn("export B='x'\"'\"'\"y'", rendered)

    def test_env_not_in_config_repr(self) -> None:
        vm = SparkVM(home_dir=self.home, env={"SECRET_KEY": "value"})
        rendered = repr(vm.config)
        self.assertNotIn("SECRET_KEY", rendered)
        self.assertNotIn("value", rendered)


class RuntimeFileAndFailureScrubTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-env-phase5-run-")
        self.home = Path(self.tmp.name)
        self.rollout_dir = self.home / "rollout"
        self.rollout_dir.mkdir(parents=True, exist_ok=True)
        (self.rollout_dir / "rollout.json").write_text("{}\n", encoding="utf-8")
        self.rollout = Rollout(
            id="rollout-env-phase5",
            name="env-phase5",
            mode="script",
            runtime="python-3.12-slim",
            path=self.rollout_dir,
            command="python3 /job/main.py",
            setup_cmd=None,
            run_cmd="python3 /job/main.py",
            disk_mb=1024,
            files=["run.sh", "rollout.json"],
            created_at="2026-01-01T00:00:00Z",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_execution_disk_runtime_file_written_only_to_staging(self) -> None:
        disk = ExecutionDisk(
            rollout=self.rollout,
            path=self.home / "rollout.ext4",
            size_mb=1024,
            mount_base=self.home / "mnt",
        )

        captured: dict[str, Path] = {}

        def _fake_create_ext4_image(path: Path, size_mib: int, *, source_dir: Path | None = None):
            del size_mib
            self.assertIsNotNone(source_dir)
            staged = source_dir
            captured["staged"] = staged
            self.assertTrue((staged / ".sparkvm" / "env.sh").exists())
            self.assertFalse((self.rollout_dir / ".sparkvm" / "env.sh").exists())
            return path

        with patch("sparkvm.disk.create_ext4_image", side_effect=_fake_create_ext4_image):
            disk.copy_rollout(runtime_files={".sparkvm/env.sh": "export X='1'\n"})

        self.assertIn("staged", captured)

    def test_failure_json_contains_only_env_keys_and_scrubs_or_drops_disk(self) -> None:
        vm = SparkVM(home_dir=self.home, env={"OPENAI_API_KEY": "super-secret-token"})
        vm._setup.ensure_layout()

        vm._setup.firecracker_binary_path = MethodType(lambda _self: Path("/fake/firecracker"), vm._setup)
        vm._setup.assert_kvm_available = MethodType(lambda _self: None, vm._setup)
        vm._images.resolve = MethodType(lambda _self, runtime=None: (_ for _ in ()).throw(FirecrackerBinaryNotInstalled("missing")), vm._images)

        with self.assertRaises(FirecrackerBinaryNotInstalled):
            vm.run(self.rollout)

        worker = next((self.home / "workers").glob("vm-*"))
        failure_path = worker / "failure.json"
        payload = json.loads(failure_path.read_text(encoding="utf-8"))

        self.assertEqual(["OPENAI_API_KEY"], payload["env_keys"])
        self.assertFalse(payload["env_values_stored"])
        text = failure_path.read_text(encoding="utf-8")
        self.assertNotIn("super-secret-token", text)

    def test_scrub_failure_removes_execution_disk_on_infrastructure_failure(self) -> None:
        vm = SparkVM(home_dir=self.home, env={"OPENAI_API_KEY": "top-secret"})
        vm._setup.ensure_layout()
        vm._setup.firecracker_binary_path = MethodType(lambda _self: Path("/fake/firecracker"), vm._setup)
        vm._setup.assert_kvm_available = MethodType(lambda _self: None, vm._setup)
        vm._images.resolve = MethodType(
            lambda _self, runtime=None: BaseImage(
                name="python-3.12-slim",
                kernel_image=Path("/fake/vmlinux"),
                rootfs_image=Path("/fake/rootfs.ext4"),
                boot_args="console=ttyS0",
            ),
            vm._images,
        )

        def _fake_copy_rollout(self_obj, runtime_files=None):  # noqa: ANN001
            del runtime_files
            self_obj.path.parent.mkdir(parents=True, exist_ok=True)
            self_obj.path.write_bytes(b"fake-ext4")

        def _fake_start(_self, startup_timeout_sec=5.0):  # noqa: ANN001
            del startup_timeout_sec
            raise RuntimeError("process start failed")

        with patch("sparkvm.vm.ExecutionDisk.copy_rollout", _fake_copy_rollout), patch(
            "sparkvm.vm.FirecrackerProcess.start", _fake_start
        ), patch("sparkvm.vm.scrub_sensitive_execution_files", side_effect=RuntimeError("scrub failed")):
            with self.assertRaises(Exception):
                vm.run(self.rollout)

        worker = next((self.home / "workers").glob("vm-*"))
        payload = json.loads((worker / "failure.json").read_text(encoding="utf-8"))
        self.assertFalse(payload["execution_disk_preserved"])
        self.assertFalse(payload["secret_scrubbed"])
        self.assertFalse((worker / "rollout.ext4").exists())


if __name__ == "__main__":
    unittest.main()
