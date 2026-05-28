from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sparkvm.cli.setup import ensure_cni_layout, get_sparkvm_paths, sparkvm_cni_conflist
from sparkvm.core.errors import NetworkSetupError
from sparkvm.machine.network import NetworkManager, namespace_name_for


def _cp(cmd: list[str], *, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=stderr)


class TestNetworkCNI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.worker_id = "worker-abc123"
        (self.home / "workers" / self.worker_id).mkdir(parents=True, exist_ok=True)
        self.manager = NetworkManager(home_dir=self.home)

    def test_stdout_ips_source_stdout(self) -> None:
        payload = {
            "ips": [{"version": "4", "address": "172.31.5.12/16", "gateway": "172.31.0.1"}],
            "dns": {"nameservers": ["8.8.8.8"]},
        }
        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", return_value=_cp(["ip", "netns", "add"])),
            patch.object(self.manager, "_run_cni", return_value=_cp(["cnitool", "add"], stdout=json.dumps(payload))),
        ):
            config = self.manager.setup(self.worker_id)

        self.assertEqual("stdout", config.ip_source)
        self.assertEqual("172.31.5.12", config.guest_ip)
        self.assertEqual("172.31.5.12/16", config.guest_cidr)
        self.assertEqual("172.31.0.1", config.gateway)
        self.assertEqual("8.8.8.8", config.dns)

    def test_stdout_missing_ips_falls_back_to_netns(self) -> None:
        ns = namespace_name_for(self.worker_id)

        def run_checked_side_effect(cmd: list[str], *, env=None):  # type: ignore[no-untyped-def]
            del env
            if cmd[:3] == ["ip", "netns", "add"]:
                return _cp(cmd)
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "-4", "addr"]:
                return _cp(
                    cmd,
                    stdout=json.dumps(
                        [
                            {"ifname": "lo", "addr_info": [{"local": "127.0.0.1", "prefixlen": 8}]},
                            {"ifname": "eth0", "addr_info": [{"local": "172.31.9.7", "prefixlen": 16}]},
                        ]
                    ),
                )
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "route", "show", "default"]:
                return _cp(cmd, stdout=json.dumps([{"dst": "default", "gateway": "172.31.0.1"}]))
            raise AssertionError(f"Unexpected command: {cmd}")

        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", side_effect=run_checked_side_effect),
            patch.object(self.manager, "_run_cni", return_value=_cp(["cnitool", "add"], stdout="{}")),
        ):
            config = self.manager.setup(self.worker_id)

        self.assertEqual("netns", config.ip_source)
        self.assertEqual("172.31.9.7", config.guest_ip)
        self.assertEqual("172.31.9.7/16", config.guest_cidr)
        self.assertEqual("172.31.0.1", config.gateway)

    def test_missing_dns_defaults_to_cloudflare(self) -> None:
        payload = {"ips": [{"version": "4", "address": "172.31.5.12/16"}]}
        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", return_value=_cp(["ip", "netns", "add"])),
            patch.object(self.manager, "_run_cni", return_value=_cp(["cnitool", "add"], stdout=json.dumps(payload))),
        ):
            config = self.manager.setup(self.worker_id)

        self.assertEqual("1.1.1.1", config.dns)

    def test_loopback_dns_defaults_to_cloudflare(self) -> None:
        payload = {
            "ips": [{"version": "4", "address": "172.31.5.12/16"}],
            "dns": {"nameservers": ["127.0.0.53"]},
        }
        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", return_value=_cp(["ip", "netns", "add"])),
            patch.object(self.manager, "_run_cni", return_value=_cp(["cnitool", "add"], stdout=json.dumps(payload))),
        ):
            config = self.manager.setup(self.worker_id)

        self.assertEqual("1.1.1.1", config.dns)

    def test_ipv6_dns_defaults_to_cloudflare_for_ipv4_guest(self) -> None:
        payload = {
            "ips": [{"version": "4", "address": "172.31.5.12/16"}],
            "dns": {"nameservers": ["2001:4860:4860::8888"]},
        }
        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", return_value=_cp(["ip", "netns", "add"])),
            patch.object(self.manager, "_run_cni", return_value=_cp(["cnitool", "add"], stdout=json.dumps(payload))),
        ):
            config = self.manager.setup(self.worker_id)

        self.assertEqual("1.1.1.1", config.dns)

    def test_no_stdout_ip_and_no_netns_ip_raises_detailed_error(self) -> None:
        ns = namespace_name_for(self.worker_id)

        def run_checked_side_effect(cmd: list[str], *, env=None):  # type: ignore[no-untyped-def]
            del env
            if cmd[:3] == ["ip", "netns", "add"]:
                return _cp(cmd)
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "-4", "addr"]:
                return _cp(cmd, stdout=json.dumps([]))
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "route", "show", "default"]:
                return _cp(cmd, stdout=json.dumps([]))
            return _cp(cmd)

        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", side_effect=run_checked_side_effect),
            patch.object(self.manager, "_run_cni", return_value=_cp(["cnitool", "add"], stdout="{}")),
            patch.object(self.manager, "cleanup_best_effort", return_value=None),
        ):
            with self.assertRaises(NetworkSetupError) as ctx:
                self.manager.setup(self.worker_id)

        message = str(ctx.exception)
        self.assertIn("CNI ADD completed but SparkVM could not resolve guest IPv4", message)
        self.assertIn("network-add.stdout.json", message)
        self.assertIn("network-netns-addr.json", message)
        self.assertIn("network-netns-route.json", message)

    def test_diagnostics_files_written(self) -> None:
        payload = {"ips": [{"version": "4", "address": "172.31.5.12/16", "gateway": "172.31.0.1"}]}
        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", return_value=_cp(["ip", "netns", "add"])),
            patch.object(self.manager, "_run_cni", return_value=_cp(["cnitool", "add"], stdout=json.dumps(payload))),
        ):
            self.manager.setup(self.worker_id)

        worker_dir = self.home / "workers" / self.worker_id
        for filename in (
            "network-add.stdout.json",
            "network-add.stderr.log",
            "network-netns-addr.json",
            "network-netns-route.json",
            "network-del.stdout.log",
            "network-del.stderr.log",
        ):
            self.assertTrue((worker_dir / filename).exists(), msg=filename)

    def test_doctor_smoke_uses_same_resolver(self) -> None:
        ns_holder: dict[str, str] = {}

        def run_checked_side_effect(cmd: list[str], *, env=None):  # type: ignore[no-untyped-def]
            del env
            if cmd[:3] == ["ip", "netns", "add"]:
                ns_holder["name"] = cmd[3]
                return _cp(cmd)
            ns = ns_holder.get("name", "")
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "-4", "addr"]:
                return _cp(cmd, stdout=json.dumps([{"ifname": "eth0", "addr_info": [{"local": "172.31.4.2", "prefixlen": 16}]}]))
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "route", "show", "default"]:
                return _cp(cmd, stdout=json.dumps([{"dst": "default", "gateway": "172.31.0.1"}]))
            if cmd[:3] == ["ip", "netns", "del"]:
                return _cp(cmd)
            raise AssertionError(f"Unexpected command: {cmd}")

        def run_cni_side_effect(action: str, **kwargs):  # type: ignore[no-untyped-def]
            if action == "add":
                return _cp(["cnitool", "add"], stdout="{}")
            if action == "del":
                return _cp(["cnitool", "del"])
            raise AssertionError(action)

        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", side_effect=run_checked_side_effect),
            patch.object(self.manager, "_run_cni", side_effect=run_cni_side_effect),
        ):
            config = self.manager.doctor_smoke()

        self.assertEqual("netns", config.ip_source)
        self.assertEqual("172.31.4.2/16", config.guest_cidr)

    def test_cleanup_stale_best_effort(self) -> None:
        targets = [("spk-a", "sparkvm"), ("spk-b", "sparkvm")]

        def run_cni_side_effect(action: str, **kwargs):  # type: ignore[no-untyped-def]
            if action != "del":
                raise AssertionError(action)
            namespace_path = str(kwargs.get("namespace_path", ""))
            if namespace_path.endswith("spk-b"):
                raise NetworkSetupError("del failed")
            return _cp(["cnitool", "del"])

        def run_checked_side_effect(cmd: list[str], *, env=None):  # type: ignore[no-untyped-def]
            del env
            if cmd == ["ip", "netns", "del", "spk-b"]:
                raise NetworkSetupError("netns delete failed")
            return _cp(cmd)

        with (
            patch.object(self.manager, "_stale_namespace_targets", return_value=targets),
            patch.object(self.manager, "_run_cni", side_effect=run_cni_side_effect),
            patch.object(self.manager, "_run_checked", side_effect=run_checked_side_effect),
        ):
            cleaned, warnings = self.manager.cleanup_stale()

        self.assertEqual(["spk-a"], cleaned)
        self.assertEqual(2, len(warnings))

    def test_cni_conflist_has_host_local_datadir(self) -> None:
        payload = sparkvm_cni_conflist(self.home)
        self.assertEqual("0.4.0", payload["cniVersion"])
        plugins = payload["plugins"]
        self.assertIsInstance(plugins, list)
        ptp = plugins[0]
        self.assertEqual("ptp", ptp["type"])
        ipam = ptp["ipam"]
        self.assertEqual("host-local", ipam["type"])
        self.assertEqual(str((self.home / "cni" / "ipam").absolute()), ipam["dataDir"])

    def test_ensure_cni_layout_creates_ipam_dir(self) -> None:
        paths = get_sparkvm_paths(self.home)
        with patch("sparkvm.cli.setup.ensure_cni_binaries", return_value=[]):
            ensure_cni_layout(paths)
        self.assertTrue((self.home / "cni" / "ipam").exists())


if __name__ == "__main__":
    unittest.main()
