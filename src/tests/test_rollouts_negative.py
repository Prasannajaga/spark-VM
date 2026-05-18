from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.errors import InvalidRolloutModeError, RolloutError, RolloutMetadataError, RolloutNotFoundError
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

    def test_create_rejects_empty_files(self) -> None:
        with self.assertRaises(RolloutError):
            self.rollout.create(
                name="bad-empty-files",
                files={},
                run_cmd="python3 /job/main.py",
            )

    def test_create_rejects_invalid_runtime(self) -> None:
        with self.assertRaises(RolloutError):
            self.rollout.create(
                name="bad-image",
                runtime="   ",
                files={"main.py": "print('x')"},
                run_cmd="python3 /job/main.py",
            )

    def test_create_rejects_invalid_mode(self) -> None:
        with self.assertRaises(InvalidRolloutModeError):
            self.rollout.create(
                name="bad-mode",
                mode="container",
                files={"main.py": "print('x')"},
                run_cmd="python3 /job/main.py",
            )

    def test_create_rejects_invalid_name_and_command(self) -> None:
        with self.assertRaises(RolloutError):
            self.rollout.create(
                name="",
                files={"main.py": "print('x')"},
                run_cmd="python3 /job/main.py",
            )

        with self.assertRaises(RolloutError):
            self.rollout.create(
                name="bad-command",
                files={"main.py": "print('x')"},
                run_cmd="  ",
            )

    def test_create_rejects_invalid_rollout_paths(self) -> None:
        invalid_paths = [
            "/etc/passwd",
            "../escape.py",
            "a//b.py",
            "./main.py",
            "dir/../main.py",
            "dir/./main.py",
            " ",
        ]

        for path in invalid_paths:
            with self.subTest(path=path):
                with self.assertRaises(RolloutError):
                    self.rollout.create(
                        name="bad-path",
                        files={path: "print('x')"},
                        run_cmd="python3 /job/main.py",
                    )

    def test_create_rejects_invalid_file_content_type(self) -> None:
        with self.assertRaises(RolloutError):
            self.rollout.create(
                name="bad-content",
                files={"main.py": 123},  # type: ignore[arg-type]
                run_cmd="python3 /job/main.py",
            )

    def test_get_by_id_rejects_invalid_rollout_id_format(self) -> None:
        for rollout_id in ["", " ", "abc", "rollout with spaces", "rollout-"]:
            with self.subTest(rollout_id=rollout_id):
                with self.assertRaises(RolloutError):
                    self.rollout.get_by_id(rollout_id)

    def test_get_by_id_raises_when_rollout_directory_missing(self) -> None:
        self.write_metadata(
            {
                "version": 1,
                "rollouts": [
                    {
                        "id": "rollout-missing-dir",
                        "name": "x",
                        "runtime": "python-3.12-slim",
                        "path": str(self.home_dir / "rollouts" / "rollout-missing-dir"),
                        "command": "python3 /job/main.py",
                        "run_cmd": "python3 /job/main.py",
                        "files": ["main.py", "run.sh"],
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": None,
                    }
                ],
            }
        )

        with self.assertRaises(RolloutNotFoundError):
            self.rollout.get_by_id("rollout-missing-dir")

    def test_get_by_id_raises_when_rollout_json_missing(self) -> None:
        rollout_dir = self.home_dir / "rollouts" / "rollout-has-dir-no-json"
        rollout_dir.mkdir(parents=True, exist_ok=True)

        self.write_metadata(
            {
                "version": 1,
                "rollouts": [
                    {
                        "id": "rollout-has-dir-no-json",
                        "name": "x",
                        "runtime": "python-3.12-slim",
                        "path": str(rollout_dir),
                        "command": "python3 /job/main.py",
                        "run_cmd": "python3 /job/main.py",
                        "files": ["main.py", "run.sh"],
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": None,
                    }
                ],
            }
        )

        with self.assertRaises(RolloutNotFoundError):
            self.rollout.get_by_id("rollout-has-dir-no-json")

    def test_delete_by_id_not_found(self) -> None:
        with self.assertRaises(RolloutNotFoundError):
            self.rollout.delete_by_id("rollout-missing-123")

    def test_corrupt_metadata_json_raises_metadata_error(self) -> None:
        self._write_metadata_raw("{this is not valid json")

        with self.assertRaises(RolloutMetadataError):
            self.rollout.list()

    def test_metadata_must_be_object(self) -> None:
        self.write_metadata(["not", "an", "object"])

        with self.assertRaises(RolloutMetadataError):
            self.rollout.list()

    def test_metadata_version_and_rollouts_types(self) -> None:
        self.write_metadata({"version": "1", "rollouts": []})
        with self.assertRaises(RolloutMetadataError):
            self.rollout.list()

        self.write_metadata({"version": 1, "rollouts": "not-a-list"})
        with self.assertRaises(RolloutMetadataError):
            self.rollout.list()

    def test_list_raises_on_invalid_rollout_entry_shape(self) -> None:
        self.write_metadata({"version": 1, "rollouts": [{"id": "rollout-bad"}]})

        with self.assertRaises(RolloutMetadataError):
            self.rollout.list()

    def test_write_metadata_os_error_is_wrapped(self) -> None:
        with patch("sparkvm.rollouts.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(RolloutMetadataError):
                self.rollout.write_metadata({"version": 1, "rollouts": []})


if __name__ == "__main__":
    unittest.main()
