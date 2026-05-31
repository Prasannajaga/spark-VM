from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sparkvm.rollouts import Rollouts
from sparkvm.core.constants import BOOT_ARGS, SPARKVM_INIT_TEMPLATE
from sparkvm.core.errors import ExecutionDiskError
from sparkvm.core.utils import ResolvedCommand
from sparkvm.vm import SparkVM
from sparkvm.machine.image import RuntimeImage
from sparkvm.machine.image_builder import BuiltImage
from sparkvm.api.rollouts import Rollout
from sparkvm.api.vm import SparkVM as SparkVMImpl
from sparkvm.cli.main import run_rollout_execute


class _FakeFirecrackerAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def put(self, path: str, payload: dict[str, object]) -> None:
        self.calls.append((path, payload))

    def attach_entropy(self) -> None:
        self.put("/entropy", {})


class TestAgentContract(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.workspace = Path(self.tmp.name) / "workspace"
        self.home.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "Dockerfile").write_text("FROM busybox\nCMD [\"echo\", \"hi\"]\n", encoding="utf-8")

    def test_rollouts_create_is_dockerfile_only(self) -> None:
        os.environ["SPARKVM_HOME"] = str(self.home)
        with patch("pathlib.Path.cwd", return_value=self.workspace.resolve()):
            manager = Rollouts()

            with self.assertRaises(Exception):
                manager.create(name="x", runtime="python-3.12")

            with self.assertRaises(Exception):
                manager.create(name="x", runtime="Dockerfile", source="/tmp")

    def test_rollout_metadata_shape(self) -> None:
        os.environ["SPARKVM_HOME"] = str(self.home)
        built = BuiltImage(
            id="image-rollout-unit",
            rollout_id="rollout-unit",
            path=self.home / "images" / "image-rollout-unit.ext4",
            metadata_path=self.home / "images" / "image-rollout-unit.json",
            docker_image_tag="sparkvm-rollout:rollout-unit",
            resolved_run_command=ResolvedCommand(
                source="docker_config",
                working_dir="/workspace",
                command="echo hi",
                entrypoint=None,
                cmd=["echo", "hi"],
            ),
            size_mb=4096,
            created_at="2026-01-01T00:00:00Z",
        )
        with patch("pathlib.Path.cwd", return_value=self.workspace.resolve()):
            with patch("sparkvm.rollouts.RolloutImageBuilder.build_from_dockerfile", return_value=built):
                rollout = Rollouts().create(name="unit", runtime="Dockerfile", deleteOnSuccess=True)

        self.assertEqual("Dockerfile", rollout.runtime)
        self.assertTrue(rollout.delete_on_success)
        payload = rollout.to_metadata_entry()
        self.assertIn("id", payload)
        self.assertIn("name", payload)
        self.assertIn("runtime", payload)
        self.assertIn("image_path", payload)
        self.assertIn("deleteOnSuccess", payload)
        self.assertIn("created_at", payload)

    def test_rollouts_create_supports_explicit_dockerfile_path(self) -> None:
        os.environ["SPARKVM_HOME"] = str(self.home)
        dockerfile_alt = self.workspace / "simplegithub.Dockerfile"
        dockerfile_alt.write_text("FROM busybox\nCMD [\"echo\", \"alt\"]\n", encoding="utf-8")
        built = BuiltImage(
            id="image-rollout-unit-alt",
            rollout_id="rollout-unit-alt",
            path=self.home / "images" / "image-rollout-unit-alt.ext4",
            metadata_path=self.home / "images" / "image-rollout-unit-alt.json",
            docker_image_tag="sparkvm-rollout:rollout-unit-alt",
            resolved_run_command=ResolvedCommand(
                source="docker_config",
                working_dir="/workspace",
                command="echo alt",
                entrypoint=None,
                cmd=["echo", "alt"],
            ),
            size_mb=4096,
            created_at="2026-01-01T00:00:00Z",
        )
        with patch("pathlib.Path.cwd", return_value=self.workspace.resolve()):
            with patch("sparkvm.rollouts.RolloutImageBuilder.build_from_dockerfile", return_value=built) as mocked_build:
                rollout = Rollouts().create(
                    name="unit-alt",
                    runtime="Dockerfile",
                    dockerfile=str(dockerfile_alt.resolve()),
                    deleteOnSuccess=False,
                )
        called = mocked_build.call_args.kwargs
        self.assertEqual(dockerfile_alt.resolve(), called["dockerfile_path"])
        self.assertEqual(str(dockerfile_alt.resolve()), rollout.dockerfile)

    def test_rollouts_create_returns_existing_same_name_without_rebuild(self) -> None:
        os.environ["SPARKVM_HOME"] = str(self.home)
        built = BuiltImage(
            id="image-rollout-existing",
            rollout_id="rollout-existing",
            path=self.home / "images" / "image-rollout-existing.ext4",
            metadata_path=self.home / "images" / "image-rollout-existing.json",
            docker_image_tag="sparkvm-rollout:rollout-existing",
            resolved_run_command=ResolvedCommand(
                source="docker_config",
                working_dir="/workspace",
                command="echo hi",
                entrypoint=None,
                cmd=["echo", "hi"],
            ),
            size_mb=4096,
            created_at="2026-01-01T00:00:00Z",
        )
        with patch("pathlib.Path.cwd", return_value=self.workspace.resolve()):
            with patch("sparkvm.rollouts.RolloutImageBuilder.build_from_dockerfile", return_value=built) as mocked_build:
                first = Rollouts().create(name="same-name", runtime="Dockerfile", deleteOnSuccess=False)
                second = Rollouts().create(name="same-name", runtime="Dockerfile", deleteOnSuccess=True)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.path, second.path)
        self.assertEqual(1, mocked_build.call_count)

    def test_vm_run_accepts_only_rollout_id(self) -> None:
        sig = inspect.signature(SparkVM.run)
        self.assertEqual(["self", "rollout_id"], list(sig.parameters.keys()))

        vm = SparkVM(vcpu=1, memory="512M", disk="1G", timeout=1.0, network=False, env={})
        with self.assertRaises(TypeError):
            vm.run("rollout-abc", object())  # type: ignore[arg-type]

    def test_guest_phase_timeouts_follow_vm_timeout(self) -> None:
        rollout = Rollout(
            id="rollout-timeout-env",
            name="timeout-env",
            runtime="Dockerfile",
            path=self.home / "rollouts" / "rollout-timeout-env",
            image_path=str(self.home / "images" / "image.ext4"),
            delete_on_success=False,
            created_at="2026-01-01T00:00:00Z",
            runtime_image={},
            dockerfile="Dockerfile",
            resolved_run_command={},
            vm_config={},
        )
        vm = SparkVMImpl(vcpu=1, memory="512M", disk="1G", timeout=17.0, network=False, env={})

        files = vm._runtime_execution_files(rollout=rollout, network_config=None)

        self.assertIn("SPARKVM_SETUP_TIMEOUT_SEC=17", files[".sparkvm/runtime.env"])
        self.assertIn("SPARKVM_RUN_TIMEOUT_SEC=17", files[".sparkvm/runtime.env"])
        self.assertIn(".sparkvm/entropy.seed", files)
        self.assertEqual(64, len(files[".sparkvm/entropy.seed"]))

    def test_microvm_config_attaches_entropy_before_boot(self) -> None:
        vm = SparkVMImpl(vcpu=1, memory="512M", disk="1G", timeout=1.0, network=False, env={})
        api = _FakeFirecrackerAPI()
        runtime_image = RuntimeImage(
            name="test",
            kernel_image=self.home / "images" / "vmlinux",
            rootfs_image=self.home / "images" / "rootfs.ext4",
            boot_args="console=ttyS0",
        )

        vm._configure_microvm(
            api=api,  # type: ignore[arg-type]
            runtime_image=runtime_image,
            worker_rootfs_path=self.home / "workers" / "worker-test" / "rootfs.ext4",
            execution_disk_path=self.home / "workers" / "worker-test" / "execution.ext4",
        )

        paths = [path for path, _ in api.calls]
        self.assertIn("/entropy", paths)
        self.assertLess(paths.index("/entropy"), paths.index("/drives/rootfs"))

    def test_default_boot_args_trust_cpu_entropy(self) -> None:
        self.assertIn("random.trust_cpu=on", BOOT_ARGS)

    def test_guest_init_waits_for_entropy_before_user_phases(self) -> None:
        self.assertIn("credit_guest_entropy_seed()", SPARKVM_INIT_TEMPLATE)
        self.assertIn("RNDADDENTROPY", SPARKVM_INIT_TEMPLATE)
        self.assertIn("wait_for_guest_entropy()", SPARKVM_INIT_TEMPLATE)
        credit_call = SPARKVM_INIT_TEMPLATE.rindex("credit_guest_entropy_seed")
        wait_call = SPARKVM_INIT_TEMPLATE.rindex("wait_for_guest_entropy")
        self.assertLess(credit_call, wait_call)
        self.assertLess(wait_call, SPARKVM_INIT_TEMPLATE.index('run_phase "setup"'))
        self.assertLess(wait_call, SPARKVM_INIT_TEMPLATE.index('run_phase "run"'))

    def test_partial_results_extraction_uses_debugfs_without_mount(self) -> None:
        worker_dir = self.home / "workers" / "worker-test"
        results_dir = worker_dir / "results"
        execution_disk = worker_dir / "execution.ext4"
        execution_disk.parent.mkdir(parents=True, exist_ok=True)
        execution_disk.write_bytes(b"not-a-real-ext4")

        def fake_debugfs_dump_file(image_path, fs_path, output_path):  # type: ignore[no-untyped-def]
            self.assertEqual(execution_disk, image_path)
            if fs_path != "/results/run.stdout.log":
                return False
            output_path.write_text("debugfs-result\n", encoding="utf-8")
            return True

        vm = SparkVMImpl(vcpu=1, memory="512M", disk="1G", timeout=1.0, network=False, env={})
        with (
            patch.object(vm, "_mount_partial_results_disk", side_effect=AssertionError("partial results must not mount")),
            patch("sparkvm.api.vm.run_checked", side_effect=AssertionError("partial results must not run mount")),
            patch("sparkvm.api.vm.debugfs_dump_file", side_effect=fake_debugfs_dump_file),
        ):
            metadata = vm._extract_partial_results_from_execution_disk(
                execution_disk_path=execution_disk,
                output_results_dir=results_dir,
                env={},
            )

        self.assertTrue(metadata["partial_results_extracted"])
        self.assertEqual(["run.stdout.log"], metadata["files"])
        self.assertEqual("debugfs-result\n", (results_dir / "run.stdout.log").read_text(encoding="utf-8"))

    def test_partial_results_mount_retries_missing_mountpoint(self) -> None:
        worker_dir = self.home / "workers" / "worker-retry"
        mount_dir = worker_dir / "mnt" / "rollout-partial-results-mount"
        execution_disk = worker_dir / "execution.ext4"
        execution_disk.parent.mkdir(parents=True, exist_ok=True)
        execution_disk.write_bytes(b"not-a-real-ext4")
        calls = 0

        def fake_run_checked(cmd, *, error_factory):  # type: ignore[no-untyped-def]
            nonlocal calls
            del cmd, error_factory
            calls += 1
            self.assertTrue(mount_dir.exists())
            if calls == 1:
                raise ExecutionDiskError("mount: mount point does not exist")

        vm = SparkVMImpl(vcpu=1, memory="512M", disk="1G", timeout=1.0, network=False, env={})
        with patch("sparkvm.api.vm.run_checked", side_effect=fake_run_checked):
            vm._mount_partial_results_disk(execution_disk_path=execution_disk, mount_dir=mount_dir)

        self.assertEqual(2, calls)

    def test_partial_results_mount_falls_back_to_read_write(self) -> None:
        worker_dir = self.home / "workers" / "worker-rw"
        mount_dir = worker_dir / "mnt" / "rollout-partial-results-mount"
        execution_disk = worker_dir / "execution.ext4"
        execution_disk.parent.mkdir(parents=True, exist_ok=True)
        execution_disk.write_bytes(b"not-a-real-ext4")
        commands: list[list[str]] = []

        def fake_run_checked(cmd, *, error_factory):  # type: ignore[no-untyped-def]
            del error_factory
            commands.append(list(cmd))
            if "loop,ro" in cmd:
                raise ExecutionDiskError("cannot mount read-only")

        vm = SparkVMImpl(vcpu=1, memory="512M", disk="1G", timeout=1.0, network=False, env={})
        with patch("sparkvm.api.vm.run_checked", side_effect=fake_run_checked):
            vm._mount_partial_results_disk(execution_disk_path=execution_disk, mount_dir=mount_dir)

        self.assertEqual("loop,ro", commands[0][2])
        self.assertEqual("loop,rw", commands[1][2])

    def test_partial_results_reads_debugfs_results(self) -> None:
        worker_dir = self.home / "workers" / "worker-debugfs"
        results_dir = worker_dir / "results"
        execution_disk = worker_dir / "execution.ext4"
        execution_disk.parent.mkdir(parents=True, exist_ok=True)
        execution_disk.write_bytes(b"not-a-real-ext4")

        def fake_debugfs_dump_file(image_path, fs_path, output_path):  # type: ignore[no-untyped-def]
            self.assertEqual(execution_disk, image_path)
            if fs_path != "/results/run.stdout.log":
                return False
            output_path.write_text("NETWORK_IPV4_RESULT=PASS\n", encoding="utf-8")
            return True

        vm = SparkVMImpl(vcpu=1, memory="512M", disk="1G", timeout=1.0, network=False, env={})
        with patch("sparkvm.api.vm.debugfs_dump_file", side_effect=fake_debugfs_dump_file):
            metadata = vm._extract_partial_results_from_execution_disk(
                execution_disk_path=execution_disk,
                output_results_dir=results_dir,
                env={},
            )

        self.assertTrue(metadata["partial_results_extracted"])
        self.assertIsNone(metadata["partial_results_error"])
        self.assertEqual("NETWORK_IPV4_RESULT=PASS\n", (results_dir / "run.stdout.log").read_text(encoding="utf-8"))

    def test_partial_results_failure_returns_metadata_without_raising(self) -> None:
        worker_dir = self.home / "workers" / "worker-graceful"
        results_dir = worker_dir / "results"
        execution_disk = worker_dir / "execution.ext4"
        execution_disk.parent.mkdir(parents=True, exist_ok=True)
        execution_disk.write_bytes(b"not-a-real-ext4")

        vm = SparkVMImpl(vcpu=1, memory="512M", disk="1G", timeout=1.0, network=False, env={})
        with patch.object(vm, "_extract_partial_results_with_debugfs", side_effect=ExecutionDiskError("debugfs failed")):
            metadata = vm._extract_partial_results_from_execution_disk(
                execution_disk_path=execution_disk,
                output_results_dir=results_dir,
                env={},
            )

        self.assertFalse(metadata["partial_results_extracted"])
        self.assertIn("debugfs failed", str(metadata["partial_results_error"]))

    def test_workers_run_uses_rollout_vm_config_when_not_overridden(self) -> None:
        rollout = Rollout(
            id="rollout-timeout",
            name="timeout",
            runtime="Dockerfile",
            path=self.home / "rollouts" / "rollout-timeout",
            image_path=str(self.home / "images" / "image.ext4"),
            delete_on_success=False,
            created_at="2026-01-01T00:00:00Z",
            runtime_image={},
            dockerfile="Dockerfile",
            resolved_run_command={},
            vm_config={
                "vcpu": 3,
                "memory": "3G",
                "disk": "5G",
                "timeout": 300.0,
                "network": False,
                "env": {"FROM_ROLLOUT": "1"},
            },
        )
        constructed: dict[str, object] = {}

        class FakeSparkVM:
            def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
                constructed.update(kwargs)

            def run(self, rollout_id: str):  # type: ignore[no-untyped-def]
                from sparkvm.api.result import VMResult

                return VMResult(
                    rollout_id=rollout_id,
                    rollout_name="timeout",
                    rollout_mode="dockerfile",
                    runtime="Dockerfile",
                    vm_id="worker-test",
                    status="passed",
                    exit_code=0,
                    duration_ms=1,
                )

        with (
            patch("sparkvm.cli.main.Rollouts.get", return_value=rollout),
            patch("sparkvm.api.vm.SparkVM", FakeSparkVM),
        ):
            run_rollout_execute(
                None,
                rollout_id=rollout.id,
                vcpu=None,
                memory=None,
                disk=None,
                timeout=None,
                network=None,
                env_pairs=None,
            )

        self.assertEqual(300.0, constructed["timeout"])
        self.assertEqual(3, constructed["vcpu"])
        self.assertEqual(False, constructed["network"])


if __name__ == "__main__":
    unittest.main()
