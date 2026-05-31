from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sparkvm.cli.setup import default_cni_ipv6_subnet, ensure_cni_layout, get_sparkvm_paths, sparkvm_cni_conflist
from sparkvm.core.errors import NetworkSetupError
from sparkvm.machine.network import NetworkConfig, NetworkDiagnostics, NetworkManager, namespace_name_for, render_network_env_file


def _cp(cmd: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


class TestNetworkCNI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.worker_id = "worker-abc123"
        (self.home / "workers" / self.worker_id).mkdir(parents=True, exist_ok=True)
        self.manager = NetworkManager(home_dir=self.home)
        forwarding_patch = patch.object(self.manager, "_ensure_host_forwarding", return_value=None)
        forwarding_patch.start()
        self.addCleanup(forwarding_patch.stop)

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
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "-6", "addr"]:
                return _cp(cmd, stdout=json.dumps([]))
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "route", "show", "default"]:
                return _cp(cmd, stdout=json.dumps([{"dst": "default", "gateway": "172.31.0.1"}]))
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "-6", "route", "show", "default"]:
                return _cp(cmd, stdout=json.dumps([]))
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

    def test_dual_stack_stdout_ips_are_exported(self) -> None:
        payload = {
            "ips": [
                {"version": "4", "address": "172.31.5.12/16", "gateway": "172.31.0.1"},
                {"version": "6", "address": "fd00:31::5/64", "gateway": "fd00:31::1"},
            ],
            "dns": {"nameservers": ["8.8.8.8"]},
        }
        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", return_value=_cp(["ip", "netns", "add"])),
            patch.object(self.manager, "_run_cni", return_value=_cp(["cnitool", "add"], stdout=json.dumps(payload))),
        ):
            config = self.manager.setup(self.worker_id)

        self.assertEqual("fd00:31::5", config.guest_ipv6)
        self.assertEqual("fd00:31::5/64", config.guest_ipv6_cidr)
        self.assertEqual("fd00:31::1", config.gateway_ipv6)

        env_file = render_network_env_file(config)
        self.assertIn("SPARKVM_GUEST_IPV6_CIDR=fd00:31::5/64", env_file)
        self.assertIn("SPARKVM_GATEWAY_IPV6=fd00:31::1", env_file)


    def test_missing_dns_defaults_to_cloudflare(self) -> None:
        payload = {"ips": [{"version": "4", "address": "172.31.5.12/16"}]}
        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", return_value=_cp(["ip", "netns", "add"])),
            patch.object(self.manager, "_run_cni", return_value=_cp(["cnitool", "add"], stdout=json.dumps(payload))),
            patch("sparkvm.machine.network._first_usable_host_resolver", return_value=None),
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
            patch("sparkvm.machine.network._first_usable_host_resolver", return_value=None),
        ):
            config = self.manager.setup(self.worker_id)

        self.assertEqual("1.1.1.1", config.dns)

    def test_loopback_dns_uses_host_resolver_before_public_fallback(self) -> None:
        payload = {
            "ips": [{"version": "4", "address": "172.31.5.12/16"}],
            "dns": {"nameservers": ["127.0.0.53"]},
        }
        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", return_value=_cp(["ip", "netns", "add"])),
            patch.object(self.manager, "_run_cni", return_value=_cp(["cnitool", "add"], stdout=json.dumps(payload))),
            patch("sparkvm.machine.network._first_usable_host_resolver", return_value="10.0.0.2"),
        ):
            config = self.manager.setup(self.worker_id)

        self.assertEqual("10.0.0.2", config.dns)

    def test_ipv6_dns_defaults_to_cloudflare_for_ipv4_guest(self) -> None:
        payload = {
            "ips": [{"version": "4", "address": "172.31.5.12/16"}],
            "dns": {"nameservers": ["2001:4860:4860::8888"]},
        }
        with (
            patch.object(self.manager, "_validate_requirements", return_value=None),
            patch.object(self.manager, "_run_checked", return_value=_cp(["ip", "netns", "add"])),
            patch.object(self.manager, "_run_cni", return_value=_cp(["cnitool", "add"], stdout=json.dumps(payload))),
            patch("sparkvm.machine.network._first_usable_host_resolver", return_value=None),
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
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "-6", "addr"]:
                return _cp(cmd, stdout=json.dumps([]))
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "route", "show", "default"]:
                return _cp(cmd, stdout=json.dumps([{"dst": "default", "gateway": "172.31.0.1"}]))
            if cmd == ["ip", "netns", "exec", ns, "ip", "-j", "-6", "route", "show", "default"]:
                return _cp(cmd, stdout=json.dumps([]))
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

    def test_cni_conflist_supports_optional_ipv6_range(self) -> None:
        with patch.dict(os.environ, {"SPARKVM_CNI_IPV6_SUBNET": "fd00:31::/64"}, clear=False):
            payload = sparkvm_cni_conflist(self.home)

        ipam = payload["plugins"][0]["ipam"]
        self.assertEqual(
            [[{"subnet": "172.31.0.0/16"}], [{"subnet": "fd00:31::/64"}]],
            ipam["ranges"],
        )
        self.assertEqual([{"dst": "0.0.0.0/0"}, {"dst": "::/0"}], ipam["routes"])

    def test_cni_conflist_can_derive_non_static_ipv6_range(self) -> None:
        with patch.dict(os.environ, {"SPARKVM_CNI_ENABLE_IPV6": "1"}, clear=False):
            payload = sparkvm_cni_conflist(self.home)

        expected = default_cni_ipv6_subnet(home_dir=self.home, network_name="sparkvm")
        ipam = payload["plugins"][0]["ipam"]
        self.assertEqual([[{"subnet": "172.31.0.0/16"}], [{"subnet": expected}]], ipam["ranges"])
        self.assertNotEqual("fd00:31::/64", expected)

    def test_ensure_cni_layout_creates_ipam_dir(self) -> None:
        paths = get_sparkvm_paths(self.home)
        with patch("sparkvm.cli.setup.ensure_cni_binaries", return_value=[]):
            ensure_cni_layout(paths)
        self.assertTrue((self.home / "cni" / "ipam").exists())

    def test_host_forwarding_installs_ipv4_nat_and_forward_rules(self) -> None:
        config = self._network_config(guest_cidr="172.31.0.14/16")
        diagnostics = NetworkDiagnostics()
        commands: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            commands.append(cmd)
            if "-C" in cmd or "-D" in cmd:
                return _cp(cmd, returncode=1, stderr="rule missing")
            if cmd in (
                ["iptables", "-t", "nat", "-S"],
                ["iptables", "-S", "FORWARD"],
                ["iptables", "-t", "mangle", "-S", "FORWARD"],
            ):
                return _cp(cmd, stdout="-A FORWARD -m comment --comment sparkvm:sparkvm:ipv4:egress -j ACCEPT\n")
            return _cp(cmd, stdout="ok")

        with (
            patch("sparkvm.machine.network._default_route_interface", return_value="uplink0"),
            patch("sparkvm.machine.network.subprocess.run", side_effect=run_side_effect),
        ):
            NetworkManager._ensure_host_forwarding(self.manager, config=config, diagnostics=diagnostics)

        self.assertIn(["sysctl", "-w", "net.ipv4.ip_forward=1"], commands)
        self.assertIn(
            [
                "iptables",
                "-t",
                "nat",
                "-I",
                "POSTROUTING",
                "1",
                "-s",
                "172.31.0.0/16",
                "!",
                "-d",
                "172.31.0.0/16",
                "-o",
                "uplink0",
                "-m",
                "comment",
                "--comment",
                "sparkvm:sparkvm:ipv4:nat",
                "-j",
                "MASQUERADE",
            ],
            commands,
        )
        self.assertIn(
            [
                "iptables",
                "-I",
                "FORWARD",
                "1",
                "-s",
                "172.31.0.0/16",
                "-o",
                "uplink0",
                "-m",
                "comment",
                "--comment",
                "sparkvm:sparkvm:ipv4:egress",
                "-j",
                "ACCEPT",
            ],
            commands,
        )
        self.assertIn(
            [
                "iptables",
                "-I",
                "FORWARD",
                "1",
                "-d",
                "172.31.0.0/16",
                "-m",
                "conntrack",
                "--ctstate",
                "RELATED,ESTABLISHED",
                "-i",
                "uplink0",
                "-m",
                "comment",
                "--comment",
                "sparkvm:sparkvm:ipv4:return",
                "-j",
                "ACCEPT",
            ],
            commands,
        )
        self.assertIn(
            [
                "iptables",
                "-t",
                "mangle",
                "-I",
                "FORWARD",
                "1",
                "-s",
                "172.31.0.0/16",
                "-p",
                "tcp",
                "--tcp-flags",
                "SYN,RST",
                "SYN",
                "-o",
                "uplink0",
                "-m",
                "comment",
                "--comment",
                "sparkvm:sparkvm:ipv4:tcpmss",
                "-j",
                "TCPMSS",
                "--clamp-mss-to-pmtu",
            ],
            commands,
        )
        self.assertIn(
            [
                "iptables",
                "-t",
                "mangle",
                "-I",
                "FORWARD",
                "1",
                "-d",
                "172.31.0.0/16",
                "-p",
                "tcp",
                "--tcp-flags",
                "SYN,RST",
                "SYN",
                "-i",
                "uplink0",
                "-m",
                "comment",
                "--comment",
                "sparkvm:sparkvm:ipv4:tcpmss-return",
                "-j",
                "TCPMSS",
                "--clamp-mss-to-pmtu",
            ],
            commands,
        )
        self.assertIn("IPv4 MASQUERADE", diagnostics.host_forwarding_log)
        self.assertIn("[firewall snapshot] iptables -t nat -S", diagnostics.host_forwarding_log)
        self.assertIn("sparkvm:sparkvm:ipv4:egress", diagnostics.host_forwarding_log)

    def test_host_forwarding_installs_ipv6_nat_and_enables_forwarding(self) -> None:
        config = self._network_config(guest_cidr="172.31.0.14/16", guest_ipv6_cidr="fd4e:f694:df70:7b4f::a/64")
        diagnostics = NetworkDiagnostics()
        commands: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            commands.append(cmd)
            if "-C" in cmd or "-D" in cmd:
                return _cp(cmd, returncode=1, stderr="rule missing")
            if cmd in (
                ["iptables", "-t", "nat", "-S"],
                ["iptables", "-S", "FORWARD"],
                ["iptables", "-t", "mangle", "-S", "FORWARD"],
                ["ip6tables", "-t", "nat", "-S"],
                ["ip6tables", "-S", "FORWARD"],
                ["ip6tables", "-t", "mangle", "-S", "FORWARD"],
            ):
                return _cp(cmd, stdout="-A FORWARD -m comment --comment sparkvm:sparkvm:ipv6:egress -j ACCEPT\n")
            return _cp(cmd, stdout="ok")

        with (
            patch("sparkvm.machine.network._default_route_interface", return_value="uplink0"),
            patch("sparkvm.machine.network.shutil.which", return_value="/usr/sbin/ip6tables"),
            patch("sparkvm.machine.network.subprocess.run", side_effect=run_side_effect),
        ):
            NetworkManager._ensure_host_forwarding(self.manager, config=config, diagnostics=diagnostics)

        self.assertIn(["sysctl", "-w", "net.ipv6.conf.uplink0.accept_ra=2"], commands)
        self.assertIn(["sysctl", "-w", "net.ipv6.conf.all.forwarding=1"], commands)
        self.assertIn(["sysctl", "-w", "net.ipv6.conf.default.forwarding=1"], commands)
        self.assertIn(["sysctl", "-w", "net.ipv6.conf.uplink0.forwarding=1"], commands)
        self.assertIn(
            [
                "ip6tables",
                "-t",
                "nat",
                "-I",
                "POSTROUTING",
                "1",
                "-s",
                "fd4e:f694:df70:7b4f::/64",
                "!",
                "-d",
                "fd4e:f694:df70:7b4f::/64",
                "-o",
                "uplink0",
                "-m",
                "comment",
                "--comment",
                "sparkvm:sparkvm:ipv6:nat",
                "-j",
                "MASQUERADE",
            ],
            commands,
        )
        self.assertIn(
            [
                "ip6tables",
                "-t",
                "mangle",
                "-I",
                "FORWARD",
                "1",
                "-s",
                "fd4e:f694:df70:7b4f::/64",
                "-p",
                "tcp",
                "--tcp-flags",
                "SYN,RST",
                "SYN",
                "-o",
                "uplink0",
                "-m",
                "comment",
                "--comment",
                "sparkvm:sparkvm:ipv6:tcpmss",
                "-j",
                "TCPMSS",
                "--clamp-mss-to-pmtu",
            ],
            commands,
        )
        self.assertIn(
            [
                "ip6tables",
                "-I",
                "FORWARD",
                "1",
                "-d",
                "fd4e:f694:df70:7b4f::/64",
                "-m",
                "conntrack",
                "--ctstate",
                "RELATED,ESTABLISHED",
                "-i",
                "uplink0",
                "-m",
                "comment",
                "--comment",
                "sparkvm:sparkvm:ipv6:return",
                "-j",
                "ACCEPT",
            ],
            commands,
        )
        self.assertIn(
            [
                "ip6tables",
                "-t",
                "mangle",
                "-I",
                "FORWARD",
                "1",
                "-d",
                "fd4e:f694:df70:7b4f::/64",
                "-p",
                "tcp",
                "--tcp-flags",
                "SYN,RST",
                "SYN",
                "-i",
                "uplink0",
                "-m",
                "comment",
                "--comment",
                "sparkvm:sparkvm:ipv6:tcpmss-return",
                "-j",
                "TCPMSS",
                "--clamp-mss-to-pmtu",
            ],
            commands,
        )
        self.assertIn("IPv6 MASQUERADE", diagnostics.host_forwarding_log)
        self.assertIn("[firewall snapshot] ip6tables -t nat -S", diagnostics.host_forwarding_log)
        self.assertIn("sparkvm:sparkvm:ipv6:egress", diagnostics.host_forwarding_log)

    def test_host_forwarding_requires_ipv4_egress_interface(self) -> None:
        config = self._network_config(guest_cidr="172.31.0.14/16")
        diagnostics = NetworkDiagnostics()

        with patch("sparkvm.machine.network._default_route_interface", return_value=None):
            with self.assertRaises(NetworkSetupError) as ctx:
                NetworkManager._ensure_host_forwarding(self.manager, config=config, diagnostics=diagnostics)

        self.assertIn("Could not resolve host IPv4 default egress interface", str(ctx.exception))

    def test_host_forwarding_skips_ipv6_when_host_has_no_ipv6_egress_by_default(self) -> None:
        config = self._network_config(guest_cidr="172.31.0.14/16", guest_ipv6_cidr="fd4e:f694:df70:7b4f::a/64")
        diagnostics = NetworkDiagnostics()

        def route_side_effect(*, ip_version: int, log_lines: list[str]) -> str | None:
            del log_lines
            if ip_version == 4:
                return "uplink0"
            return None

        def run_side_effect(cmd: list[str], **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            if "-C" in cmd or "-D" in cmd:
                return _cp(cmd, returncode=1, stderr="rule missing")
            return _cp(cmd, stdout="ok")

        with (
            patch("sparkvm.machine.network._default_route_interface", side_effect=route_side_effect),
            patch("sparkvm.machine.network.subprocess.run", side_effect=run_side_effect),
            patch.dict(os.environ, {"SPARKVM_REQUIRE_IPV6_FORWARDING": ""}, clear=False),
        ):
            NetworkManager._ensure_host_forwarding(self.manager, config=config, diagnostics=diagnostics)

        self.assertIn("IPv6 forwarding skipped", diagnostics.host_forwarding_log)
        self.assertIn("could not resolve host IPv6 default egress interface", diagnostics.host_forwarding_log)

    def test_host_forwarding_requires_ipv6_egress_interface_when_strict_ipv6_requested(self) -> None:
        config = self._network_config(guest_cidr="172.31.0.14/16", guest_ipv6_cidr="fd4e:f694:df70:7b4f::a/64")
        diagnostics = NetworkDiagnostics()

        def route_side_effect(*, ip_version: int, log_lines: list[str]) -> str | None:
            del log_lines
            if ip_version == 4:
                return "uplink0"
            return None

        def run_side_effect(cmd: list[str], **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            if "-C" in cmd or "-D" in cmd:
                return _cp(cmd, returncode=1, stderr="rule missing")
            return _cp(cmd, stdout="ok")

        with (
            patch("sparkvm.machine.network._default_route_interface", side_effect=route_side_effect),
            patch("sparkvm.machine.network.subprocess.run", side_effect=run_side_effect),
            patch.dict(os.environ, {"SPARKVM_REQUIRE_IPV6_FORWARDING": "1"}, clear=False),
        ):
            with self.assertRaises(NetworkSetupError) as ctx:
                NetworkManager._ensure_host_forwarding(self.manager, config=config, diagnostics=diagnostics)

        self.assertIn("Could not resolve host IPv6 default egress interface", str(ctx.exception))

    def _network_config(self, *, guest_cidr: str, guest_ipv6_cidr: str | None = None) -> NetworkConfig:
        return NetworkConfig(
            enabled=True,
            worker_id=self.worker_id,
            namespace_name=namespace_name_for(self.worker_id),
            namespace_path=f"/var/run/netns/{namespace_name_for(self.worker_id)}",
            network_name="sparkvm",
            ifname="veth0",
            tap_name="tap0",
            guest_mac="02:fc:00:00:00:01",
            guest_ip=guest_cidr.split("/", 1)[0],
            guest_cidr=guest_cidr,
            guest_ipv6=guest_ipv6_cidr.split("/", 1)[0] if guest_ipv6_cidr else None,
            guest_ipv6_cidr=guest_ipv6_cidr,
            gateway="172.31.0.1",
            gateway_ipv6="fd4e:f694:df70:7b4f::1" if guest_ipv6_cidr else None,
            dns="1.1.1.1",
            raw_result={},
            ip_source="stdout",
            diagnostics=None,
        )


if __name__ == "__main__":
    unittest.main()
