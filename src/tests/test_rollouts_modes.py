from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.errors import InvalidRepoError, RolloutConfigError, RolloutError
from sparkvm.rollouts import Rollouts, resolve_container_command


class RolloutsModesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-rollouts-modes-")
        self.home = Path(self.tmp.name)
        self.rollouts = Rollouts(home_dir=self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _read_json(self, path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _create_local_git_repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
        repo_tmp = tempfile.TemporaryDirectory(prefix="sparkvm-local-repo-")
        repo = Path(repo_tmp.name)
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "sparkvm@example.com"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "SparkVM Test"], cwd=repo, check=True, capture_output=True, text=True)
        (repo / "main.py").write_text("print('repo')\n", encoding="utf-8")
        (repo / "README.md").write_text("repo\n", encoding="utf-8")
        (repo / "Dockerfile").write_text(
            "FROM python:3.12-slim\n"
            "WORKDIR /workspace\n"
            "COPY . .\n"
            'CMD ["python", "main.py"]\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return repo_tmp, repo, commit

    def _fake_docker_run_checked_factory(
        self,
        *,
        working_dir: str = "/workspace",
        entrypoint: list[str] | None = None,
        cmd: list[str] | None = None,
    ):
        calls: list[list[str]] = []

        def _fake_run_checked(cmd_args, *, error_factory, cwd=None):  # noqa: ANN001
            del error_factory, cwd
            cmd_list = [str(x) for x in cmd_args]
            calls.append(cmd_list)
            if cmd_list[:3] == ["docker", "image", "inspect"]:
                payload = [{"Config": {"WorkingDir": working_dir, "Entrypoint": entrypoint, "Cmd": cmd, "Env": ["A=B"]}}]
                return subprocess.CompletedProcess(cmd_list, 0, stdout=json.dumps(payload), stderr="")
            if cmd_list[:2] == ["docker", "create"]:
                return subprocess.CompletedProcess(cmd_list, 0, stdout="container-123\n", stderr="")
            return subprocess.CompletedProcess(cmd_list, 0, stdout="", stderr="")

        return _fake_run_checked, calls

    def test_script_rollout_create_writes_files_and_metadata(self) -> None:
        rollout = self.rollouts.create(
            name="hello-script",
            mode="script",
            files={"main.py": "print('hello')\n"},
            run_cmd="python3 /job/main.py",
        )

        self.assertEqual("script", rollout.mode)
        self.assertEqual("python-3.12-slim", rollout.runtime)
        self.assertEqual("python3 /job/main.py", rollout.command)
        self.assertEqual("python3 /job/main.py", rollout.run_cmd)
        self.assertEqual(1024, rollout.disk_mb)

        rollout_dir = self.home / "rollouts" / rollout.id
        self.assertTrue((rollout_dir / "main.py").exists())
        self.assertTrue((rollout_dir / "run.sh").exists())
        self.assertTrue((rollout_dir / "rollout.json").exists())

        run_sh = (rollout_dir / "run.sh").read_text(encoding="utf-8")
        self.assertIn("cd /job", run_sh)
        self.assertIn("python3 /job/main.py", run_sh)

        payload = self._read_json(rollout_dir / "rollout.json")
        self.assertEqual("script", payload["mode"])
        self.assertEqual("python-3.12-slim", payload["runtime"])
        self.assertEqual("python3 /job/main.py", payload["command"])
        self.assertEqual(1024, payload["disk_mb"])

        metadata = self._read_json(self.home / "rollouts" / "metadata.json")
        self.assertEqual(1, metadata["version"])
        entries = metadata["rollouts"]
        self.assertEqual(1, len(entries))
        self.assertEqual("script", entries[0]["mode"])
        self.assertEqual("python-3.12-slim", entries[0]["runtime"])

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_repo_rollout_local_path_copies_repo_and_writes_scripts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-local-repo-") as repo_tmp:
            repo = Path(repo_tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "sparkvm@example.com"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "SparkVM Test"], cwd=repo, check=True, capture_output=True, text=True)
            (repo / "main.py").write_text("print('repo')\n", encoding="utf-8")
            (repo / "README.md").write_text("repo\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            rollout = self.rollouts.create(
                name="repo-test",
                mode="repo",
                source=repo,
                setup_cmd="pip install -e .",
                run_cmd="pytest -q",
            )

        rollout_dir = self.home / "rollouts" / rollout.id
        copied_repo = rollout_dir / "repo"
        self.assertTrue(copied_repo.exists())
        self.assertTrue((copied_repo / "main.py").exists())
        self.assertFalse((copied_repo / ".git").exists())

        setup_sh = (rollout_dir / "setup.sh").read_text(encoding="utf-8")
        run_sh = (rollout_dir / "run.sh").read_text(encoding="utf-8")
        self.assertIn("cd /job/repo", setup_sh)
        self.assertIn("pip install -e .", setup_sh)
        self.assertIn("cd /job/repo", run_sh)
        self.assertIn("pytest -q", run_sh)

        rollout_payload = self._read_json(rollout_dir / "rollout.json")
        self.assertEqual("repo", rollout_payload["mode"])
        self.assertEqual("python-3.12-slim", rollout_payload["runtime"])
        self.assertEqual("local", rollout_payload["source"]["type"])
        self.assertEqual(commit, rollout_payload["source"]["commit"])
        self.assertEqual("pip install -e .", rollout_payload["setup_cmd"])
        self.assertEqual("pytest -q", rollout_payload["run_cmd"])
        self.assertEqual(4096, rollout_payload["disk_mb"])

    def test_repo_rollout_invalid_local_path_raises(self) -> None:
        with self.assertRaises(InvalidRepoError):
            self.rollouts.create(
                name="repo-missing",
                mode="repo",
                source=self.home / "does-not-exist",
                run_cmd="python3 main.py",
            )

    def test_repo_rollout_local_path_without_git_raises(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-non-git-") as tmp_dir:
            src = Path(tmp_dir)
            (src / "main.py").write_text("print('x')\n", encoding="utf-8")

            with self.assertRaises(InvalidRepoError) as ctx:
                self.rollouts.create(
                    name="repo-no-git",
                    mode="repo",
                    source=src,
                    run_cmd="python3 main.py",
                )

            self.assertIn(".git directory", str(ctx.exception))

    def test_repo_rollout_git_url_executes_expected_git_commands(self) -> None:
        calls: list[tuple[list[str], str | None]] = []

        def _fake_run_checked(cmd: list[str], *, error_factory, cwd=None):  # noqa: ANN001
            del error_factory
            calls.append((list(cmd), str(cwd) if cwd is not None else None))
            if cmd[:2] == ["git", "clone"]:
                target = Path(cmd[3])
                target.mkdir(parents=True, exist_ok=True)
                (target / ".git").mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[:2] == ["git", "checkout"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[:3] == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
            raise AssertionError(f"Unexpected command: {cmd}")

        with patch("sparkvm.rollouts.run_checked", side_effect=_fake_run_checked):
            rollout = self.rollouts.create(
                name="repo-url",
                mode="repo",
                source="https://github.com/org/repo.git",
                ref="main",
                run_cmd="python3 main.py",
            )

        rollout_dir = self.home / "rollouts" / rollout.id
        repo_dir = rollout_dir / "repo"
        source_dir = rollout_dir / "source"
        self.assertFalse((repo_dir / ".git").exists())

        commands = [cmd for cmd, _cwd in calls]
        self.assertIn(["git", "clone", "https://github.com/org/repo.git", str(source_dir)], commands)
        self.assertIn(["git", "checkout", "main"], commands)
        self.assertIn(["git", "rev-parse", "HEAD"], commands)

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_dockerfile_mode_does_not_require_setup_cmd_or_run_cmd_if_cmd_exists(self) -> None:
        repo_tmp, repo, _commit = self._create_local_git_repo()
        self.addCleanup(repo_tmp.cleanup)
        fake_run_checked, calls = self._fake_docker_run_checked_factory(
            working_dir="/workspace",
            entrypoint=None,
            cmd=["python", "main.py"],
        )
        with patch("sparkvm.rollouts.run_checked", side_effect=fake_run_checked):
            rollout = self.rollouts.create(
                name="dockerfile-default-cmd",
                mode="repo",
                source=repo,
                dockerfile="Dockerfile",
            )

        payload = self._read_json(self.home / "rollouts" / rollout.id / "rollout.json")
        self.assertEqual("docker_config", payload["resolved_run_command"]["source"])
        self.assertIsNone(payload["run_cmd"])
        self.assertIn(["docker", "build", "-f", str(self.home / "rollouts" / rollout.id / "source" / "Dockerfile"), "-t", f"sparkvm-rollout:{rollout.id}", str(self.home / "rollouts" / rollout.id / "source")], calls)

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_dockerfile_mode_accepts_external_dockerfile_path(self) -> None:
        repo_tmp, repo, _commit = self._create_local_git_repo()
        self.addCleanup(repo_tmp.cleanup)
        external_tmp = tempfile.TemporaryDirectory(prefix="sparkvm-external-dockerfile-")
        self.addCleanup(external_tmp.cleanup)
        external_dockerfile = Path(external_tmp.name) / "Dockerfile.ext"
        external_dockerfile.write_text(
            "FROM python:3.12-slim\n"
            "WORKDIR /workspace\n"
            "COPY . .\n"
            'CMD ["python", "main.py"]\n',
            encoding="utf-8",
        )
        fake_run_checked, calls = self._fake_docker_run_checked_factory(
            working_dir="/workspace",
            entrypoint=None,
            cmd=["python", "main.py"],
        )

        with patch("sparkvm.rollouts.run_checked", side_effect=fake_run_checked):
            rollout = self.rollouts.create(
                name="dockerfile-external-path",
                mode="repo",
                source=repo,
                dockerfile=str(external_dockerfile),
            )

        self.assertIn(
            [
                "docker",
                "build",
                "-f",
                str(external_dockerfile.resolve()),
                "-t",
                f"sparkvm-rollout:{rollout.id}",
                str(self.home / "rollouts" / rollout.id / "source"),
            ],
            calls,
        )

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_dockerfile_mode_raises_if_no_cmd_entrypoint_and_no_run_cmd(self) -> None:
        repo_tmp, repo, _commit = self._create_local_git_repo()
        self.addCleanup(repo_tmp.cleanup)
        fake_run_checked, _calls = self._fake_docker_run_checked_factory(
            working_dir="/workspace",
            entrypoint=None,
            cmd=None,
        )
        with patch("sparkvm.rollouts.run_checked", side_effect=fake_run_checked):
            with self.assertRaises(RolloutConfigError):
                self.rollouts.create(
                    name="dockerfile-missing-command",
                    mode="repo",
                    source=repo,
                    dockerfile="Dockerfile",
                )

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_run_cmd_overrides_dockerfile_cmd(self) -> None:
        repo_tmp, repo, _commit = self._create_local_git_repo()
        self.addCleanup(repo_tmp.cleanup)
        fake_run_checked, _calls = self._fake_docker_run_checked_factory(
            working_dir="/workspace",
            entrypoint=["python"],
            cmd=["main.py"],
        )
        with patch("sparkvm.rollouts.run_checked", side_effect=fake_run_checked):
            rollout = self.rollouts.create(
                name="dockerfile-override",
                mode="repo",
                source=repo,
                dockerfile="Dockerfile",
                run_cmd="pytest -q",
            )
        payload = self._read_json(self.home / "rollouts" / rollout.id / "rollout.json")
        self.assertEqual("run_cmd", payload["resolved_run_command"]["source"])
        run_sh = (self.home / "rollouts" / rollout.id / "run.sh").read_text(encoding="utf-8")
        self.assertIn("exec pytest -q", run_sh)

    def test_resolve_container_command_uses_workdir_and_combines_entrypoint_cmd(self) -> None:
        resolved = resolve_container_command(
            run_cmd=None,
            docker_entrypoint=["python", "-m"],
            docker_cmd=["pytest", "-q"],
            working_dir="/repo",
        )
        self.assertEqual("docker_config", resolved.source)
        self.assertEqual("/repo", resolved.working_dir)
        self.assertEqual("python -m pytest -q", resolved.command)

    def test_resolve_container_command_shell_form_is_preserved(self) -> None:
        resolved = resolve_container_command(
            run_cmd=None,
            docker_entrypoint=None,
            docker_cmd="pytest -q tests/test_api.py",
            working_dir=None,
        )
        self.assertEqual("/workspace", resolved.working_dir)
        self.assertEqual("pytest -q tests/test_api.py", resolved.command)

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_image_mode_copies_source_to_workspace(self) -> None:
        repo_tmp, repo, _commit = self._create_local_git_repo()
        self.addCleanup(repo_tmp.cleanup)
        fake_run_checked, _calls = self._fake_docker_run_checked_factory(
            working_dir="/workspace",
            entrypoint=None,
            cmd=["python", "main.py"],
        )
        with patch("sparkvm.rollouts.run_checked", side_effect=fake_run_checked):
            rollout = self.rollouts.create(
                name="image-mode",
                mode="repo",
                source=repo,
                image="python:3.12-slim",
            )
        rollout_dir = self.home / "rollouts" / rollout.id
        self.assertTrue((rollout_dir / "workspace" / "main.py").exists())

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_dockerfile_mode_does_not_force_workspace_copy(self) -> None:
        repo_tmp, repo, _commit = self._create_local_git_repo()
        self.addCleanup(repo_tmp.cleanup)
        fake_run_checked, _calls = self._fake_docker_run_checked_factory(
            working_dir="/workspace",
            entrypoint=None,
            cmd=["python", "main.py"],
        )
        with patch("sparkvm.rollouts.run_checked", side_effect=fake_run_checked):
            rollout = self.rollouts.create(
                name="dockerfile-no-workspace-copy",
                mode="repo",
                source=repo,
                dockerfile="Dockerfile",
            )
        rollout_dir = self.home / "rollouts" / rollout.id
        self.assertFalse((rollout_dir / "workspace").exists())

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_setup_cmd_in_dockerfile_mode_runs_after_build_when_provided(self) -> None:
        repo_tmp, repo, _commit = self._create_local_git_repo()
        self.addCleanup(repo_tmp.cleanup)
        fake_run_checked, calls = self._fake_docker_run_checked_factory(
            working_dir="/workspace",
            entrypoint=None,
            cmd=["python", "main.py"],
        )
        with patch("sparkvm.rollouts.run_checked", side_effect=fake_run_checked):
            self.rollouts.create(
                name="dockerfile-setup-advanced",
                mode="repo",
                source=repo,
                dockerfile="Dockerfile",
                setup_cmd="pip install -e .",
            )
        docker_exec_calls = [call for call in calls if call[:2] == ["docker", "exec"]]
        self.assertEqual(1, len(docker_exec_calls))

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_rollout_json_stores_resolved_run_command(self) -> None:
        repo_tmp, repo, _commit = self._create_local_git_repo()
        self.addCleanup(repo_tmp.cleanup)
        fake_run_checked, _calls = self._fake_docker_run_checked_factory(
            working_dir="/workspace",
            entrypoint=["python"],
            cmd=["-m", "uvicorn", "src.main:app"],
        )
        with patch("sparkvm.rollouts.run_checked", side_effect=fake_run_checked):
            rollout = self.rollouts.create(
                name="dockerfile-resolved-command",
                mode="repo",
                source=repo,
                dockerfile="Dockerfile",
            )
        payload = self._read_json(self.home / "rollouts" / rollout.id / "rollout.json")
        self.assertIn("resolved_run_command", payload)
        self.assertEqual("docker_config", payload["resolved_run_command"]["source"])

    def test_unsafe_script_file_paths_raise(self) -> None:
        with self.assertRaises(RolloutError):
            self.rollouts.create(
                name="bad-abs",
                mode="script",
                files={"/tmp/a.py": "print('x')"},
                run_cmd="python3 /job/a.py",
            )

        with self.assertRaises(RolloutError):
            self.rollouts.create(
                name="bad-dotdot",
                mode="script",
                files={"../a.py": "print('x')"},
                run_cmd="python3 /job/a.py",
            )

    def test_create_same_name_replaces_existing_rollout(self) -> None:
        first = self.rollouts.create(
            name="same-name",
            mode="script",
            files={"main.py": "print('first')\n"},
            run_cmd="python3 /job/main.py",
        )
        first_path = self.home / "rollouts" / first.id
        self.assertTrue(first_path.exists())

        second = self.rollouts.create(
            name="same-name",
            mode="script",
            files={"main.py": "print('second')\n"},
            run_cmd="python3 /job/main.py",
        )
        second_path = self.home / "rollouts" / second.id

        self.assertNotEqual(first.id, second.id)
        self.assertFalse(first_path.exists())
        self.assertTrue(second_path.exists())

        metadata = self._read_json(self.home / "rollouts" / "metadata.json")
        entries = metadata["rollouts"]
        self.assertEqual(1, len(entries))
        self.assertEqual(second.id, entries[0]["id"])
        self.assertEqual("same-name", entries[0]["name"])


if __name__ == "__main__":
    unittest.main()
