from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.errors import InvalidRepoError, RolloutConfigError, RolloutMetadataError, RolloutNotFoundError
from sparkvm.rollouts import Rollouts


class RolloutNegativeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-rollout-negative-")
        self.home_dir = Path(self.tmp.name)
        self.rollout = Rollouts(home_dir=self.home_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_metadata_raw(self, payload: str) -> None:
        self.rollout.rollouts_dir.mkdir(parents=True, exist_ok=True)
        self.rollout.metadata_path.write_text(payload, encoding="utf-8")

    def write_metadata(self, obj: object) -> None:
        self._write_metadata_raw(json.dumps(obj))

    def test_create_rejects_missing_source_and_run_cmd(self) -> None:
        with self.assertRaises(TypeError):
            self.rollout.create(name="x", run_cmd="echo hi")  # type: ignore[call-arg]

        with self.assertRaises(TypeError):
            self.rollout.create(name="x", source="/tmp")  # type: ignore[call-arg]

    def test_create_rejects_unsupported_args(self) -> None:
        with self.assertRaises(RolloutConfigError):
            self.rollout.create(
                name="legacy",
                source="https://github.com/org/repo.git",
                run_cmd="pytest -q",
                files={"main.py": "print('x')"},
            )

    def test_create_rejects_non_git_local_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sparkvm-non-git-") as tmp_dir:
            src = Path(tmp_dir)
            with self.assertRaises(InvalidRepoError):
                self.rollout.create(name="repo", source=src, run_cmd="python3 main.py")

    def test_get_by_id_raises_when_rollout_directory_missing(self) -> None:
        self.write_metadata(
            {
                "version": 1,
                "rollouts": [
                    {
                        "id": "rollout-missing-dir",
                        "name": "x",
                        "mode": "repo",
                        "runtime": "python-3.12-slim",
                        "path": str(self.home_dir / "rollouts" / "rollout-missing-dir"),
                        "run_cmd": "python3 /job/source/main.py",
                        "files": ["source/", "run.sh"],
                        "deleteOnSuccess": False,
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": None,
                    }
                ],
            }
        )

        with self.assertRaises(RolloutNotFoundError):
            self.rollout.get_by_id("rollout-missing-dir")

    def test_corrupt_metadata_json_raises_metadata_error(self) -> None:
        self._write_metadata_raw("{this is not valid json")

        with self.assertRaises(RolloutMetadataError):
            self.rollout.list()

    def test_write_metadata_os_error_is_wrapped(self) -> None:
        with patch("sparkvm.rollouts.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(RolloutMetadataError):
                self.rollout.write_metadata({"version": 1, "rollouts": []})


if __name__ == "__main__":
    unittest.main()
