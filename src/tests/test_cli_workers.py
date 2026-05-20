from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.cli import main as cli_main


class CLIWorkersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-cli-workers-test-")
        self.home = Path(self.tmp.name)
        self.worker_dir = self.home / "workers" / "vm-abc123"
        self.worker_dir.mkdir(parents=True, exist_ok=True)
        (self.worker_dir / "firecracker.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
        payload = {
            "vm_id": "vm-abc123",
            "rollout_id": "rollout-xyz",
            "rollout_name": "demo",
            "runtime": "python-3.12",
            "status": "failed",
            "error_type": "FirecrackerBootError",
            "error_message": "boom",
            "duration_ms": 10,
            "firecracker_log_path": str(self.worker_dir / "firecracker.log"),
            "execution_disk_path": str(self.worker_dir / "rollout.ext4"),
            "created_at": "2026-05-17T12:00:00Z",
        }
        (self.worker_dir / "failure.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        self.result_worker_dir = self.home / "workers" / "vm-def456"
        self.result_worker_dir.mkdir(parents=True, exist_ok=True)
        (self.result_worker_dir / "firecracker.log").write_text("rc-log\n", encoding="utf-8")
        (self.result_worker_dir / "results").mkdir(parents=True, exist_ok=True)
        (self.result_worker_dir / "results" / "run.stdout.log").write_text("hello\n", encoding="utf-8")
        result_payload = {
            "vm_id": "vm-def456",
            "rollout_id": "rollout-r1",
            "rollout_name": "demo-result",
            "runtime": "python-3.12",
            "status": "run_failed",
            "exit_code": 1,
            "duration_ms": 12,
            "firecracker_log_path": str(self.result_worker_dir / "firecracker.log"),
            "execution_disk_path": str(self.result_worker_dir / "rollout.ext4"),
            "created_at": "2026-05-18T12:00:00Z",
        }
        (self.result_worker_dir / "result.json").write_text(json.dumps(result_payload, indent=2) + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = cli_main(argv)
        return code, stream.getvalue()

    def _run_cli_with_stderr(self, argv: list[str]) -> tuple[int, str, str]:
        out_stream = io.StringIO()
        err_stream = io.StringIO()
        with redirect_stdout(out_stream), redirect_stderr(err_stream):
            code = cli_main(argv)
        return code, out_stream.getvalue(), err_stream.getvalue()

    def test_workers_list(self) -> None:
        code, out = self._run_cli(["--home-dir", str(self.home), "workers", "list"])
        self.assertEqual(0, code)
        self.assertIn("vm-abc123", out)
        self.assertIn("vm-def456", out)
        self.assertIn("FirecrackerBootError", out)

    def test_workers_view_default_log(self) -> None:
        code, out = self._run_cli(["--home-dir", str(self.home), "workers", "view", "vm-abc123"])
        self.assertEqual(0, code)
        self.assertIn("line1", out)
        self.assertIn("line3", out)

    def test_workers_view_tail(self) -> None:
        code, out = self._run_cli(["--home-dir", str(self.home), "workers", "view", "vm-abc123", "--tail", "2"])
        self.assertEqual(0, code)
        self.assertNotIn("line1", out)
        self.assertIn("line2", out)
        self.assertIn("line3", out)

    def test_workers_view_failure(self) -> None:
        code, out = self._run_cli(["--home-dir", str(self.home), "workers", "view", "vm-abc123", "--failure"])
        self.assertEqual(0, code)
        self.assertIn('"vm_id": "vm-abc123"', out)

    def test_workers_view_path(self) -> None:
        code, out = self._run_cli(["--home-dir", str(self.home), "workers", "view", "vm-abc123", "--path"])
        self.assertEqual(0, code)
        self.assertIn(str(self.worker_dir), out)

    def test_workers_view_result(self) -> None:
        code, out = self._run_cli(["--home-dir", str(self.home), "workers", "view", "vm-def456", "--result"])
        self.assertEqual(0, code)
        self.assertIn('"vm_id": "vm-def456"', out)
        self.assertIn('"status": "run_failed"', out)

    def test_workers_view_results(self) -> None:
        code, out = self._run_cli(["--home-dir", str(self.home), "workers", "view", "vm-def456", "--results"])
        self.assertEqual(0, code)
        self.assertIn("run.stdout.log", out)
        self.assertIn("hello", out)

    def test_workers_view_live_streams_log_chunks(self) -> None:
        chunks = iter(["lineA\n", "lineB\n"])

        def _fake_stream(_self, vm_id: str, *, tail: int | None = None, poll_interval_sec: float = 0.2):
            del tail, poll_interval_sec
            self.assertEqual("vm-abc123", vm_id)
            for chunk in chunks:
                yield chunk

        with patch("sparkvm.workers.Workers.stream_log", _fake_stream):
            code, out = self._run_cli(["--home-dir", str(self.home), "workers", "view", "vm-abc123", "--live"])
        self.assertEqual(0, code)
        self.assertIn("lineA", out)
        self.assertIn("lineB", out)

    def test_workers_view_live_rejects_conflicting_flags(self) -> None:
        code, _out, err = self._run_cli_with_stderr(
            ["--home-dir", str(self.home), "workers", "view", "vm-abc123", "--live", "--result"]
        )
        self.assertEqual(1, code)
        self.assertIn("--live can only be used with default log view", err)

    def test_workers_delete_force(self) -> None:
        code, _ = self._run_cli(["--home-dir", str(self.home), "workers", "delete", "vm-abc123", "--force"])
        self.assertEqual(0, code)
        self.assertFalse(self.worker_dir.exists())


if __name__ == "__main__":
    unittest.main()
