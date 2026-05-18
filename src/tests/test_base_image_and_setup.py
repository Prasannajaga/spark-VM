from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli import setup as setup_mod
from sparkvm.config import DEFAULT_BASE_IMAGE, build_config
from sparkvm.errors import BaseImageNotFound, SparkVMSetupError
from sparkvm.image import resolve_base_image
from sparkvm.rollouts import Rollouts


class BaseImageResolverTest(unittest.TestCase):
    def test_resolve_base_image_returns_managed_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-base-image-") as tmp:
            home = Path(tmp)
            image_dir = home / "images"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "vmlinux").write_text("kernel\n", encoding="utf-8")
            (image_dir / "debian-rootfs.ext4").write_text("rootfs\n", encoding="utf-8")

            config = build_config(
                vcpu=1,
                memory="512M",
                timeout=30,
                base_image=DEFAULT_BASE_IMAGE,
                home_dir=home,
            )
            image = resolve_base_image("debian-minbase", config)

            self.assertEqual("debian-minbase", image.name)
            self.assertEqual(image_dir / "vmlinux", image.kernel_image)
            self.assertEqual(image_dir / "debian-rootfs.ext4", image.rootfs_image)

    def test_resolve_base_image_missing_rootfs_raises(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-base-image-") as tmp:
            home = Path(tmp)
            image_dir = home / "images"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "vmlinux").write_text("kernel\n", encoding="utf-8")

            config = build_config(
                vcpu=1,
                memory="512M",
                timeout=30,
                base_image=DEFAULT_BASE_IMAGE,
                home_dir=home,
            )

            with self.assertRaises(BaseImageNotFound):
                resolve_base_image("debian-minbase", config)


