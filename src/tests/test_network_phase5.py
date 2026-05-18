from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkvm.errors import CleanupError
from sparkvm.firecracker.api import FirecrackerAPIClient
from sparkvm.network import NetworkManager, detect_default_iface, render_network_env_file


class NetworkPhase5Test(unittest.TestCase):
    def test_detect_default_iface_parses_ip_route_get_output(self) -> None:
        iface = detect_default_iface("1.1.1.1 via 10.0.2.2 dev eth0 src 10.0.2.15 uid 1000")
        self.assertEqual("eth0", iface)

    def test_setup_builds_expected_commands_and_tap_name(self) -> None:
        manager = NetworkManager(home_dir=Path("/tmp"))
        commands: list[list[str]] = []

        class _CP:
            def __init__(self, stdout: str = "") -> None:
                self.stdout = stdout

        def _fake_run_raw(cmd: list[str]):
            commands.append(list(cmd))
            if cmd[:4] == ["ip", "route", "get", "1.1.1.1"]:
                return _CP("1.1.1.1 via 192.168.1.1 dev wlp2s0 src 192.168.1.22")
            return _CP("")

        with patch("sparkvm.network._has_network_privileges", return_value=True), patch.object(
            manager, "_run_raw", side_effect=_fake_run_raw
        ):
            config = manager.setup("vm-02e67edfc7a0")

        self.assertLessEqual(len(config.tap_name), 15)
        self.assertTrue(config.tap_name.startswith("spk"))
        self.assertEqual("wlp2s0", config.out_iface)
        self.assertEqual("1", config.host_ip.split(".")[3])
        self.assertEqual("2", config.guest_ip.split(".")[3])

        self.assertIn(["ip", "tuntap", "add", "dev", config.tap_name, "mode", "tap"], commands)
        self.assertIn(["ip", "addr", "add", f"{config.host_ip}/30", "dev", config.tap_name], commands)
        self.assertIn(["sysctl", "-w", "net.ipv4.ip_forward=1"], commands)

    def test_cleanup_builds_reverse_delete_commands(self) -> None:
        manager = NetworkManager(home_dir=Path("/tmp"))
        calls: list[list[str]] = []

        def _capture(cmd: list[str]) -> None:
            calls.append(list(cmd))

        with patch.object(manager, "_run_checked", side_effect=_capture):
            cfg = manager.setup.__globals__["_build_network_config"](vm_id="vm-abcd1234", out_iface="eth0")
            manager.cleanup(cfg)

        self.assertIn(["ip", "link", "delete", cfg.tap_name], calls)
        self.assertIn(
            [
                "iptables",
                "-t",
                "nat",
                "-D",
                "POSTROUTING",
                "-s",
                cfg.subnet_cidr,
                "-o",
                "eth0",
                "-j",
                "MASQUERADE",
            ],
            calls,
        )

    def test_cleanup_raises_cleanup_error_when_command_fails(self) -> None:
        manager = NetworkManager(home_dir=Path("/tmp"))

        def _fail(_cmd: list[str]) -> None:
            raise Exception("boom")

        with patch.object(manager, "_run_checked", side_effect=_fail):
            cfg = manager.setup.__globals__["_build_network_config"](vm_id="vm-abcd1234", out_iface="eth0")
            with self.assertRaises(CleanupError):
                manager.cleanup(cfg)

    def test_render_network_env_file(self) -> None:
        cfg = NetworkManager.setup.__globals__["_build_network_config"](vm_id="vm-xyz", out_iface="eth0")
        text = render_network_env_file(cfg)
        self.assertIn("SPARKVM_NET_ENABLED=1", text)
        self.assertIn("SPARKVM_GUEST_CIDR=", text)
        self.assertIn("SPARKVM_HOST_IP=", text)


class FirecrackerAttachNetworkTest(unittest.TestCase):
    def test_attach_network_payload(self) -> None:
        client = FirecrackerAPIClient(Path("/tmp/firecracker.sock"))
        captured: dict[str, object] = {}

        def _fake_put(path: str, payload: dict[str, object]):
            captured["path"] = path
            captured["payload"] = payload
            return None

        with patch.object(client, "put", side_effect=_fake_put):
            client.attach_network(host_dev_name="spkabc", guest_mac="02:fc:00:00:00:01")

        self.assertEqual("/network-interfaces/eth0", captured["path"])
        self.assertEqual(
            {
                "iface_id": "eth0",
                "host_dev_name": "spkabc",
                "guest_mac": "02:fc:00:00:00:01",
            },
            captured["payload"],
        )


if __name__ == "__main__":
    unittest.main()
