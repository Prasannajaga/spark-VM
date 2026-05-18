from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.runtimes.debian import SPARKVM_INIT_TEMPLATE


class InitTemplatePhase5Test(unittest.TestCase):
    def test_template_contains_network_env_sourcing(self) -> None:
        self.assertIn("/job/.sparkvm/network.env", SPARKVM_INIT_TEMPLATE)
        self.assertIn("ip link set eth0 up", SPARKVM_INIT_TEMPLATE)
        self.assertIn("ip route add default via \"$SPARKVM_HOST_IP\" dev eth0", SPARKVM_INIT_TEMPLATE)

    def test_template_contains_env_sh_sourcing(self) -> None:
        self.assertIn("/job/.sparkvm/env.sh", SPARKVM_INIT_TEMPLATE)
        self.assertIn("set -a", SPARKVM_INIT_TEMPLATE)
        self.assertIn("set +a", SPARKVM_INIT_TEMPLATE)

    def test_template_contains_tmpfs_mounts(self) -> None:
        self.assertIn("mount -t tmpfs tmpfs /tmp", SPARKVM_INIT_TEMPLATE)
        self.assertIn("mount -t tmpfs tmpfs /run", SPARKVM_INIT_TEMPLATE)
        self.assertIn("mount -t tmpfs tmpfs /var/tmp", SPARKVM_INIT_TEMPLATE)

    def test_template_contains_shutdown_fallback(self) -> None:
        self.assertIn("if command -v poweroff", SPARKVM_INIT_TEMPLATE)
        self.assertIn("if command -v halt", SPARKVM_INIT_TEMPLATE)
        self.assertIn("if command -v reboot", SPARKVM_INIT_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
