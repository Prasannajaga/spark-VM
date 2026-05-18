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

from cli.runtimes import run_dockify_command, run_runtimes_list_command


class DockifyCommandTest(unittest.TestCase):
    def test_dockify_builds_expected_paths_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-dockify-") as tmp:
            home = Path(tmp)
            calls: list[list[str]] = []

            def _fake_run_checked(cmd: list[str], *, cwd: Path | None = None):
                del cwd
                calls.append(list(cmd))
                if cmd[:2] == ["docker", "create"]:
                    class _Result:
                        stdout = "cid123\n"

                    return _Result()
                if cmd and cmd[0] == "dd":
                    image_path = Path(next(x for x in cmd if x.startswith("of=")).split("=", 1)[1])
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    image_path.write_bytes(b"disk")
                return type("_Result", (), {"stdout": ""})()

            def _fake_export(_container_id: str, rootfs_dir: Path) -> None:
                (rootfs_dir / "bin").mkdir(parents=True, exist_ok=True)
                (rootfs_dir / "usr/bin").mkdir(parents=True, exist_ok=True)
                (rootfs_dir / "bin" / "sh").write_text("", encoding="utf-8")
                (rootfs_dir / "usr/bin" / "mount").write_text("", encoding="utf-8")

            with patch("cli.runtimes.shutil.which", return_value="/usr/bin/docker"), patch(
                "cli.runtimes._run_checked", side_effect=_fake_run_checked
            ), patch(
                "cli.runtimes._run_docker_export", side_effect=_fake_export
            ):
                run_dockify_command(
                    str(home),
                    "python:3.12-slim",
                    name=None,
                    size_mb=2048,
                    force=False,
                    pull=True,
                    owner=None,
                )

            rootfs = home / "images" / "python-3.12-slim.ext4"
            metadata = home / "images" / "python-3.12-slim.json"
            self.assertTrue(rootfs.exists())
            self.assertTrue(metadata.exists())

            payload = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual("python-3.12-slim", payload["runtime"])
            self.assertEqual("python:3.12-slim", payload["source_image"])
            self.assertEqual(2048, payload["size_mb"])
            self.assertTrue(payload["init_injected"])
            self.assertEqual(str(rootfs), payload["rootfs"])

            self.assertIn(["docker", "pull", "python:3.12-slim"], calls)
            self.assertIn(["docker", "create", "python:3.12-slim"], calls)
            self.assertTrue(any(cmd[:2] == ["mkfs.ext4", "-d"] for cmd in calls))
            self.assertIn(["docker", "rm", "-f", "cid123"], calls)

    def test_dockify_cleans_up_outputs_on_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-dockify-") as tmp:
            home = Path(tmp)
            calls: list[list[str]] = []

            def _fake_run_checked(cmd: list[str], *, cwd: Path | None = None):
                del cwd
                calls.append(list(cmd))
                if cmd[:2] == ["docker", "create"]:
                    return type("_Result", (), {"stdout": "cid123\n"})()
                if cmd and cmd[0] == "dd":
                    image_path = Path(next(x for x in cmd if x.startswith("of=")).split("=", 1)[1])
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    image_path.write_bytes(b"disk")
                if cmd[:2] == ["mkfs.ext4", "-d"]:
                    raise RuntimeError("mkfs -d failed")
                return type("_Result", (), {"stdout": ""})()

            def _fake_export(_container_id: str, rootfs_dir: Path) -> None:
                (rootfs_dir / "bin").mkdir(parents=True, exist_ok=True)
                (rootfs_dir / "usr/bin").mkdir(parents=True, exist_ok=True)
                (rootfs_dir / "bin" / "sh").write_text("", encoding="utf-8")
                (rootfs_dir / "usr/bin" / "mount").write_text("", encoding="utf-8")

            with patch("cli.runtimes.shutil.which", return_value="/usr/bin/docker"), patch(
                "cli.runtimes._run_checked", side_effect=_fake_run_checked
            ), patch(
                "cli.runtimes._run_docker_export", side_effect=_fake_export
            ):
                with self.assertRaises(RuntimeError):
                    run_dockify_command(
                        str(home),
                        "python:3.12-slim",
                        name=None,
                        size_mb=2048,
                        force=False,
                        pull=False,
                        owner=None,
                    )

            self.assertFalse((home / "images" / "python-3.12-slim.ext4").exists())
            self.assertFalse((home / "images" / "python-3.12-slim.json").exists())
            self.assertIn(["docker", "rm", "-f", "cid123"], calls)


class RuntimeListTest(unittest.TestCase):
    def test_runtimes_list_reads_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-runtimes-list-") as tmp:
            home = Path(tmp)
            images = home / "images"
            images.mkdir(parents=True, exist_ok=True)

            payload = {
                "runtime": "python-3.12-slim",
                "source_image": "python:3.12-slim",
                "rootfs": str(images / "python-3.12-slim.ext4"),
                "size_mb": 2048,
                "created_at": "2026-05-18T12:00:00Z",
                "init_injected": True,
            }
            (images / "python-3.12-slim.ext4").write_bytes(b"disk")
            (images / "python-3.12-slim.json").write_text(json.dumps(payload), encoding="utf-8")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                run_runtimes_list_command(str(home))

            text = out.getvalue()
            self.assertIn("python-3.12-slim", text)
            self.assertIn("python:3.12-slim", text)


if __name__ == "__main__":
    unittest.main()