class SetupBuilderTest(unittest.TestCase):
    def test_build_debian_rootfs_non_root_fails_fast_with_message(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-setup-builder-") as tmp:
            out = Path(tmp) / "images" / "debian-rootfs.ext4"
            with patch("cli.setup.os.geteuid", return_value=1000):
                with self.assertRaises(SparkVMSetupError) as ctx:
                    setup_mod.build_debian_minbase_rootfs(output_path=out, force=True)
            self.assertIn("requires root privileges", str(ctx.exception))

    def test_build_debian_rootfs_invokes_debootstrap_minbase(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-setup-builder-") as tmp:
            out = Path(tmp) / "images" / "debian-rootfs.ext4"
            calls: list[list[str]] = []

            def _fake_run(cmd: list[str], *, cwd: Path | None = None):
                del cwd
                calls.append(list(cmd))
                if "debootstrap" in cmd:
                    rootfs_dir = Path(cmd[-2])
                    (rootfs_dir / "bin").mkdir(parents=True, exist_ok=True)
                    (rootfs_dir / "sbin").mkdir(parents=True, exist_ok=True)
                    (rootfs_dir / "bin" / "sh").write_text("", encoding="utf-8")
                    (rootfs_dir / "bin" / "mount").write_text("", encoding="utf-8")
                    (rootfs_dir / "sbin" / "poweroff").write_text("", encoding="utf-8")
                if cmd and cmd[0] == "dd":
                    of_arg = next(item for item in cmd if item.startswith("of="))
                    image_path = Path(of_arg.split("=", 1)[1])
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    image_path.write_bytes(b"img")
                if cmd and cmd[0] == "mount":
                    Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
                if cmd and cmd[0] == "cp":
                    src = Path(cmd[-2][:-2])
                    dst = Path(cmd[-1])
                    shutil.copytree(src, dst, dirs_exist_ok=True)

            with patch("cli.setup.os.geteuid", return_value=0), patch("cli.setup._run_checked", side_effect=_fake_run):
                result = setup_mod.build_debian_minbase_rootfs(output_path=out, force=True)

            self.assertEqual(out, result)
            self.assertTrue(out.exists())

            flattened = [" ".join(cmd) for cmd in calls]
            self.assertTrue(any("debootstrap --variant=minbase" in line for line in flattened))
            self.assertFalse(any(cmd and cmd[0] == "sudo" for cmd in calls))

    def test_build_debian_rootfs_attempts_unmount_on_copy_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-setup-builder-") as tmp:
            out = Path(tmp) / "images" / "debian-rootfs.ext4"
            calls: list[list[str]] = []

            def _fake_run(cmd: list[str], *, cwd: Path | None = None):
                del cwd
                calls.append(list(cmd))
                if "debootstrap" in cmd:
                    rootfs_dir = Path(cmd[-2])
                    (rootfs_dir / "bin").mkdir(parents=True, exist_ok=True)
                    (rootfs_dir / "sbin").mkdir(parents=True, exist_ok=True)
                    (rootfs_dir / "bin" / "sh").write_text("", encoding="utf-8")
                    (rootfs_dir / "bin" / "mount").write_text("", encoding="utf-8")
                    (rootfs_dir / "sbin" / "poweroff").write_text("", encoding="utf-8")
                if cmd and cmd[0] == "dd":
                    of_arg = next(item for item in cmd if item.startswith("of="))
                    image_path = Path(of_arg.split("=", 1)[1])
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    image_path.write_bytes(b"img")
                if cmd and cmd[0] == "cp":
                    raise SparkVMSetupError("copy failed")

            with patch("cli.setup.os.geteuid", return_value=0), patch("cli.setup._run_checked", side_effect=_fake_run):
                with self.assertRaises(SparkVMSetupError):
                    setup_mod.build_debian_minbase_rootfs(output_path=out, force=True)

            self.assertTrue(any(cmd and cmd[0] == "umount" for cmd in calls))


class RolloutBaseImageDefaultsTest(unittest.TestCase):
    def test_rollout_create_defaults_base_image(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-rollout-base-image-") as tmp:
            rollouts = Rollouts(home_dir=Path(tmp))
            rollout = rollouts.create(
                name="default-base-image",
                mode="script",
                files={"main.sh": "echo hi\n"},
                run_cmd="sh /job/main.sh",
            )
            self.assertEqual("debian-minbase", rollout.base_image)

            payload = (rollout.path / "rollout.json").read_text(encoding="utf-8")
            self.assertIn('"base_image": "debian-minbase"', payload)

    def test_script_setup_sh_written_only_when_setup_cmd_provided(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-rollout-setup-script-") as tmp:
            rollouts = Rollouts(home_dir=Path(tmp))
            without_setup = rollouts.create(
                name="no-setup",
                mode="script",
                files={"main.sh": "echo hi\n"},
                run_cmd="sh /job/main.sh",
            )
            self.assertFalse((without_setup.path / "setup.sh").exists())

            with_setup = rollouts.create(
                name="with-setup",
                mode="script",
                files={"main.sh": "echo hi\n"},
                setup_cmd="echo setup",
                run_cmd="sh /job/main.sh",
            )
            self.assertTrue((with_setup.path / "setup.sh").exists())


class SetupCommandRootAndOwnerTest(unittest.TestCase):
    def test_setup_command_non_root_shows_sudo_recommendation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-setup-cmd-") as tmp_home:
            home = Path(tmp_home)
            with patch("cli.setup.os.geteuid", return_value=1000), patch("cli.setup.pwd.getpwuid") as mock_pw:
                mock_pw.return_value.pw_name = "prasanna"
                with self.assertRaises(SparkVMSetupError) as ctx:
                    setup_mod.run_setup_command(str(home), runtime=None, force=False, owner=None)
                msg = str(ctx.exception)
                self.assertIn("sudo sparkvm setup --home-dir", msg)
                self.assertIn("--owner prasanna", msg)

    def test_setup_command_owner_triggers_chown_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-setup-owner-") as tmp_home:
            home = Path(tmp_home)

            with patch("cli.setup.os.geteuid", return_value=0), patch("cli.setup.check_linux_host"), patch(
                "cli.setup.normalize_arch", return_value="x86_64"
            ), patch("cli.setup.check_kvm_access"), patch("cli.setup.require_host_tools"), patch(
                "cli.setup.ensure_firecracker_binary", return_value=home / "bin" / "firecracker"
            ), patch("cli.setup.ensure_kernel_image", return_value=home / "images" / "vmlinux"), patch(
                "cli.setup.build_debian_minbase_rootfs", return_value=home / "images" / "debian-rootfs.ext4"
            ), patch("cli.setup._initialize_rollouts_metadata"), patch("cli.setup.chown_tree") as mock_chown:
                setup_mod.run_setup_command(str(home), runtime=None, force=False, owner="prasanna")

            mock_chown.assert_called_once()

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_repo_setup_sh_written_only_when_setup_cmd_provided(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-rollout-repo-") as tmp_home, tempfile.TemporaryDirectory(
            prefix="sparkvm-rollout-repo-src-"
        ) as tmp_repo:
            home = Path(tmp_home)
            repo = Path(tmp_repo)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "sparkvm@example.com"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "SparkVM Test"], cwd=repo, check=True, capture_output=True, text=True)
            (repo / "main.sh").write_text("echo repo\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)

            rollouts = Rollouts(home_dir=home)
            without_setup = rollouts.create(
                name="repo-no-setup",
                mode="repo",
                source=repo,
                run_cmd="sh main.sh",
            )
            self.assertFalse((without_setup.path / "setup.sh").exists())

            with_setup = rollouts.create(
                name="repo-with-setup",
                mode="repo",
                source=repo,
                setup_cmd="echo setup",
                run_cmd="sh main.sh",
            )
            self.assertTrue((with_setup.path / "setup.sh").exists())


if __name__ == "__main__":
    unittest.main()
