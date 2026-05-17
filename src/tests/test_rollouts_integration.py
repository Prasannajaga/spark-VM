from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm import SparkVM
from sparkvm.errors import RolloutNotFoundError
from sparkvm.rollouts import Rollout


@unittest.skipUnless(
    os.getenv("SPARKVM_RUN_INTEGRATION") == "1",
    "Set SPARKVM_RUN_INTEGRATION=1 to run integration tests.",
)
class RolloutIntegrationTest(unittest.TestCase):
    def test_python_rollout_execution(self) -> None:
        home_dir = Path(os.getenv("SPARKVM_HOME", "~/.sparkvm")).expanduser()
        rollout_manager = Rollout(home_dir=home_dir)

        rollout = rollout_manager.create(
            name="integration-python",
            runtime="python-3.12",
            files={"main.py": "print('integration-ok')"},
            command="python3 /job/main.py",
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
            rollout_manager = Rollout(home_dir=home_dir)

            rollout = rollout_manager.create(
                name="integration-list-get-delete",
                runtime="python-3.12",
                files={"main.py": "print('integration-list-get-delete')"},
                command="python3 /job/main.py",
            )

            try:
                listed_rollouts = rollout_manager.list()
                self.assertTrue(any(item.id == rollout.id for item in listed_rollouts))

                fetched_rollout = rollout_manager.get_by_id(rollout.id)
                self.assertEqual(fetched_rollout.id, rollout.id)
                self.assertEqual(fetched_rollout.name, rollout.name)
                self.assertEqual(fetched_rollout.runtime, "python-3.12")

                rollout_manager.delete_by_id(rollout.id)
                self.assertFalse(rollout_manager.exists(rollout.id))

                with self.assertRaises(RolloutNotFoundError):
                    rollout_manager.get_by_id(rollout.id)

                with self.assertRaises(RolloutNotFoundError):
                    SparkVM(vcpu=1, memory="512M", timeout=30, home_dir=home_dir).run(rollout.id)
            finally:
                if rollout_manager.exists(rollout.id):
                    rollout_manager.delete_by_id(rollout.id)


if __name__ == "__main__":
    unittest.main()
