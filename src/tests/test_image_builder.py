from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.errors import RolloutBuildError, RolloutConfigError
from sparkvm.image_builder import RolloutImageBuilder, image_id_for_rollout, resolve_run_command


class ImageBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-image-builder-test-")
        self.home = Path(self.tmp.name)
        self.source = self.home / "rollouts" / "rollout-x" / "source"
        self.source.mkdir(parents=True, exist_ok=True)
        self.dockerfile = self.source / "Dockerfile"
        self.dockerfile.write_text("FROM alpine:latest\nCMD [\"echo\", \"hi\"]\n", encoding="utf-8")
        self.build_dir = self.home / "rollouts" / "rollout-x" / "build"
        self.image_path = self.home / "images" / "image-rollout-x.ext4"
        self.image_metadata_path = self.home / "images" / "image-rollout-x.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_image_id_for_rollout(self) -> None:
        self.assertEqual("image-rollout-version-3-abc", image_id_for_rollout("rollout-version-3-abc"))

    def test_resolve_run_command_combines_docker_exec_form(self) -> None:
        resolved = resolve_run_command(
            run_cmd=None,
            docker_working_dir="/workspace",
            docker_entrypoint=["python", "-m"],
            docker_cmd=["pytest", "-q"],
        )
        self.assertEqual("docker_config", resolved.source)
        self.assertEqual("/workspace", resolved.working_dir)
        self.assertEqual("python -m pytest -q", resolved.command)

    def test_resolve_run_command_uses_override(self) -> None:
        resolved = resolve_run_command(
            run_cmd="pytest -q",
            docker_working_dir="",
            docker_entrypoint=["python"],
            docker_cmd=["main.py"],
        )
        self.assertEqual("run_cmd", resolved.source)
        self.assertEqual("/workspace", resolved.working_dir)
        self.assertEqual("pytest -q", resolved.command)

    def test_resolve_run_command_requires_command(self) -> None:
        with self.assertRaises(RolloutConfigError):
            resolve_run_command(
                run_cmd=None,
                docker_working_dir=None,
                docker_entrypoint=None,
                docker_cmd=None,
            )

    def test_build_from_dockerfile_runs_expected_docker_flow_and_writes_metadata(self) -> None:
        run_checked_calls: list[list[str]] = []
        subprocess_calls: list[list[str]] = []

        def fake_subprocess_run(cmd, stdout=None, stderr=None, check=False):  # noqa: ANN001
            del stdout, stderr, check
            subprocess_calls.append([str(part) for part in cmd])
            return subprocess.CompletedProcess(cmd, 0)

        def fake_run_checked(cmd, *, error_factory, cwd=None, **kwargs):  # noqa: ANN001
            del error_factory, cwd, kwargs
            cmd_list = [str(part) for part in cmd]
            run_checked_calls.append(cmd_list)
            if cmd_list[:3] == ["docker", "image", "inspect"]:
                payload = [{"Config": {"WorkingDir": "/workspace", "Entrypoint": None, "Cmd": ["echo", "hi"], "Env": ["A=B"]}}]
                return subprocess.CompletedProcess(cmd_list, 0, stdout=json.dumps(payload), stderr="")
            return subprocess.CompletedProcess(cmd_list, 0, stdout="", stderr="")

        def fake_convert(**kwargs):  # noqa: ANN001
            output_path = kwargs["output_path"]
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"ext4")

        with patch("sparkvm.image_builder.subprocess.run", side_effect=fake_subprocess_run), patch(
            "sparkvm.image_builder.run_checked", side_effect=fake_run_checked
        ), patch("sparkvm.image_builder.convert_docker_export_to_ext4", side_effect=fake_convert):
            built = RolloutImageBuilder().build_from_dockerfile(
                rollout_id="rollout-x",
                source_dir=self.source,
                dockerfile_path=self.dockerfile,
                run_cmd=None,
                disk_mb=4096,
                image_path=self.image_path,
                image_metadata_path=self.image_metadata_path,
                build_dir=self.build_dir,
            )

        self.assertEqual(["docker", "build", "-f", str(self.dockerfile), "-t", "sparkvm-rollout:rollout-x", str(self.source)], subprocess_calls[0])
        self.assertIn(["docker", "image", "inspect", "sparkvm-rollout:rollout-x"], run_checked_calls)
        self.assertIn(["docker", "create", "--name", "sparkvm-build-rollout-x", "sparkvm-rollout:rollout-x"], run_checked_calls)
        self.assertIn(["docker", "export", "-o", str(self.home / "cache" / "rootfs-rollout-x.tar"), "sparkvm-build-rollout-x"], run_checked_calls)
        self.assertIn(["docker", "rm", "-f", "sparkvm-build-rollout-x"], run_checked_calls)
        self.assertEqual(self.image_path, built.path)

        metadata = json.loads(self.image_metadata_path.read_text(encoding="utf-8"))
        self.assertEqual("image-rollout-x", metadata["id"])
        self.assertEqual(str(self.image_path), metadata["rootfs_path"])
        self.assertEqual("echo hi", metadata["resolved_run_command"]["command"])
        images_metadata = json.loads((self.home / "images" / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual("image-rollout-x", images_metadata["images"][0]["id"])

    def test_build_failure_preserves_build_logs(self) -> None:
        def failing_subprocess_run(cmd, stdout=None, stderr=None, check=False):  # noqa: ANN001
            del stdout, check
            if stderr is not None:
                stderr.write(b"docker build failed\n")
            return subprocess.CompletedProcess(cmd, 1)

        with patch("sparkvm.image_builder.subprocess.run", side_effect=failing_subprocess_run):
            with self.assertRaises(RolloutBuildError):
                RolloutImageBuilder().build_from_dockerfile(
                    rollout_id="rollout-x",
                    source_dir=self.source,
                    dockerfile_path=self.dockerfile,
                    run_cmd=None,
                    disk_mb=4096,
                    image_path=self.image_path,
                    image_metadata_path=self.image_metadata_path,
                    build_dir=self.build_dir,
                )

        self.assertIn("docker build failed", (self.build_dir / "build.stderr.log").read_text(encoding="utf-8"))
        self.assertFalse(self.image_path.exists())


if __name__ == "__main__":
    unittest.main()
