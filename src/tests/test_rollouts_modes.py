from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.errors import InvalidRepoError, RolloutConfigError
from sparkvm.rollouts import Rollouts


class RolloutsRepoOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-rollouts-repo-only-")
        self.home = Path(self.tmp.name)
        self.rollouts = Rollouts(home_dir=self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_local_git_repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
        repo_tmp = tempfile.TemporaryDirectory(prefix="sparkvm-local-repo-")
        repo = Path(repo_tmp.name)
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "sparkvm@example.com"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "SparkVM Test"], cwd=repo, check=True, capture_output=True, text=True)
        (repo / "main.py").write_text("print('repo')\n", encoding="utf-8")
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

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_create_repo_rollout_from_local_source(self) -> None:
        repo_tmp, repo, commit = self._create_local_git_repo()
        self.addCleanup(repo_tmp.cleanup)

        rollout = self.rollouts.create(
            name="version-3",
            source=repo,
            run_cmd="python3 main.py",
            delete_on_success=False,
        )

        self.assertEqual("repo", rollout.mode)
        self.assertEqual("python3 main.py", rollout.run_cmd)
        self.assertFalse(rollout.delete_on_success)
        rollout_dir = self.home / "rollouts" / rollout.id
        payload = json.loads((rollout_dir / "rollout.json").read_text(encoding="utf-8"))
        self.assertEqual("repo", payload["mode"])
        self.assertEqual(False, payload["deleteOnSuccess"])
        self.assertEqual("local", payload["source"]["type"])
        self.assertEqual(commit, payload["source"]["commit"])
        self.assertTrue((rollout_dir / "source" / "main.py").exists())
        self.assertFalse((rollout_dir / "source" / ".git").exists())

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_create_repo_rollout_requires_git_directory_for_local_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-non-git-") as tmp_dir:
            src = Path(tmp_dir)
            (src / "main.py").write_text("print('x')\n", encoding="utf-8")

            with self.assertRaises(InvalidRepoError) as ctx:
                self.rollouts.create(
                    name="repo-no-git",
                    source=src,
                    run_cmd="python3 main.py",
                )

            self.assertIn(".git directory", str(ctx.exception))

    @unittest.skipUnless(shutil.which("git") is not None, "git is required")
    def test_unsupported_legacy_args_are_rejected(self) -> None:
        repo_tmp, repo, _commit = self._create_local_git_repo()
        self.addCleanup(repo_tmp.cleanup)
        with self.assertRaises(RolloutConfigError):
            self.rollouts.create(
                name="legacy-mode",
                source=repo,
                run_cmd="python3 main.py",
                mode="repo",
            )


if __name__ == "__main__":
    unittest.main()
