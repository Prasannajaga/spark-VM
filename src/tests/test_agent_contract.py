from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sparkvm.rollouts import Rollouts
from sparkvm.utils import ResolvedCommand
from sparkvm.vm import SparkVM
from sparkvm.image_builder import BuiltImage


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


if __name__ == "__main__":
    unittest.main()
