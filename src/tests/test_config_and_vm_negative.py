from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import MethodType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.config import build_config, parse_memory_to_mib, resolve_home_dir
from sparkvm.errors import InvalidMemoryError, InvalidResourceError, RolloutNotFoundError, RuntimeImageNotFound, SparkVMConfigError
from sparkvm.rollouts import Rollout
from sparkvm.vm import RunConfig, SparkVM


class ConfigNegativeTest(unittest.TestCase):
    def test_parse_memory_rejects_bool_non_numeric_and_non_positive(self) -> None:
        bad_values = [True, "bad", 0, -1, "0", "-1", object()]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(InvalidMemoryError):
                    parse_memory_to_mib(value)  # type: ignore[arg-type]

    def test_build_config_rejects_invalid_vcpu_timeout_and_runtime(self) -> None:
        with self.assertRaises(InvalidResourceError):
            build_config(vcpu=0, memory="512M", timeout=30, runtime="python-3.12-slim", home_dir=None)

        with self.assertRaises(InvalidResourceError):
            build_config(vcpu=1, memory="512M", timeout=True, runtime="python-3.12-slim", home_dir=None)

        with self.assertRaises(InvalidResourceError):
            build_config(vcpu=1, memory="512M", timeout=0, runtime="python-3.12-slim", home_dir=None)

        with self.assertRaises(SparkVMConfigError):
            build_config(vcpu=1, memory="512M", timeout=30, runtime="   ", home_dir=None)

    def test_resolve_home_dir_prefers_sudo_invoking_user_home_when_root(self) -> None:
        fake_pw = type("P", (), {"pw_dir": "/home/prasanna"})()
        with unittest.mock.patch(
            "sparkvm.config.os.getenv",
            side_effect=lambda key, default=None: {"SPARKVM_HOME": "", "SUDO_USER": "prasanna"}.get(key, default),
        ), unittest.mock.patch("sparkvm.config.os.geteuid", return_value=0), unittest.mock.patch(
            "sparkvm.config.pwd.getpwnam", return_value=fake_pw
        ):
            resolved = resolve_home_dir(None)
        self.assertEqual(Path("/home/prasanna/.sparkvm"), resolved)


class SparkVMNegativeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdirs: list[tempfile.TemporaryDirectory[str]] = []

    def tearDown(self) -> None:
        for tmp in self._tmpdirs:
            tmp.cleanup()

    def _new_temp_path(self, *, prefix: str) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix=prefix)
        self._tmpdirs.append(tmp)
        path = Path(tmp.name)
        (path / "config.json").write_text(json.dumps({"resource_policy": {"max_vm_cpu_percent": 100, "max_vm_memory_percent": 100, "max_vm_disk_percent": 100, "min_host_cpu_percent": 0, "min_host_memory_percent": 0, "min_host_disk_percent": 0}}), encoding="utf-8")
        return path

    def test_run_rejects_invalid_rollout_type(self) -> None:
        vm = SparkVM(home_dir=self._new_temp_path(prefix="sparkvm-vm-negative-"))
        with self.assertRaises(TypeError):
            vm.run(123)  # type: ignore[arg-type]

    def test_run_rollout_id_not_found_raises(self) -> None:
        vm = SparkVM(home_dir=self._new_temp_path(prefix="sparkvm-vm-negative-"))
        with self.assertRaises(RolloutNotFoundError):
            vm.run("rollout-does-not-exist")

    def test_run_uses_run_config_runtime(self) -> None:
        vm = SparkVM(home_dir=self._new_temp_path(prefix="sparkvm-vm-negative-"))
        rollout_dir = self._new_temp_path(prefix="sparkvm-rollout-runtime-")
        (rollout_dir / "rollout.json").write_text("{}", encoding="utf-8")

        captured: dict[str, str] = {}

        vm._setup.ensure_layout()
        vm._setup.firecracker_binary_path = MethodType(lambda _self: Path("/fake/firecracker"), vm._setup)
        vm._setup.assert_kvm_available = MethodType(lambda _self: None, vm._setup)

        def _fake_resolve(_self, runtime: str | None = None):
            captured["runtime"] = str(runtime)
            raise RuntimeImageNotFound("missing")

        vm._images.resolve = MethodType(_fake_resolve, vm._images)
        rollout = Rollout(
            id="rollout-runtime-uses-config",
            name="runtime-from-config",
            mode="repo",
            path=rollout_dir,
            command="python3 /job/source/main.py",
            setup_cmd=None,
            run_cmd="python3 /job/source/main.py",
            disk_mb=1024,
            files=["run.sh"],
            created_at="2026-01-01T00:00:00Z",
            runtime="python-3.12-slim",
        )
        vm._rollouts.get_by_id = MethodType(lambda _self, _rid: rollout, vm._rollouts)

        with self.assertRaises(RuntimeImageNotFound):
            vm.run(
                rollout.id,
                config=RunConfig(runtime="ubuntu-24.04"),
            )
        self.assertEqual("ubuntu-24.04", captured["runtime"])


if __name__ == "__main__":
    unittest.main()
