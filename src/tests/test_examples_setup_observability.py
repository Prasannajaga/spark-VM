from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ExamplesSetupObservabilityTest(unittest.TestCase):
    def test_github_example_uses_bounded_network_debug_commands(self) -> None:
        example_path = Path(__file__).resolve().parents[2] / "examples" / "githubExample.py"
        text = example_path.read_text(encoding="utf-8")

        self.assertIn("set -eux", text)
        self.assertIn('echo "[net] dns"', text)
        self.assertIn("timeout 5 getent hosts pypi.org || true", text)
        self.assertIn("curl -Iv --connect-timeout 5 --max-time 10 https://pypi.org/simple/ || true", text)
        self.assertIn("timeout=60.0", text)

    def test_github_example_includes_network_preflight(self) -> None:
        example_path = Path(__file__).resolve().parents[2] / "examples" / "githubExample.py"
        text = example_path.read_text(encoding="utf-8")

        self.assertIn("ip addr || true", text)
        self.assertIn("ip route || true", text)
        self.assertIn("cat /etc/resolv.conf || true", text)
        self.assertIn("timeout 5 getent hosts pypi.org || true", text)
        self.assertIn("curl -Iv --connect-timeout 5 --max-time 10 https://pypi.org/simple/ || true", text)


if __name__ == "__main__":
    unittest.main()
