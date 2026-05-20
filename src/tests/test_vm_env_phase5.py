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
from sparkvm.errors import ExecutionDiskError
from sparkvm.errors import FirecrackerBinaryNotInstalled
from sparkvm.image import BaseImage
from sparkvm.result import VMResult
from sparkvm.rollouts import Rollout
from sparkvm.vm import SparkVM, render_env_file, shell_quote


class _FakeAPI:
    def __init__(self, socket_path: Path, timeout_sec: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout_sec = timeout_sec

    def put(self, _path: str, _payload: dict[str, object]) -> None:
        return None

    def get(self, _path: str) -> None:
        return None


class _FakeFirecrackerProcess:
    def __init__(self, *, firecracker_bin: Path, socket_path: Path, log_path: Path | None = None) -> None:
        self.firecracker_bin = firecracker_bin
        self.socket_path = socket_path
        self.log_path = log_path
        self._running = False

    def start(self, startup_timeout_sec: float = 5.0) -> None:
        del startup_timeout_sec
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.write_text("", encoding="utf-8")
        if self.log_path is not None:
            self.log_path.write_text("fake-firecracker-log\n", encoding="utf-8")
        self._running = True

    def wait(self, timeout_sec: float | None = None) -> int:
        del timeout_sec
        return 0

    def stop(self) -> None:
        self._running = False

    def poll(self) -> int | None:
        return None if self._running else 0


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

    def test_runtime_execution_files_include_runtime_env(self) -> None:
        vm = SparkVM(home_dir=self.home, env={"OPENAI_API_KEY": "sk-123"}, network=True)
        files = vm.runtime_execution_files(network_config=None)

        self.assertIn(".sparkvm/runtime.env", files)
        runtime_env = files[".sparkvm/runtime.env"]
        self.assertIn("SPARKVM_SETUP_TIMEOUT_SEC=300", runtime_env)
        self.assertIn("SPARKVM_RUN_TIMEOUT_SEC=300", runtime_env)

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
        vm = SparkVM(home_dir=self.home, env={"OPENAI_API_KEY": "top-secret"}, keep_disk_on_failure=True)
        vm._setup.ensure_layout()
        (self.home / "images").mkdir(parents=True, exist_ok=True)
        (self.home / "images" / "fake-rootfs.ext4").write_bytes(b"rootfs")
        vm._setup.firecracker_binary_path = MethodType(lambda _self: Path("/fake/firecracker"), vm._setup)
        vm._setup.assert_kvm_available = MethodType(lambda _self: None, vm._setup)
        vm._images.resolve = MethodType(
            lambda _self, runtime=None: BaseImage(
                name="python-3.12-slim",
                kernel_image=Path("/fake/vmlinux"),
                rootfs_image=(self.home / "images" / "fake-rootfs.ext4"),
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
        ), patch("sparkvm.vm.SparkVM.scrub_and_redact_execution_disk", side_effect=RuntimeError("scrub failed")):
            with self.assertRaises(Exception):
                vm.run(self.rollout)

        worker = next((self.home / "workers").glob("vm-*"))
        payload = json.loads((worker / "failure.json").read_text(encoding="utf-8"))
        self.assertFalse(payload["execution_disk_preserved"])
        self.assertFalse(payload["secret_scrubbed"])
        self.assertFalse((worker / "rollout.ext4").exists())

    def test_keep_disk_with_env_scrubs_before_preserving(self) -> None:
        vm = SparkVM(
            home_dir=self.home,
            env={"OPENAI_API_KEY": "top-secret"},
            keep_disk_on_failure=True,
        )
        vm._setup.ensure_layout()
        (self.home / "images").mkdir(parents=True, exist_ok=True)
        (self.home / "images" / "fake-rootfs.ext4").write_bytes(b"rootfs")
        vm._setup.firecracker_binary_path = MethodType(lambda _self: Path("/fake/firecracker"), vm._setup)
        vm._setup.assert_kvm_available = MethodType(lambda _self: None, vm._setup)
        vm._images.resolve = MethodType(
            lambda _self, runtime=None: BaseImage(
                name="python-3.12-slim",
                kernel_image=Path("/fake/vmlinux"),
                rootfs_image=(self.home / "images" / "fake-rootfs.ext4"),
                boot_args="console=ttyS0",
            ),
            vm._images,
        )
        vm.wait_for_firecracker_socket = MethodType(lambda self, api, process, timeout_sec: None, vm)
        vm.configure_microvm = MethodType(
            lambda self, api, runtime_image, worker_rootfs_path, execution_disk_path: None,
            vm,
        )

        def _fake_copy_rollout(self_obj, runtime_files=None):  # noqa: ANN001
            del runtime_files
            self_obj.path.parent.mkdir(parents=True, exist_ok=True)
            self_obj.path.write_bytes(b"fake-ext4")

        def _fake_read_result(self_obj, vm_id: str, duration_ms: int, firecracker_log_path: Path | None) -> VMResult:  # noqa: ANN001
            return VMResult(
                rollout_id=self.rollout.id,
                rollout_name=self.rollout.name,
                rollout_mode=self.rollout.mode,
                runtime=self.rollout.base_image,
                vm_id=vm_id,
                status="run_failed",
                exit_code=1,
                duration_ms=duration_ms,
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=self_obj.path,
            )

        with patch("sparkvm.vm.FirecrackerAPIClient", _FakeAPI), patch(
            "sparkvm.vm.FirecrackerProcess", _FakeFirecrackerProcess
        ), patch("sparkvm.vm.ExecutionDisk.copy_rollout", _fake_copy_rollout), patch(
            "sparkvm.vm.ExecutionDisk.read_result", _fake_read_result
        ), patch("sparkvm.vm.SparkVM.scrub_and_redact_execution_disk", return_value=None) as scrub_mock:
            vm.run(self.rollout)

        worker = next((self.home / "workers").glob("vm-*"))
        self.assertTrue((worker / "rollout.ext4").exists())
        scrub_mock.assert_called_once()
        payload = json.loads((worker / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["execution_disk_preserved"])

    def test_keep_disk_with_env_and_scrub_failure_deletes_execution_disk(self) -> None:
        vm = SparkVM(
            home_dir=self.home,
            env={"OPENAI_API_KEY": "top-secret"},
            keep_disk_on_failure=True,
        )
        vm._setup.ensure_layout()
        (self.home / "images").mkdir(parents=True, exist_ok=True)
        (self.home / "images" / "fake-rootfs.ext4").write_bytes(b"rootfs")
        vm._setup.firecracker_binary_path = MethodType(lambda _self: Path("/fake/firecracker"), vm._setup)
        vm._setup.assert_kvm_available = MethodType(lambda _self: None, vm._setup)
        vm._images.resolve = MethodType(
            lambda _self, runtime=None: BaseImage(
                name="python-3.12-slim",
                kernel_image=Path("/fake/vmlinux"),
                rootfs_image=(self.home / "images" / "fake-rootfs.ext4"),
                boot_args="console=ttyS0",
            ),
            vm._images,
        )
        vm.wait_for_firecracker_socket = MethodType(lambda self, api, process, timeout_sec: None, vm)
        vm.configure_microvm = MethodType(
            lambda self, api, runtime_image, worker_rootfs_path, execution_disk_path: None,
            vm,
        )

        def _fake_copy_rollout(self_obj, runtime_files=None):  # noqa: ANN001
            del runtime_files
            self_obj.path.parent.mkdir(parents=True, exist_ok=True)
            self_obj.path.write_bytes(b"fake-ext4")

        def _fake_read_result(self_obj, vm_id: str, duration_ms: int, firecracker_log_path: Path | None) -> VMResult:  # noqa: ANN001
            return VMResult(
                rollout_id=self.rollout.id,
                rollout_name=self.rollout.name,
                rollout_mode=self.rollout.mode,
                runtime=self.rollout.base_image,
                vm_id=vm_id,
                status="run_failed",
                exit_code=1,
                duration_ms=duration_ms,
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=self_obj.path,
            )

        with patch("sparkvm.vm.FirecrackerAPIClient", _FakeAPI), patch(
            "sparkvm.vm.FirecrackerProcess", _FakeFirecrackerProcess
        ), patch("sparkvm.vm.ExecutionDisk.copy_rollout", _fake_copy_rollout), patch(
            "sparkvm.vm.ExecutionDisk.read_result", _fake_read_result
        ), patch("sparkvm.vm.SparkVM.scrub_and_redact_execution_disk", side_effect=RuntimeError("scrub failed")):
            vm.run(self.rollout)

        worker = next((self.home / "workers").glob("vm-*"))
        self.assertFalse((worker / "rollout.ext4").exists())
        payload = json.loads((worker / "result.json").read_text(encoding="utf-8"))
        self.assertFalse(payload["execution_disk_preserved"])
        self.assertTrue(payload["secret_scrub_failed"])

    def test_env_value_redacted_from_preserved_firecracker_log(self) -> None:
        secret = "sk-super-secret"
        vm = SparkVM(home_dir=self.home, env={"OPENAI_API_KEY": secret})
        vm._setup.ensure_layout()
        (self.home / "images").mkdir(parents=True, exist_ok=True)
        (self.home / "images" / "fake-rootfs.ext4").write_bytes(b"rootfs")
        vm._setup.firecracker_binary_path = MethodType(lambda _self: Path("/fake/firecracker"), vm._setup)
        vm._setup.assert_kvm_available = MethodType(lambda _self: None, vm._setup)
        vm._images.resolve = MethodType(
            lambda _self, runtime=None: BaseImage(
                name="python-3.12-slim",
                kernel_image=Path("/fake/vmlinux"),
                rootfs_image=(self.home / "images" / "fake-rootfs.ext4"),
                boot_args="console=ttyS0",
            ),
            vm._images,
        )
        vm.wait_for_firecracker_socket = MethodType(lambda self, api, process, timeout_sec: None, vm)
        vm.configure_microvm = MethodType(
            lambda self, api, runtime_image, worker_rootfs_path, execution_disk_path: None,
            vm,
        )

        def _fake_copy_rollout(self_obj, runtime_files=None):  # noqa: ANN001
            del runtime_files
            self_obj.path.parent.mkdir(parents=True, exist_ok=True)
            self_obj.path.write_bytes(b"fake-ext4")

        def _fake_read_result(self_obj, vm_id: str, duration_ms: int, firecracker_log_path: Path | None) -> VMResult:  # noqa: ANN001
            assert firecracker_log_path is not None
            firecracker_log_path.write_text(f"token={secret}\n", encoding="utf-8")
            return VMResult(
                rollout_id=self.rollout.id,
                rollout_name=self.rollout.name,
                rollout_mode=self.rollout.mode,
                runtime=self.rollout.base_image,
                vm_id=vm_id,
                status="run_failed",
                exit_code=1,
                duration_ms=duration_ms,
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=self_obj.path,
            )

        with patch("sparkvm.vm.FirecrackerAPIClient", _FakeAPI), patch(
            "sparkvm.vm.FirecrackerProcess", _FakeFirecrackerProcess
        ), patch("sparkvm.vm.ExecutionDisk.copy_rollout", _fake_copy_rollout), patch(
            "sparkvm.vm.ExecutionDisk.read_result", _fake_read_result
        ):
            vm.run(self.rollout)

        worker = next((self.home / "workers").glob("vm-*"))
        log_text = (worker / "firecracker.log").read_text(encoding="utf-8")
        self.assertNotIn(secret, log_text)

    def test_env_values_never_appear_in_result_json(self) -> None:
        secret = "sk-never-store"
        vm = SparkVM(home_dir=self.home, env={"OPENAI_API_KEY": secret})
        vm._setup.ensure_layout()
        (self.home / "images").mkdir(parents=True, exist_ok=True)
        (self.home / "images" / "fake-rootfs.ext4").write_bytes(b"rootfs")
        vm._setup.firecracker_binary_path = MethodType(lambda _self: Path("/fake/firecracker"), vm._setup)
        vm._setup.assert_kvm_available = MethodType(lambda _self: None, vm._setup)
        vm._images.resolve = MethodType(
            lambda _self, runtime=None: BaseImage(
                name="python-3.12-slim",
                kernel_image=Path("/fake/vmlinux"),
                rootfs_image=(self.home / "images" / "fake-rootfs.ext4"),
                boot_args="console=ttyS0",
            ),
            vm._images,
        )
        vm.wait_for_firecracker_socket = MethodType(lambda self, api, process, timeout_sec: None, vm)
        vm.configure_microvm = MethodType(
            lambda self, api, runtime_image, worker_rootfs_path, execution_disk_path: None,
            vm,
        )

        def _fake_copy_rollout(self_obj, runtime_files=None):  # noqa: ANN001
            del runtime_files
            self_obj.path.parent.mkdir(parents=True, exist_ok=True)
            self_obj.path.write_bytes(b"fake-ext4")

        def _fake_read_result(self_obj, vm_id: str, duration_ms: int, firecracker_log_path: Path | None) -> VMResult:  # noqa: ANN001
            return VMResult(
                rollout_id=self.rollout.id,
                rollout_name=self.rollout.name,
                rollout_mode=self.rollout.mode,
                runtime=self.rollout.base_image,
                vm_id=vm_id,
                status="run_failed",
                exit_code=1,
                duration_ms=duration_ms,
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=self_obj.path,
            )

        with patch("sparkvm.vm.FirecrackerAPIClient", _FakeAPI), patch(
            "sparkvm.vm.FirecrackerProcess", _FakeFirecrackerProcess
        ), patch("sparkvm.vm.ExecutionDisk.copy_rollout", _fake_copy_rollout), patch(
            "sparkvm.vm.ExecutionDisk.read_result", _fake_read_result
        ):
            vm.run(self.rollout)

        worker = next((self.home / "workers").glob("vm-*"))
        result_text = (worker / "result.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, result_text)

    def test_extract_partial_results_handles_missing_files(self) -> None:
        vm = SparkVM(home_dir=self.home)
        execution_disk_path = self.home / "rollout.ext4"
        execution_disk_path.write_bytes(b"fake-ext4")
        output_results_dir = self.home / "worker" / "results"

        with patch("sparkvm.vm.run_checked", return_value=None), patch("sparkvm.vm.unmount_ext4", return_value=None):
            meta = vm.extract_partial_results_from_execution_disk(
                execution_disk_path=execution_disk_path,
                output_results_dir=output_results_dir,
                env={},
            )

        self.assertFalse(meta["partial_results_extracted"])
        self.assertEqual([], meta["files"])
        self.assertIsNone(meta["partial_results_error"])

    def test_extract_partial_results_handles_mount_failure(self) -> None:
        vm = SparkVM(home_dir=self.home)
        execution_disk_path = self.home / "rollout.ext4"
        execution_disk_path.write_bytes(b"fake-ext4")
        output_results_dir = self.home / "worker" / "results"

        with patch("sparkvm.vm.run_checked", side_effect=ExecutionDiskError("mount failed")):
            meta = vm.extract_partial_results_from_execution_disk(
                execution_disk_path=execution_disk_path,
                output_results_dir=output_results_dir,
                env={},
            )

        self.assertFalse(meta["partial_results_extracted"])
        self.assertIn("mount failed", str(meta["partial_results_error"]))

    def test_extract_partial_results_redacts_env_values(self) -> None:
        vm = SparkVM(home_dir=self.home, env={"OPENAI_API_KEY": "sk-secret"})
        execution_disk_path = self.home / "rollout.ext4"
        execution_disk_path.write_bytes(b"fake-ext4")
        output_results_dir = self.home / "worker" / "results"

        def _fake_mount(cmd, error_factory):  # noqa: ANN001
            if cmd[:2] == ["mount", "-o"]:
                mount_dir = Path(cmd[-1])
                (mount_dir / "results").mkdir(parents=True, exist_ok=True)
                (mount_dir / "results" / "run.stdout.log").write_text("token=sk-secret\n", encoding="utf-8")
            return None

        with patch("sparkvm.vm.run_checked", side_effect=_fake_mount), patch("sparkvm.vm.unmount_ext4", return_value=None):
            meta = vm.extract_partial_results_from_execution_disk(
                execution_disk_path=execution_disk_path,
                output_results_dir=output_results_dir,
                env={"OPENAI_API_KEY": "sk-secret"},
            )

        self.assertTrue(meta["partial_results_extracted"])
        rendered = (output_results_dir / "run.stdout.log").read_text(encoding="utf-8")
        self.assertNotIn("sk-secret", rendered)
        self.assertIn("[REDACTED]", rendered)


if __name__ == "__main__":
    unittest.main()
