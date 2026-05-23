from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import MethodType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.errors import FirecrackerBinaryNotInstalled
from sparkvm.rollouts import Rollout
from sparkvm.vm import SparkVM, render_env_file, shell_quote


class EnvValidationAndRenderingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-env-phase5-")
        self.home = Path(self.tmp.name)
        (self.home / "config.json").write_text(json.dumps({"resource_policy": {"max_vm_cpu_percent": 100, "max_vm_memory_percent": 100, "max_vm_disk_percent": 100, "min_host_cpu_percent": 0, "min_host_memory_percent": 0, "min_host_disk_percent": 0}}), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_valid_env_accepted_invalid_env_rejected(self) -> None:
        SparkVM(home_dir=self.home, env={"OPENAI_API_KEY": "abc", "A1_B2": "ok"})

        with self.assertRaises(ValueError):
            SparkVM(home_dir=self.home, env={"1BAD": "x"})
        with self.assertRaises(ValueError):
            SparkVM(home_dir=self.home, env={"": "x"})
        with self.assertRaises(TypeError):
            SparkVM(home_dir=self.home, env={"GOOD": 1})  # type: ignore[arg-type]

    def test_shell_quote_and_render_env_file(self) -> None:
        self.assertEqual("'a b'", shell_quote("a b"))
        rendered = render_env_file({"A": "a b", "B": "x'\"y"})
        self.assertIn("export A='a b'", rendered)
        self.assertIn("export B='x'\"'\"'\"y'", rendered)


class RuntimeFileAndFailureScrubTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkvm-env-phase5-run-")
        self.home = Path(self.tmp.name)
        (self.home / "config.json").write_text(json.dumps({"resource_policy": {"max_vm_cpu_percent": 100, "max_vm_memory_percent": 100, "max_vm_disk_percent": 100, "min_host_cpu_percent": 0, "min_host_memory_percent": 0, "min_host_disk_percent": 0}}), encoding="utf-8")
        self.rollout_dir = self.home / "rollout"
        self.rollout_dir.mkdir(parents=True, exist_ok=True)
        (self.rollout_dir / "rollout.json").write_text("{}\n", encoding="utf-8")
        self.rollout = Rollout(
            id="rollout-env-phase5",
            name="env-phase5",
            mode="repo",
            runtime="python-3.12-slim",
            path=self.rollout_dir,
            command="python3 /job/source/main.py",
            setup_cmd="echo setup",
            run_cmd="python3 /job/source/main.py",
            disk_mb=1024,
            files=["run.sh", "rollout.json"],
            created_at="2026-01-01T00:00:00Z",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_runtime_execution_files_include_runtime_env_and_setup_switch(self) -> None:
        vm = SparkVM(home_dir=self.home, env={"OPENAI_API_KEY": "sk-123"}, network=True)
        files = vm.runtime_execution_files(rollout=self.rollout, network_config=None)

        self.assertIn(".sparkvm/runtime.env", files)
        runtime_env = files[".sparkvm/runtime.env"]
        self.assertIn("SPARKVM_SETUP_TIMEOUT_SEC=300", runtime_env)
        self.assertIn("SPARKVM_RUN_TIMEOUT_SEC=300", runtime_env)
        self.assertIn("SPARKVM_RUN_SETUP_IN_GUEST=1", runtime_env)

    def test_failure_json_contains_only_env_keys(self) -> None:
        vm = SparkVM(home_dir=self.home, env={"OPENAI_API_KEY": "super-secret-token"})
        vm._rollouts.get_by_id = MethodType(lambda _self, _rid: self.rollout, vm._rollouts)
        vm._setup.ensure_layout()

        vm._setup.firecracker_binary_path = MethodType(
            lambda _self: (_ for _ in ()).throw(FirecrackerBinaryNotInstalled("missing")),
            vm._setup,
        )

        with self.assertRaises(FirecrackerBinaryNotInstalled):
            vm.run(self.rollout.id)

        worker = next((self.home / "workers").glob("vm-*"))
        failure_path = worker / "failure.json"
        payload = json.loads(failure_path.read_text(encoding="utf-8"))

        self.assertEqual(["OPENAI_API_KEY"], payload["env_keys"])
        self.assertFalse(payload["env_values_stored"])
        text = failure_path.read_text(encoding="utf-8")
        self.assertNotIn("super-secret-token", text)


if __name__ == "__main__":
    unittest.main()
