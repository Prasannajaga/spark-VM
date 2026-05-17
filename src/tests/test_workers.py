from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.errors import WorkerMetadataError, WorkerNotFoundError
from sparkvm.workers import Workers


class WorkersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-workers-test-")
        self.home = Path(self.tmp.name)
        self.workers = Workers(home_dir=self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_worker(
        self,
        vm_id: str,
        *,
        with_failure_json: bool = True,
        log_text: str = "line1\nline2\nline3\n",
    ) -> Path:
        worker_dir = self.home / "workers" / vm_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        (worker_dir / "firecracker.log").write_text(log_text, encoding="utf-8")
        if with_failure_json:
            payload = {
                "vm_id": vm_id,
                "rollout_id": "rollout-example-1",
                "rollout_name": "example",
                "runtime": "python-3.12",
                "status": "failed",
                "error_type": "FirecrackerBootError",
                "error_message": "boom",
                "duration_ms": 42,
                "firecracker_log_path": str(worker_dir / "firecracker.log"),
                "execution_disk_path": str(worker_dir / "rollout.ext4"),
                "created_at": "2026-05-17T12:00:00Z",
            }
            (worker_dir / "failure.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return worker_dir

    def test_list_with_failure_json(self) -> None:
        self._create_worker("vm-abc123", with_failure_json=True)
        items = self.workers.list()
        self.assertEqual(1, len(items))
        item = items[0]
        self.assertEqual("vm-abc123", item.vm_id)
        self.assertEqual("failed", item.status)
        self.assertEqual("FirecrackerBootError", item.error_type)

    def test_list_without_failure_json_status_unknown(self) -> None:
        self._create_worker("vm-abc123", with_failure_json=False)
        items = self.workers.list()
        self.assertEqual(1, len(items))
        self.assertEqual("unknown", items[0].status)

    def test_get_by_id(self) -> None:
        self._create_worker("vm-abc123", with_failure_json=True)
        item = self.workers.get_by_id("vm-abc123")
        self.assertEqual("vm-abc123", item.vm_id)

    def test_get_by_id_not_found(self) -> None:
        with self.assertRaises(WorkerNotFoundError):
            self.workers.get_by_id("vm-missing123")

    def test_log_text(self) -> None:
        self._create_worker("vm-abc123", with_failure_json=True, log_text="a\nb\nc\n")
        text = self.workers.log_text("vm-abc123")
        self.assertIn("a", text)
        self.assertIn("c", text)

    def test_log_text_tail(self) -> None:
        self._create_worker("vm-abc123", with_failure_json=True, log_text="a\nb\nc\n")
        text = self.workers.log_text("vm-abc123", tail=2)
        self.assertEqual("b\nc", text)

    def test_failure_json(self) -> None:
        self._create_worker("vm-abc123", with_failure_json=True)
        payload = self.workers.failure_json("vm-abc123")
        self.assertEqual("vm-abc123", payload["vm_id"])

    def test_failure_json_missing_raises(self) -> None:
        self._create_worker("vm-abc123", with_failure_json=False)
        with self.assertRaises(WorkerMetadataError):
            self.workers.failure_json("vm-abc123")

    def test_delete_by_id(self) -> None:
        worker_dir = self._create_worker("vm-abc123", with_failure_json=True)
        self.workers.delete_by_id("vm-abc123", force=True)
        self.assertFalse(worker_dir.exists())


if __name__ == "__main__":
    unittest.main()
