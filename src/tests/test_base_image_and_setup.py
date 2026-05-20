from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli import setup as setup_mod
from sparkvm.config import build_config
from sparkvm.errors import KernelImageNotFound, RuntimeImageNotFound
from sparkvm.image import normalize_runtime_name, resolve_runtime_image


class RuntimeNameTest(unittest.TestCase):
    def test_normalize_runtime_name_basic(self) -> None:
        self.assertEqual("python-3.12-slim", normalize_runtime_name("python:3.12-slim"))
        self.assertEqual("node-22-slim", normalize_runtime_name("node:22-slim"))
        self.assertEqual("ubuntu-24.04", normalize_runtime_name("ubuntu:24.04"))

    def test_normalize_runtime_name_ghcr(self) -> None:
        self.assertEqual(
            "ghcr.io-org-image-tag",
            normalize_runtime_name("ghcr.io/org/image:tag"),
        )


class RuntimeImageResolverTest(unittest.TestCase):
    def test_resolve_runtime_image_accepts_normalized_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-runtime-image-") as tmp:
            home = Path(tmp)
            image_dir = home / "images"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "vmlinux").write_text("kernel\n", encoding="utf-8")
            (image_dir / "python-3.12-slim.ext4").write_text("rootfs\n", encoding="utf-8")

            config = build_config(
                vcpu=1,
                memory="512M",
                timeout=30,
                runtime="python-3.12-slim",
                home_dir=home,
            )
            image = resolve_runtime_image("python-3.12-slim", config)

            self.assertEqual("python-3.12-slim", image.name)
            self.assertEqual(image_dir / "vmlinux", image.kernel_image)
            self.assertEqual(image_dir / "python-3.12-slim.ext4", image.rootfs_image)
            self.assertEqual(image_dir / "python-3.12-slim.json", image.metadata_path)

    def test_resolve_runtime_image_accepts_docker_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-runtime-image-") as tmp:
            home = Path(tmp)
            image_dir = home / "images"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "vmlinux").write_text("kernel\n", encoding="utf-8")
            (image_dir / "python-3.12-slim.ext4").write_text("rootfs\n", encoding="utf-8")

            config = build_config(
                vcpu=1,
                memory="512M",
                timeout=30,
                runtime="python-3.12-slim",
                home_dir=home,
            )
            image = resolve_runtime_image("python:3.12-slim", config)
            self.assertEqual("python-3.12-slim", image.name)

    def test_resolve_runtime_image_missing_runtime_raises_with_rollout_hint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-runtime-image-") as tmp:
            home = Path(tmp)
            image_dir = home / "images"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "vmlinux").write_text("kernel\n", encoding="utf-8")

            config = build_config(
                vcpu=1,
                memory="512M",
                timeout=30,
                runtime="python-3.12-slim",
                home_dir=home,
            )

            with self.assertRaises(RuntimeImageNotFound) as ctx:
                resolve_runtime_image("python:3.12-slim", config)
            self.assertIn("Create a repo rollout with dockerfile support", str(ctx.exception))

    def test_resolve_runtime_image_missing_kernel_raises(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-runtime-image-") as tmp:
            home = Path(tmp)
            image_dir = home / "images"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "python-3.12-slim.ext4").write_text("rootfs\n", encoding="utf-8")

            config = build_config(
                vcpu=1,
                memory="512M",
                timeout=30,
                runtime="python-3.12-slim",
                home_dir=home,
            )

            with self.assertRaises(KernelImageNotFound):
                resolve_runtime_image("python-3.12-slim", config)


class SetupCommandTest(unittest.TestCase):
    def test_setup_initializes_metadata_and_not_debian_rootfs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-setup-") as tmp_home:
            home = Path(tmp_home)
            paths = setup_mod.get_sparkvm_paths(home)

            def _fake_firecracker(*args, **kwargs):
                del args, kwargs
                paths.firecracker_bin.parent.mkdir(parents=True, exist_ok=True)
                paths.firecracker_bin.write_text("#!/bin/sh\n", encoding="utf-8")
                paths.firecracker_bin.chmod(0o755)
                return paths.firecracker_bin

            def _fake_kernel(*args, **kwargs):
                del args, kwargs
                paths.kernel_image.parent.mkdir(parents=True, exist_ok=True)
                paths.kernel_image.write_text("kernel\n", encoding="utf-8")
                return paths.kernel_image

            with patch("cli.setup.check_linux_host"), patch("cli.setup.normalize_arch", return_value="x86_64"), patch(
                "cli.setup.require_setup_tools"
            ), patch("cli.setup.ensure_firecracker_binary", side_effect=_fake_firecracker), patch(
                "cli.setup.ensure_kernel_image", side_effect=_fake_kernel
            ):
                setup_mod.run_setup(paths, force=False, owner=None)

            metadata_path = home / "rollouts" / "metadata.json"
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual([], payload["rollouts"])
            self.assertFalse((home / "images" / "debian-rootfs.ext4").exists())

    def test_setup_command_runs_without_runtime_argument(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-setup-cmd-") as tmp_home:
            home = Path(tmp_home)
            buffer = io.StringIO()
            with patch("cli.setup.run_setup"), contextlib.redirect_stdout(buffer):
                setup_mod.run_setup_command(str(home), force=False, owner=None)
            out = buffer.getvalue()
            self.assertIn("Using SparkVM home:", out)


if __name__ == "__main__":
    unittest.main()
