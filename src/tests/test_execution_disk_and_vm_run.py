from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import MethodType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.disk import ExecutionDisk
from sparkvm.image import BaseImage
from sparkvm.result import PhaseResult, VMResult
from sparkvm.rollouts import Rollout
from sparkvm.vm import SparkVM


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

    def start(self, startup_timeout_sec: float = 5.0) -> None:
        del startup_timeout_sec
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.write_text("", encoding="utf-8")
        if self.log_path is not None:
            self.log_path.write_text("fake-firecracker-log\n", encoding="utf-8")

    def wait(self, timeout_sec: float | None = None) -> int:
        del timeout_sec
        return 0

    def stop(self) -> None:
        return None

    def poll(self) -> int | None:
        return None


class _FakeExecutionDisk:
    last_size_mb: int | None = None

    def __init__(self, *, rollout: Rollout, path: Path, size_mb: int, mount_base: Path) -> None:
        del mount_base
        self.rollout = rollout
        self.path = path
        _FakeExecutionDisk.last_size_mb = size_mb

    def copy_rollout(self, runtime_files: dict[str, str] | None = None) -> None:
        del runtime_files
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"fake-ext4")

    def read_result(self, vm_id: str, duration_ms: int, firecracker_log_path: Path | None) -> VMResult:
        return VMResult(
            rollout_id=self.rollout.id,
            rollout_name=self.rollout.name,
            rollout_mode=self.rollout.mode,
            runtime=self.rollout.base_image,
            vm_id=vm_id,
            status="passed",
            exit_code=0,
            duration_ms=duration_ms,
            run=PhaseResult(name="run", stdout="ok\n", stderr="", exit_code=0),
            firecracker_log_path=firecracker_log_path,
            execution_disk_path=self.path,
        )

    def cleanup(self, remove_disk: bool = True) -> None:
        if remove_disk and self.path.exists():
            self.path.unlink()


class ExecutionDiskAndVMRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-disk-vm-run-")
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_execution_disk_copy_rollout_copies_entire_directory(self) -> None:
        rollout_path = self.home / "rollout-source"
        (rollout_path / "repo").mkdir(parents=True, exist_ok=True)
        (rollout_path / "repo" / "main.py").write_text("print('repo')\n", encoding="utf-8")
        (rollout_path / "run.sh").write_text("#!/bin/sh\necho run\n", encoding="utf-8")
        (rollout_path / "rollout.json").write_text("{}\n", encoding="utf-8")

        rollout = Rollout(
            id="rollout-copy-test",
            name="copy",
            mode="repo",
            base_image="debian-minbase",
            path=rollout_path,
            command=None,
            setup_cmd=None,
            run_cmd="python3 main.py",
            disk_mb=2048,
            files=["repo/", "run.sh", "rollout.json"],
            created_at="2026-01-01T00:00:00Z",
        )
        disk = ExecutionDisk(
            rollout=rollout,
            path=self.home / "rollout.ext4",
            size_mb=2048,
            mount_base=self.home / "mnt",
        )

        seen = {"called": False}

        def _fake_create_ext4_image(path: Path, size_mib: int, *, source_dir: Path | None = None) -> Path:
            seen["called"] = True
            self.assertEqual(disk.path, path)
            self.assertEqual(2048, size_mib)
            self.assertIsNotNone(source_dir)
            staged = source_dir
            self.assertTrue((staged / "repo" / "main.py").exists())
            self.assertTrue((staged / "run.sh").exists())
            self.assertTrue((staged / "rollout.json").exists())
            return path

        with patch("sparkvm.disk.create_ext4_image", side_effect=_fake_create_ext4_image):
            disk.copy_rollout()

        self.assertTrue(seen["called"])

    def test_vm_run_uses_rollout_disk_mb(self) -> None:
        rollout_dir = self.home / "rollout-item"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        (rollout_dir / "rollout.json").write_text("{}", encoding="utf-8")
        rollout = Rollout(
            id="rollout-size-test",
            name="size",
            mode="script",
            base_image="debian-minbase",
            path=rollout_dir,
            command="python3 /job/main.py",
            setup_cmd=None,
            run_cmd="python3 /job/main.py",
            disk_mb=3072,
            files=["main.py", "run.sh"],
            created_at="2026-01-01T00:00:00Z",
        )

        vm = SparkVM(home_dir=self.home)
        vm._setup.ensure_layout()
        vm._setup.firecracker_binary_path = MethodType(lambda _self: Path("/fake/firecracker"), vm._setup)
        vm._setup.assert_kvm_available = MethodType(lambda _self: None, vm._setup)
        vm._images.resolve = MethodType(
            lambda _self, _runtime=None: BaseImage(
                name="debian-minbase",
                kernel_image=Path("/fake/vmlinux"),
                rootfs_image=(self.home / "images" / "fake-rootfs.ext4"),
                boot_args="console=ttyS0",
            ),
            vm._images,
        )
        (self.home / "images").mkdir(parents=True, exist_ok=True)
        (self.home / "images" / "fake-rootfs.ext4").write_bytes(b"rootfs")
        vm.wait_for_firecracker_socket = MethodType(
            lambda self, api, process, timeout_sec: None, vm
        )
        vm.configure_microvm = MethodType(
            lambda self, api, runtime_image, worker_rootfs_path, execution_disk_path: None, vm
        )

        _FakeExecutionDisk.last_size_mb = None
        with patch("sparkvm.vm.FirecrackerAPIClient", _FakeAPI), patch("sparkvm.vm.FirecrackerProcess", _FakeFirecrackerProcess), patch(
            "sparkvm.vm.ExecutionDisk",
            _FakeExecutionDisk,
        ):
            result = vm.run(rollout)

        self.assertEqual(3072, _FakeExecutionDisk.last_size_mb)
        self.assertEqual(0, result.exit_code)

    def test_vm_run_does_not_write_results_into_persistent_rollout(self) -> None:
        rollout_dir = self.home / "rollout-immutable"
        rollout_dir.mkdir(parents=True, exist_ok=True)
        (rollout_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")
        (rollout_dir / "run.sh").write_text("#!/bin/sh\npython3 /job/main.py\n", encoding="utf-8")
        rollout_json_path = rollout_dir / "rollout.json"
        rollout_json_path.write_text('{"id":"rollout-immutable"}\n', encoding="utf-8")
        original_rollout_json = rollout_json_path.read_text(encoding="utf-8")

        rollout = Rollout(
            id="rollout-immutable",
            name="immutable",
            mode="script",
            base_image="debian-minbase",
            path=rollout_dir,
            command="python3 /job/main.py",
            setup_cmd=None,
            run_cmd="python3 /job/main.py",
            disk_mb=1024,
            files=["main.py", "run.sh"],
            created_at="2026-01-01T00:00:00Z",
        )

        vm = SparkVM(home_dir=self.home)
        vm._setup.ensure_layout()
        vm._setup.firecracker_binary_path = MethodType(lambda _self: Path("/fake/firecracker"), vm._setup)
        vm._setup.assert_kvm_available = MethodType(lambda _self: None, vm._setup)
        (self.home / "images").mkdir(parents=True, exist_ok=True)
        (self.home / "images" / "fake-rootfs.ext4").write_bytes(b"rootfs")
        vm._images.resolve = MethodType(
            lambda _self, _runtime=None: BaseImage(
                name="debian-minbase",
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

        with patch("sparkvm.vm.FirecrackerAPIClient", _FakeAPI), patch(
            "sparkvm.vm.FirecrackerProcess",
            _FakeFirecrackerProcess,
        ), patch("sparkvm.vm.ExecutionDisk", _FakeExecutionDisk):
            vm.run(rollout)

        disallowed = [
            "results",
            "output.log",
            "error.log",
            "setup.stdout.log",
            "setup.stderr.log",
            "run.stdout.log",
            "run.stderr.log",
            "network.stdout.log",
            "network.stderr.log",
            "final_exit_code",
            "result.json",
            "failure.json",
            "env.sh",
            "runtime.env",
            "network.env",
        ]
        for name in disallowed:
            self.assertFalse((rollout_dir / name).exists(), f"rollout unexpectedly mutated: {name}")
        self.assertEqual(original_rollout_json, rollout_json_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
