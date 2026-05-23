from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm import SparkVM
from sparkvm.errors import RolloutNotFoundError
from sparkvm.rollouts import Rollouts


@unittest.skipUnless(
    os.getenv("SPARKVM_RUN_INTEGRATION") == "1",
    "Set SPARKVM_RUN_INTEGRATION=1 to run integration tests.",
)
class RolloutIntegrationTest(unittest.TestCase):
    def test_python_rollout_execution(self) -> None:
        home_dir = Path(os.getenv("SPARKVM_HOME", "~/.sparkvm")).expanduser()
        rollout_manager = Rollouts(home_dir=home_dir)
        repo_dir = home_dir / "itest-repo-simple"
        repo_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "sparkvm@example.com"], cwd=repo_dir, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "SparkVM Test"], cwd=repo_dir, check=True, capture_output=True, text=True)
        (repo_dir / "main.py").write_text("print('integration-ok')\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

        rollout = rollout_manager.create(
            name="integration-python",
            source=repo_dir,
            run_cmd="python3 main.py",
        )

        try:
            result = SparkVM(vcpu=1, memory="512M", timeout=30, home_dir=home_dir).run(rollout.id)
            self.assertEqual(result.exit_code, 0)
            self.assertIn("integration-ok", result.stdout)
            self.assertTrue(result.passed)
            self.assertEqual(result.rollout_id, rollout.id)
        finally:
            try:
                rollout_manager.delete_by_id(rollout.id)
            except RolloutNotFoundError:
                pass

    def test_rollout_list_get_delete_and_run_deleted_id(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-itest-rollout-") as tmp_home:
            home_dir = Path(tmp_home)
            rollout_manager = Rollouts(home_dir=home_dir)
            repo_dir = home_dir / "itest-repo-list"
            repo_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "sparkvm@example.com"], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "SparkVM Test"], cwd=repo_dir, check=True, capture_output=True, text=True)
            (repo_dir / "main.py").write_text("print('integration-list-get-delete')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

            rollout = rollout_manager.create(
                name="integration-list-get-delete",
                source=repo_dir,
                run_cmd="python3 main.py",
            )

            try:
                listed_rollouts = rollout_manager.list()
                self.assertTrue(any(item.id == rollout.id for item in listed_rollouts))

                fetched_rollout = rollout_manager.get_by_id(rollout.id)
                self.assertEqual(fetched_rollout.id, rollout.id)
                self.assertEqual(fetched_rollout.name, rollout.name)
                self.assertEqual(fetched_rollout.runtime, "python-3.12-slim")

                rollout_manager.delete_by_id(rollout.id)
                self.assertFalse(rollout_manager.exists(rollout.id))

                with self.assertRaises(RolloutNotFoundError):
                    rollout_manager.get_by_id(rollout.id)

                with self.assertRaises(RolloutNotFoundError):
                    SparkVM(vcpu=1, memory="512M", timeout=30, home_dir=home_dir).run(rollout.id)
            finally:
                if rollout_manager.exists(rollout.id):
                    rollout_manager.delete_by_id(rollout.id)

    @unittest.skipUnless(shutil.which("git") is not None, "git is required for repo integration test")
    def test_repo_rollout_execution_from_local_git_repo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-itest-repo-home-") as tmp_home, tempfile.TemporaryDirectory(
            prefix="sparkvm-itest-repo-src-"
        ) as repo_tmp:
            home_dir = Path(tmp_home)
            repo_dir = Path(repo_tmp)
            rollout_manager = Rollouts(home_dir=home_dir)

            subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "sparkvm@example.com"], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "SparkVM Test"], cwd=repo_dir, check=True, capture_output=True, text=True)
            (repo_dir / "main.py").write_text("print('repo-integration-ok')\n", encoding="utf-8")
            (repo_dir / "Dockerfile").write_text(
                "FROM alpine:latest\n"
                "WORKDIR /workspace\n"
                "COPY . .\n"
                'CMD ["sh", "-c", "echo hello-from-dockerfile"]\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

            rollout = rollout_manager.create(
                name="integration-repo",
                source=repo_dir,
                run_cmd="sh -c 'echo hello-from-repo'",
            )

            try:
                result = SparkVM(vcpu=1, memory="512M", timeout=30, home_dir=home_dir).run(rollout.id)
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(result.status, "passed")
                self.assertEqual(result.rollout_mode, "repo")
                self.assertIn("hello-from-dockerfile", result.stdout)
            finally:
                if rollout_manager.exists(rollout.id):
                    rollout_manager.delete_by_id(rollout.id)


if __name__ == "__main__":
    unittest.main()
