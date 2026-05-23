from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import MethodType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.image import BaseImage
from sparkvm.result import VMResult
from sparkvm.rollouts import Rollout
from sparkvm.vm import RunConfig, SparkVM


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


class VMWorkersPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-vm-workers-policy-")
        self.home = Path(self.tmp.name)
        (self.home / "config.json").write_text(json.dumps({"resource_policy": {"max_vm_cpu_percent": 100, "max_vm_memory_percent": 100, "max_vm_disk_percent": 100, "min_host_cpu_percent": 0, "min_host_memory_percent": 0, "min_host_disk_percent": 0}}), encoding="utf-8")
        self.rollout_dir = self.home / "rollout-item"
        self.rollout_dir.mkdir(parents=True, exist_ok=True)
        (self.rollout_dir / "rollout.json").write_text("{}", encoding="utf-8")
        self.rollout = Rollout(
            id="rollout-example-1",
            name="example",
            mode="repo",
            base_image="debian-minbase",
            path=self.rollout_dir,
            command="python3 /job/source/main.py",
            setup_cmd=None,
            run_cmd="python3 /job/source/main.py",
            disk_mb=1024,
            files=["run.sh"],
            created_at="2026-01-01T00:00:00Z",
            updated_at=None,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _new_vm(self) -> SparkVM:
        vm = SparkVM(home_dir=self.home)
        vm._rollouts.get_by_id = MethodType(lambda _self, _rid: self.rollout, vm._rollouts)
        vm._setup.ensure_layout()
        (self.home / "images").mkdir(parents=True, exist_ok=True)
        (self.home / "images" / "fake-rootfs.ext4").write_bytes(b"rootfs")
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
        vm.wait_for_firecracker_socket = MethodType(lambda self, api, process, timeout_sec: None, vm)
        vm.configure_microvm = MethodType(lambda self, api, runtime_image, worker_rootfs_path, execution_disk_path: None, vm)
        return vm

    def test_non_zero_exit_code_preserves_worker_with_machine_specs(self) -> None:
        vm = self._new_vm()

        def _fake_copy_rollout(self_obj, runtime_files=None) -> None:  # noqa: ANN001
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
                exit_code=2,
                duration_ms=duration_ms,
                run=None,
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=self_obj.path,
            )

        with patch("sparkvm.vm.FirecrackerAPIClient", _FakeAPI), patch("sparkvm.vm.FirecrackerProcess", _FakeFirecrackerProcess), patch(
            "sparkvm.vm.ExecutionDisk.copy_rollout", _fake_copy_rollout
        ), patch("sparkvm.vm.ExecutionDisk.read_result", _fake_read_result):
            result = vm.run(self.rollout.id, config=RunConfig(vcpu=2, memory="2G", disk="4G", timeout=30, network=False))

        self.assertEqual(2, result.exit_code)
        worker_entries = list((self.home / "workers").glob("vm-*"))
        self.assertEqual(1, len(worker_entries))
        payload = json.loads((worker_entries[0] / "result.json").read_text(encoding="utf-8"))
        self.assertIn("machine_specs", payload)
        self.assertEqual(2, payload["machine_specs"]["vcpu"])
        self.assertEqual(2048, payload["machine_specs"]["memory"])
        self.assertEqual(4096, payload["machine_specs"]["disk"])
        self.assertFalse(payload["machine_specs"]["network"])

    def test_delete_on_success_invokes_rollout_delete(self) -> None:
        vm = self._new_vm()
        rollout = Rollout(
            id=self.rollout.id,
            name=self.rollout.name,
            mode=self.rollout.mode,
            base_image=self.rollout.base_image,
            path=self.rollout.path,
            command=self.rollout.command,
            setup_cmd=self.rollout.setup_cmd,
            run_cmd=self.rollout.run_cmd,
            disk_mb=self.rollout.disk_mb,
            files=self.rollout.files,
            created_at=self.rollout.created_at,
            delete_on_success=True,
        )
        vm._rollouts.get_by_id = MethodType(lambda _self, _rid: rollout, vm._rollouts)

        def _fake_copy_rollout(self_obj, runtime_files=None) -> None:  # noqa: ANN001
            del runtime_files
            self_obj.path.parent.mkdir(parents=True, exist_ok=True)
            self_obj.path.write_bytes(b"fake-ext4")

        def _fake_read_result(self_obj, vm_id: str, duration_ms: int, firecracker_log_path: Path | None) -> VMResult:  # noqa: ANN001
            return VMResult(
                rollout_id=rollout.id,
                rollout_name=rollout.name,
                rollout_mode=rollout.mode,
                runtime=rollout.base_image,
                vm_id=vm_id,
                status="passed",
                exit_code=0,
                duration_ms=duration_ms,
                run=None,
                firecracker_log_path=firecracker_log_path,
                execution_disk_path=self_obj.path,
            )

        called: list[str] = []
        vm._rollouts.delete_by_id = MethodType(lambda _self, rid: called.append(rid), vm._rollouts)

        with patch("sparkvm.vm.FirecrackerAPIClient", _FakeAPI), patch("sparkvm.vm.FirecrackerProcess", _FakeFirecrackerProcess), patch(
            "sparkvm.vm.ExecutionDisk.copy_rollout", _fake_copy_rollout
        ), patch("sparkvm.vm.ExecutionDisk.read_result", _fake_read_result):
            result = vm.run(rollout.id)

        self.assertTrue(result.passed)
        self.assertEqual([rollout.id], called)


if __name__ == "__main__":
    unittest.main()
