from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.runtimes.debian import SPARKVM_INIT_TEMPLATE


class InitTemplatePhase5Test(unittest.TestCase):
    def test_template_contains_universal_bootstrap_functions(self) -> None:
        self.assertIn("prepare_linux_runtime()", SPARKVM_INIT_TEMPLATE)
        self.assertIn("mount_job_disk()", SPARKVM_INIT_TEMPLATE)
        self.assertIn("configure_network()", SPARKVM_INIT_TEMPLATE)
        self.assertIn("load_runtime_env()", SPARKVM_INIT_TEMPLATE)
        self.assertIn("run_phase()", SPARKVM_INIT_TEMPLATE)

    def test_template_contains_network_env_sourcing(self) -> None:
        self.assertIn("/job/.sparkvm/network.env", SPARKVM_INIT_TEMPLATE)
        self.assertIn("ip link set eth0 up", SPARKVM_INIT_TEMPLATE)
        self.assertIn("ip route add default via \"$SPARKVM_HOST_IP\" dev eth0", SPARKVM_INIT_TEMPLATE)

    def test_template_contains_env_sh_sourcing(self) -> None:
        self.assertIn("/job/.sparkvm/runtime.env", SPARKVM_INIT_TEMPLATE)
        self.assertIn("SPARKVM_SETUP_TIMEOUT_SEC", SPARKVM_INIT_TEMPLATE)
        self.assertIn("SPARKVM_RUN_TIMEOUT_SEC", SPARKVM_INIT_TEMPLATE)
        self.assertIn("/job/.sparkvm/env.sh", SPARKVM_INIT_TEMPLATE)
        self.assertIn("set -a", SPARKVM_INIT_TEMPLATE)
        self.assertIn("set +a", SPARKVM_INIT_TEMPLATE)

    def test_template_contains_tmpfs_mounts(self) -> None:
        self.assertIn("mountpoint -q /proc || mount -t proc proc /proc", SPARKVM_INIT_TEMPLATE)
        self.assertIn("mountpoint -q /sys || mount -t sysfs sysfs /sys", SPARKVM_INIT_TEMPLATE)
        self.assertIn("mountpoint -q /dev || mount -t devtmpfs devtmpfs /dev", SPARKVM_INIT_TEMPLATE)
        self.assertIn("mountpoint -q /dev/pts || mount -t devpts devpts /dev/pts", SPARKVM_INIT_TEMPLATE)
        self.assertIn("mount -t tmpfs tmpfs /tmp", SPARKVM_INIT_TEMPLATE)
        self.assertIn("mount -t tmpfs tmpfs /run", SPARKVM_INIT_TEMPLATE)
        self.assertIn("mount -t tmpfs tmpfs /var/tmp", SPARKVM_INIT_TEMPLATE)
        self.assertIn("ln -sf /proc/self/fd /dev/fd", SPARKVM_INIT_TEMPLATE)
        self.assertIn("ln -sf /proc/self/fd/0 /dev/stdin", SPARKVM_INIT_TEMPLATE)
        self.assertIn("ln -sf /proc/self/fd/1 /dev/stdout", SPARKVM_INIT_TEMPLATE)
        self.assertIn("ln -sf /proc/self/fd/2 /dev/stderr", SPARKVM_INIT_TEMPLATE)

    def test_template_contains_shutdown_fallback(self) -> None:
        self.assertIn("if command -v poweroff", SPARKVM_INIT_TEMPLATE)
        self.assertIn("if command -v halt", SPARKVM_INIT_TEMPLATE)
        self.assertIn("if command -v reboot", SPARKVM_INIT_TEMPLATE)

    def test_template_runs_rollout_shell_scripts_generically(self) -> None:
        self.assertIn('sh "$script"', SPARKVM_INIT_TEMPLATE)
        self.assertIn('run_phase "setup" "/job/setup.sh"', SPARKVM_INIT_TEMPLATE)
        self.assertIn('run_phase "run" "/job/run.sh"', SPARKVM_INIT_TEMPLATE)
        self.assertIn('SPARKVM_RUN_SETUP_IN_GUEST:-0', SPARKVM_INIT_TEMPLATE)
        self.assertIn('run_phase "setup" "/job/setup.sh" "${SPARKVM_SETUP_TIMEOUT_SEC:-300}"', SPARKVM_INIT_TEMPLATE)
        self.assertIn('run_phase "run" "/job/run.sh" "${SPARKVM_RUN_TIMEOUT_SEC:-300}"', SPARKVM_INIT_TEMPLATE)
        self.assertIn('timeout "$timeout_sec" sh "$script"', SPARKVM_INIT_TEMPLATE)
        self.assertIn("timeout command missing; phase timeout disabled", SPARKVM_INIT_TEMPLATE)
        self.assertIn("export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", SPARKVM_INIT_TEMPLATE)
        self.assertNotIn("python3 ", SPARKVM_INIT_TEMPLATE)
        self.assertNotIn("python ", SPARKVM_INIT_TEMPLATE)
        self.assertNotIn("node ", SPARKVM_INIT_TEMPLATE)
        self.assertNotIn("pip ", SPARKVM_INIT_TEMPLATE)
        self.assertNotIn("go ", SPARKVM_INIT_TEMPLATE)
        self.assertNotIn("cargo ", SPARKVM_INIT_TEMPLATE)
        self.assertNotIn("java ", SPARKVM_INIT_TEMPLATE)

    def test_template_collects_network_diagnostics(self) -> None:
        self.assertIn('echo "SparkVM: network diagnostics begin" > /dev/console', SPARKVM_INIT_TEMPLATE)
        self.assertIn("ip addr > /dev/console 2>&1 || true", SPARKVM_INIT_TEMPLATE)
        self.assertIn("ip route > /dev/console 2>&1 || true", SPARKVM_INIT_TEMPLATE)
        self.assertIn("cat /etc/resolv.conf > /dev/console 2>&1 || true", SPARKVM_INIT_TEMPLATE)
        self.assertIn("/job/results/network.stdout.log", SPARKVM_INIT_TEMPLATE)
        self.assertIn("/job/results/network.stderr.log", SPARKVM_INIT_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
