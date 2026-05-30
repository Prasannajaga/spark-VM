"""Host-side TAP/NAT network management for SparkVM microVMs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
from pathlib import Path

from ..core.commands import run_checked
from ..core.errors import CleanupError, NetworkSetupError
from ..core.utils import has_cap_net_admin, has_network_privileges

from ..core.constants import NET_SETUP_PRIVILEGE_MESSAGE


DEFAULT_NETWORK_NAME = "sparkvm"
DEFAULT_IFNAME = "veth0"
DEFAULT_TAP_NAME = "tap0"
DEFAULT_DNS = "1.1.1.1"
HOST_RESOLV_CONF_CANDIDATES = (
    "/run/systemd/resolve/resolv.conf",
    "/etc/resolv.conf",
)
CNI_BINARIES = ("cnitool", "ptp", "host-local", "firewall", "tc-redirect-tap")

NETWORK_DIAG_FILENAMES = {
    "add_stdout_json": "network-add.stdout.json",
    "add_stderr_log": "network-add.stderr.log",
    "netns_addr_json": "network-netns-addr.json",
    "netns_route_json": "network-netns-route.json",
    "host_forwarding_log": "network-host-forwarding.log",
    "del_stdout_log": "network-del.stdout.log",
    "del_stderr_log": "network-del.stderr.log",
}


@dataclass
class NetworkDiagnostics:
    add_stdout_json: str = ""
    add_stderr_log: str = ""
    netns_addr_json: str = ""
    netns_route_json: str = ""
    host_forwarding_log: str = ""
    del_stdout_log: str = ""
    del_stderr_log: str = ""

    def to_payloads(self) -> dict[str, str]:
        return {
            NETWORK_DIAG_FILENAMES["add_stdout_json"]: _normalize_json_text(self.add_stdout_json, fallback="{}\n"),
            NETWORK_DIAG_FILENAMES["add_stderr_log"]: self.add_stderr_log,
            NETWORK_DIAG_FILENAMES["netns_addr_json"]: _normalize_json_text(self.netns_addr_json, fallback="[]\n"),
            NETWORK_DIAG_FILENAMES["netns_route_json"]: _normalize_json_text(self.netns_route_json, fallback="[]\n"),
            NETWORK_DIAG_FILENAMES["host_forwarding_log"]: self.host_forwarding_log,
            NETWORK_DIAG_FILENAMES["del_stdout_log"]: self.del_stdout_log,
            NETWORK_DIAG_FILENAMES["del_stderr_log"]: self.del_stderr_log,
        }


@dataclass(frozen=True)
class NetworkConfig:
    enabled: bool
    tap_name: str
    guest_mac: str
    guest_ip: str | None
    guest_cidr: str | None
    guest_ipv6: str | None
    guest_ipv6_cidr: str | None
    gateway: str | None
    gateway_ipv6: str | None
    dns: str
    raw_result: dict[str, Any]
    ip_source: str
    diagnostics: dict[str, str] | None = None


def render_network_env_file(config: NetworkConfig) -> str:
    dns = config.dns.strip() if config.dns.strip() else DEFAULT_DNS
    lines = [
        "SPARKVM_NET_ENABLED=1",
        "SPARKVM_GUEST_IFACE=eth0",
        f"SPARKVM_GUEST_CIDR={config.guest_cidr or ''}",
        f"SPARKVM_GUEST_IP={config.guest_ip or ''}",
        f"SPARKVM_GUEST_IPV6_CIDR={config.guest_ipv6_cidr or ''}",
        f"SPARKVM_GUEST_IPV6={config.guest_ipv6 or ''}",
        f"SPARKVM_GATEWAY={config.gateway or ''}",
        f"SPARKVM_GATEWAY_IPV6={config.gateway_ipv6 or ''}",
        f"SPARKVM_DNS={dns}",
        f"SPARKVM_NET_IP_SOURCE={config.ip_source}",
        "",
    ]
    return "\n".join(lines)


class NetworkManager:
    def __init__(self, *, home_dir: Path) -> None:
        self.home_dir = Path(home_dir)

    def setup(self, vm_id: str) -> NetworkConfig:
        if not has_network_privileges():
            raise NetworkSetupError(NET_SETUP_PRIVILEGE_MESSAGE)

        out_iface = self.detect_default_iface()
        config = build_network_config(vm_id=vm_id, out_iface=out_iface)

        try:
            self._run_checked(["ip", "netns", "add", namespace_name])
            namespace_created = True

            cni_add = self._run_cni("add", namespace_path=namespace_path, diagnostics=diagnostics)
            cni_add_attempted = True

            raw_result = self._parse_json_result(cni_add.stdout)
            guest_ip, guest_cidr, guest_ipv6, guest_ipv6_cidr, gateway, gateway_ipv6, dns, ip_source = self._resolve_network_fields(
                worker_id=vm_id,
                namespace_name=namespace_name,
                raw_result=raw_result,
                diagnostics=diagnostics,
            )

            config = NetworkConfig(
                enabled=True,
                worker_id=vm_id,
                namespace_name=namespace_name,
                namespace_path=namespace_path,
                network_name=self.network_name,
                ifname=self.ifname,
                tap_name=self.tap_name,
                guest_mac=guest_mac_addr,
                guest_ip=guest_ip,
                guest_cidr=guest_cidr,
                guest_ipv6=guest_ipv6,
                guest_ipv6_cidr=guest_ipv6_cidr,
                gateway=gateway,
                gateway_ipv6=gateway_ipv6,
                dns=dns,
                raw_result=raw_result,
                ip_source=ip_source,
                diagnostics=diagnostics.to_payloads(),
            )
            self._ensure_host_forwarding(config=config, diagnostics=diagnostics)
            config = NetworkConfig(
                enabled=config.enabled,
                worker_id=config.worker_id,
                namespace_name=config.namespace_name,
                namespace_path=config.namespace_path,
                network_name=config.network_name,
                ifname=config.ifname,
                tap_name=config.tap_name,
                guest_mac=config.guest_mac,
                guest_ip=config.guest_ip,
                guest_cidr=config.guest_cidr,
                guest_ipv6=config.guest_ipv6,
                guest_ipv6_cidr=config.guest_ipv6_cidr,
                gateway=config.gateway,
                gateway_ipv6=config.gateway_ipv6,
                dns=config.dns,
                raw_result=config.raw_result,
                ip_source=config.ip_source,
                diagnostics=diagnostics.to_payloads(),
            )
            self._persist_network_artifacts(worker_id=vm_id, raw_result=raw_result, diagnostics=diagnostics)
            return config
        except NetworkSetupError as exc:
            failure_result = self._parse_json_result_best_effort(diagnostics.add_stdout_json)
            self._persist_network_artifacts(worker_id=vm_id, raw_result=failure_result, diagnostics=diagnostics)
            self._attach_diagnostics(exc, worker_id=vm_id, diagnostics=diagnostics)
            if namespace_created:
                self.cleanup_best_effort(
                    NetworkConfig(
                        enabled=True,
                        worker_id=vm_id,
                        namespace_name=namespace_name,
                        namespace_path=namespace_path,
                        network_name=self.network_name,
                        ifname=self.ifname,
                        tap_name=self.tap_name,
                        guest_mac=guest_mac_addr,
                        guest_ip=None,
                        guest_cidr=None,
                        guest_ipv6=None,
                        guest_ipv6_cidr=None,
                        gateway=None,
                        gateway_ipv6=None,
                        dns=DEFAULT_DNS,
                        raw_result={},
                        ip_source="stdout",
                        diagnostics=diagnostics.to_payloads(),
                    )
                )
            raise
        except Exception as exc:
            self.cleanup_best_effort(config)
            raise NetworkSetupError(str(exc)) from exc

    def cleanup(self, config: NetworkConfig) -> None:
        errors: list[Exception] = []

        commands = [
            [
                "iptables",
                "-t",
                "nat",
                "-D",
                "POSTROUTING",
                "-s",
                config.subnet_cidr,
                "-o",
                config.out_iface,
                "-j",
                "MASQUERADE",
            ],
            ["iptables", "-D", "FORWARD", "-i", config.tap_name, "-o", config.out_iface, "-j", "ACCEPT"],
            [
                "iptables",
                "-D",
                "FORWARD",
                "-i",
                config.out_iface,
                "-o",
                config.tap_name,
                "-m",
                "conntrack",
                "--ctstate",
                "RELATED,ESTABLISHED",
                "-j",
                "ACCEPT",
            ],
            ["iptables", "-D", "FORWARD", "-i", config.tap_name, "-d", "169.254.169.254", "-j", "REJECT"],
            ["ip", "link", "delete", config.tap_name],
        ]

        for cmd in commands:
            try:
                self.run_checked(cmd)
            except Exception as exc:
                errors.append(exc)

        if errors:
            raise CleanupError(f"Network cleanup failed for {config.tap_name}: {errors[0]}")

    def detect_default_iface(self) -> str:
        completed = self.run_raw(["ip", "route", "get", "1.1.1.1"])
        return detect_default_iface(completed.stdout)

    def run_raw(self, cmd: list[str]):
        try:
            return run_checked(cmd, error_factory=NetworkSetupError)
        except NetworkSetupError as exc:
            detail = str(exc)
            if looks_like_privilege_error(detail):
                raise NetworkSetupError(NET_SETUP_PRIVILEGE_MESSAGE) from exc
            raise

    def run_checked(self, cmd: list[str]) -> None:
        self.run_raw(cmd)

    def cleanup_best_effort(self, config: NetworkConfig) -> None:
        try:
            self.cleanup(config)
        except Exception:
            return


        if shutil.which("ip") is None:
            raise NetworkSetupError("Network setup failed. Required command not found: ip")

        tun = Path("/dev/net/tun")
        if not tun.exists():
            raise NetworkSetupError("Network setup failed. Missing /dev/net/tun on host.")

        missing_binaries: list[str] = []
        for binary in CNI_BINARIES:
            candidate = self.cni_path / binary
            if not candidate.exists() or not os.access(candidate, os.X_OK):
                missing_binaries.append(str(candidate))

        if missing_binaries:
            joined = ", ".join(missing_binaries)
            raise NetworkSetupError(
                "Network setup failed. Missing required CNI binaries under SPARKVM_HOME/cni/bin: "
                f"{joined}. Run `sparkvm setup`."
            )

        config_path = self.netconf_path / f"{self.network_name}.conflist"
        if not config_path.exists():
            raise NetworkSetupError(
                f"Network setup failed. Missing CNI config: {config_path}. Run `sparkvm setup`."
            )

    def _cni_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["CNI_PATH"] = str(self.cni_path)
        env["NETCONFPATH"] = str(self.netconf_path)
        env["CNI_ARGS"] = f"IgnoreUnknown=1;TC_REDIRECT_TAP_NAME={self.tap_name}"
        env["CNI_IFNAME"] = self.ifname
        return env

    def _run_checked(self, cmd: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
        except FileNotFoundError as exc:
            raise NetworkSetupError(f"Required command not found: {cmd[0]}") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or "command failed"
            raise NetworkSetupError(f"Command failed: {' '.join(cmd)}\n{detail}") from exc

    def _run_cni(
        self,
        action: str,
        *,
        namespace_path: str,
        diagnostics: NetworkDiagnostics | None = None,
        network_name: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cnitool = self.cni_path / "cnitool"
        selected_network = (network_name or self.network_name).strip() or self.network_name
        cmd = [str(cnitool), action, selected_network, namespace_path]
        env = self._cni_env()
        try:
            completed = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
        except FileNotFoundError as exc:
            raise NetworkSetupError(f"Required command not found: {cmd[0]}") from exc
        except subprocess.CalledProcessError as exc:
            if diagnostics is not None:
                if action == "add":
                    diagnostics.add_stdout_json = exc.stdout or ""
                    diagnostics.add_stderr_log = exc.stderr or ""
                elif action == "del":
                    diagnostics.del_stdout_log = exc.stdout or ""
                    diagnostics.del_stderr_log = exc.stderr or ""
            if action == "add":
                raise self._build_cni_add_failure(cmd=cmd, env=env, stdout=exc.stdout, stderr=exc.stderr) from exc
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or "command failed"
            raise NetworkSetupError(f"Command failed: {' '.join(cmd)}\n{detail}") from exc

        if diagnostics is not None:
            if action == "add":
                diagnostics.add_stdout_json = completed.stdout or ""
                diagnostics.add_stderr_log = completed.stderr or ""
            elif action == "del":
                diagnostics.del_stdout_log = completed.stdout or ""
                diagnostics.del_stderr_log = completed.stderr or ""
        return completed

    def _parse_json_result(self, raw: str) -> dict[str, Any]:
        payload = (raw or "").strip()
        if not payload:
            return {}
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise NetworkSetupError("CNI ADD returned invalid JSON output.") from exc
        if not isinstance(decoded, dict):
            raise NetworkSetupError("CNI ADD returned an unexpected payload.")
        return decoded

    def _resolve_network_fields(
        self,
        *,
        worker_id: str,
        namespace_name: str,
        raw_result: dict[str, Any],
        diagnostics: NetworkDiagnostics,
    ) -> tuple[str, str, str | None, str | None, str | None, str | None, str, str]:
        dns = self._extract_dns(raw_result)
        from_stdout = self._extract_ips_from_stdout(raw_result)
        if from_stdout is not None:
            guest_ip, guest_cidr, guest_ipv6, guest_ipv6_cidr, gateway, gateway_ipv6 = from_stdout
            return guest_ip, guest_cidr, guest_ipv6, guest_ipv6_cidr, gateway, gateway_ipv6, dns, "stdout"

        guest_ip, guest_cidr = self._resolve_ipv4_from_netns(namespace_name=namespace_name, diagnostics=diagnostics)
        guest_ipv6, guest_ipv6_cidr = self._resolve_ipv6_from_netns(namespace_name=namespace_name)
        gateway = self._resolve_gateway_from_netns(namespace_name=namespace_name, diagnostics=diagnostics)
        gateway_ipv6 = self._resolve_gateway_ipv6_from_netns(namespace_name=namespace_name)
        if not guest_ip or not guest_cidr:
            diag_paths = self._diagnostic_path_map(worker_id)
            raise NetworkSetupError(
                "CNI ADD completed but SparkVM could not resolve guest IPv4 from CNI stdout or netns inspection.\n"
                f"Diagnostics: {', '.join(f'{name}={path}' for name, path in diag_paths.items())}"
            )
        return guest_ip, guest_cidr, guest_ipv6, guest_ipv6_cidr, gateway, gateway_ipv6, dns, "netns"

    def _extract_ips_from_stdout(
        self, payload: dict[str, Any]
    ) -> tuple[str, str, str | None, str | None, str | None, str | None] | None:
        ips = payload.get("ips")
        ipv4_entry: dict[str, Any] | None = None
        ipv6_entry: dict[str, Any] | None = None
        if isinstance(ips, list):
            for entry in ips:
                if not isinstance(entry, dict):
                    continue
                version = str(entry.get("version", "")).strip()
                address = str(entry.get("address", "")).strip()
                if version == "4" and address and "/" in address:
                    ipv4_entry = entry
                elif version == "6" and address and "/" in address:
                    ipv6_entry = entry

        if ipv4_entry is None:
            return None

        guest_cidr = str(ipv4_entry.get("address", "")).strip()
        guest_ip = guest_cidr.split("/", 1)[0].strip()
        if not guest_ip:
            return None

        gateway_raw = ipv4_entry.get("gateway")
        gateway = str(gateway_raw).strip() if gateway_raw is not None else None
        if gateway == "":
            gateway = None

        guest_ipv6: str | None = None
        guest_ipv6_cidr: str | None = None
        gateway_ipv6: str | None = None
        if ipv6_entry is not None:
            candidate_cidr = str(ipv6_entry.get("address", "")).strip()
            candidate_ip = candidate_cidr.split("/", 1)[0].strip()
            if candidate_ip:
                guest_ipv6 = candidate_ip
                guest_ipv6_cidr = candidate_cidr
            gateway_ipv6_raw = ipv6_entry.get("gateway")
            gateway_ipv6 = str(gateway_ipv6_raw).strip() if gateway_ipv6_raw is not None else None
            if gateway_ipv6 == "":
                gateway_ipv6 = None

        return guest_ip, guest_cidr, guest_ipv6, guest_ipv6_cidr, gateway, gateway_ipv6

    def _extract_dns(self, payload: dict[str, Any]) -> str:
        dns = payload.get("dns")
        if isinstance(dns, dict):
            nameservers = dns.get("nameservers")
            if isinstance(nameservers, list):
                for item in nameservers:
                    if isinstance(item, str) and _is_usable_guest_dns_nameserver(item):
                        return item.strip()

        host_dns = _first_usable_host_resolver()
        if host_dns is not None:
            return host_dns
        return DEFAULT_DNS

    def _resolve_ipv4_from_netns(self, *, namespace_name: str, diagnostics: NetworkDiagnostics) -> tuple[str | None, str | None]:
        completed = self._run_checked(["ip", "netns", "exec", namespace_name, "ip", "-j", "-4", "addr"])
        diagnostics.netns_addr_json = completed.stdout or ""
        payload = self._parse_json_list(diagnostics.netns_addr_json)
        for iface in payload:
            if not isinstance(iface, dict):
                continue
            if str(iface.get("ifname", "")).strip() == "lo":
                continue
            addr_info = iface.get("addr_info")
            if not isinstance(addr_info, list):
                continue
            for entry in addr_info:
                if not isinstance(entry, dict):
                    continue
                local = str(entry.get("local", "")).strip()
                prefixlen = entry.get("prefixlen")
                if not local or prefixlen in (None, ""):
                    continue
                guest_cidr = f"{local}/{prefixlen}"
                return local, guest_cidr
        return None, None

    def _resolve_ipv6_from_netns(self, *, namespace_name: str) -> tuple[str | None, str | None]:
        completed = self._run_checked(["ip", "netns", "exec", namespace_name, "ip", "-j", "-6", "addr"])
        payload = self._parse_json_list(completed.stdout or "")
        for iface in payload:
            if not isinstance(iface, dict):
                continue
            if str(iface.get("ifname", "")).strip() == "lo":
                continue
            addr_info = iface.get("addr_info")
            if not isinstance(addr_info, list):
                continue
            for entry in addr_info:
                if not isinstance(entry, dict):
                    continue
                scope = str(entry.get("scope", "")).strip()
                if scope == "link":
                    continue
                local = str(entry.get("local", "")).strip()
                prefixlen = entry.get("prefixlen")
                if not local or prefixlen in (None, ""):
                    continue
                guest_cidr = f"{local}/{prefixlen}"
                return local, guest_cidr
        return None, None

    def _resolve_gateway_from_netns(self, *, namespace_name: str, diagnostics: NetworkDiagnostics) -> str | None:
        completed = self._run_checked(["ip", "netns", "exec", namespace_name, "ip", "-j", "route", "show", "default"])
        diagnostics.netns_route_json = completed.stdout or ""
        payload = self._parse_json_list(diagnostics.netns_route_json)
        for route in payload:
            if not isinstance(route, dict):
                continue
            dst = str(route.get("dst", "")).strip()
            if dst not in {"default", "0.0.0.0/0"}:
                continue
            gateway = str(route.get("gateway", route.get("via", ""))).strip()
            if gateway:
                return gateway
        return None

    def _resolve_gateway_ipv6_from_netns(self, *, namespace_name: str) -> str | None:
        completed = self._run_checked(["ip", "netns", "exec", namespace_name, "ip", "-j", "-6", "route", "show", "default"])
        payload = self._parse_json_list(completed.stdout or "")
        for route in payload:
            if not isinstance(route, dict):
                continue
            dst = str(route.get("dst", "")).strip()
            if dst not in {"default", "::/0"}:
                continue
            gateway = str(route.get("gateway", route.get("via", ""))).strip()
            if gateway:
                return gateway
        return None

    def _parse_json_list(self, raw: str) -> list[Any]:
        payload = (raw or "").strip()
        if not payload:
            return []
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return []
        if not isinstance(decoded, list):
            return []
        return decoded

    def _parse_json_result_best_effort(self, raw: str) -> dict[str, Any]:
        payload = (raw or "").strip()
        if not payload:
            return {}
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        if not isinstance(decoded, dict):
            return {}
        return decoded

    def _ensure_host_forwarding(self, *, config: NetworkConfig, diagnostics: NetworkDiagnostics) -> None:
        log_lines: list[str] = []
        try:
            self._ensure_host_forwarding_rules(config=config, log_lines=log_lines)
        finally:
            diagnostics.host_forwarding_log = "\n".join(log_lines) + ("\n" if log_lines else "")

    def _ensure_host_forwarding_rules(self, *, config: NetworkConfig, log_lines: list[str]) -> None:
        if config.guest_cidr:
            ipv4_subnet = _network_from_cidr(config.guest_cidr)
            if ipv4_subnet is not None:
                ipv4_comment_prefix = f"sparkvm:{config.network_name}:ipv4"
                ipv4_egress_iface = _default_route_interface(ip_version=4, log_lines=log_lines)
                if ipv4_egress_iface is None:
                    raise NetworkSetupError("Network setup failed. Could not resolve host IPv4 default egress interface.")
                self._run_forwarding_command(["sysctl", "-w", "net.ipv4.ip_forward=1"], log_lines=log_lines)
                self._remove_legacy_forwarding_rules(
                    commands=[
                        ["iptables", "-t", "nat", "-D", "POSTROUTING", "-s", ipv4_subnet, "!", "-d", ipv4_subnet, "-j", "MASQUERADE"],
                        ["iptables", "-D", "FORWARD", "-s", ipv4_subnet, "-j", "ACCEPT"],
                        [
                            "iptables",
                            "-D",
                            "FORWARD",
                            "-d",
                            ipv4_subnet,
                            "-m",
                            "conntrack",
                            "--ctstate",
                            "RELATED,ESTABLISHED",
                            "-j",
                            "ACCEPT",
                        ],
                    ],
                    log_lines=log_lines,
                )
                self._ensure_firewall_rule(
                    check_cmd=_with_optional_output_interface(
                        [
                            "iptables",
                            "-t",
                            "nat",
                            "-C",
                            "POSTROUTING",
                            "-s",
                            ipv4_subnet,
                            "!",
                            "-d",
                            ipv4_subnet,
                        ],
                        ipv4_egress_iface,
                    )
                    + _comment_match(f"{ipv4_comment_prefix}:nat")
                    + ["-j", "MASQUERADE"],
                    add_cmd=_with_optional_output_interface(
                        [
                            "iptables",
                            "-t",
                            "nat",
                            "-I",
                            "POSTROUTING",
                            "1",
                            "-s",
                            ipv4_subnet,
                            "!",
                            "-d",
                            ipv4_subnet,
                        ],
                        ipv4_egress_iface,
                    )
                    + _comment_match(f"{ipv4_comment_prefix}:nat")
                    + ["-j", "MASQUERADE"],
                    label=f"IPv4 MASQUERADE for {ipv4_subnet}" + (f" via {ipv4_egress_iface}" if ipv4_egress_iface else ""),
                    log_lines=log_lines,
                )
                self._ensure_firewall_rule(
                    check_cmd=_with_optional_output_interface(["iptables", "-C", "FORWARD", "-s", ipv4_subnet], ipv4_egress_iface)
                    + _comment_match(f"{ipv4_comment_prefix}:egress")
                    + ["-j", "ACCEPT"],
                    add_cmd=_with_optional_output_interface(["iptables", "-I", "FORWARD", "1", "-s", ipv4_subnet], ipv4_egress_iface)
                    + _comment_match(f"{ipv4_comment_prefix}:egress")
                    + ["-j", "ACCEPT"],
                    label=f"IPv4 forward egress for {ipv4_subnet}" + (f" via {ipv4_egress_iface}" if ipv4_egress_iface else ""),
                    log_lines=log_lines,
                )
                self._ensure_firewall_rule(
                    check_cmd=_with_optional_input_interface(
                        [
                            "iptables",
                            "-C",
                            "FORWARD",
                            "-d",
                            ipv4_subnet,
                            "-m",
                            "conntrack",
                            "--ctstate",
                            "RELATED,ESTABLISHED",
                        ],
                        ipv4_egress_iface,
                    )
                    + _comment_match(f"{ipv4_comment_prefix}:return")
                    + ["-j", "ACCEPT"],
                    add_cmd=_with_optional_input_interface(
                        [
                            "iptables",
                            "-I",
                            "FORWARD",
                            "1",
                            "-d",
                            ipv4_subnet,
                            "-m",
                            "conntrack",
                            "--ctstate",
                            "RELATED,ESTABLISHED",
                        ],
                        ipv4_egress_iface,
                    )
                    + _comment_match(f"{ipv4_comment_prefix}:return")
                    + ["-j", "ACCEPT"],
                    label=f"IPv4 forward return for {ipv4_subnet}" + (f" via {ipv4_egress_iface}" if ipv4_egress_iface else ""),
                    log_lines=log_lines,
                )
                self._ensure_firewall_rule(
                    check_cmd=_with_optional_output_interface(
                        [
                            "iptables",
                            "-t",
                            "mangle",
                            "-C",
                            "FORWARD",
                            "-s",
                            ipv4_subnet,
                            "-p",
                            "tcp",
                            "--tcp-flags",
                            "SYN,RST",
                            "SYN",
                        ],
                        ipv4_egress_iface,
                    )
                    + _comment_match(f"{ipv4_comment_prefix}:tcpmss")
                    + ["-j", "TCPMSS", "--clamp-mss-to-pmtu"],
                    add_cmd=_with_optional_output_interface(
                        [
                            "iptables",
                            "-t",
                            "mangle",
                            "-I",
                            "FORWARD",
                            "1",
                            "-s",
                            ipv4_subnet,
                            "-p",
                            "tcp",
                            "--tcp-flags",
                            "SYN,RST",
                            "SYN",
                        ],
                        ipv4_egress_iface,
                    )
                    + _comment_match(f"{ipv4_comment_prefix}:tcpmss")
                    + ["-j", "TCPMSS", "--clamp-mss-to-pmtu"],
                    label=f"IPv4 TCPMSS clamp for {ipv4_subnet}" + (f" via {ipv4_egress_iface}" if ipv4_egress_iface else ""),
                    log_lines=log_lines,
                )
                self._ensure_firewall_rule(
                    check_cmd=_with_optional_input_interface(
                        [
                            "iptables",
                            "-t",
                            "mangle",
                            "-C",
                            "FORWARD",
                            "-d",
                            ipv4_subnet,
                            "-p",
                            "tcp",
                            "--tcp-flags",
                            "SYN,RST",
                            "SYN",
                        ],
                        ipv4_egress_iface,
                    )
                    + _comment_match(f"{ipv4_comment_prefix}:tcpmss-return")
                    + ["-j", "TCPMSS", "--clamp-mss-to-pmtu"],
                    add_cmd=_with_optional_input_interface(
                        [
                            "iptables",
                            "-t",
                            "mangle",
                            "-I",
                            "FORWARD",
                            "1",
                            "-d",
                            ipv4_subnet,
                            "-p",
                            "tcp",
                            "--tcp-flags",
                            "SYN,RST",
                            "SYN",
                        ],
                        ipv4_egress_iface,
                    )
                    + _comment_match(f"{ipv4_comment_prefix}:tcpmss-return")
                    + ["-j", "TCPMSS", "--clamp-mss-to-pmtu"],
                    label=f"IPv4 return TCPMSS clamp for {ipv4_subnet}" + (f" via {ipv4_egress_iface}" if ipv4_egress_iface else ""),
                    log_lines=log_lines,
                )
                self._record_firewall_snapshot(
                    commands=[
                        ["iptables", "-t", "nat", "-S"],
                        ["iptables", "-S", "FORWARD"],
                        ["iptables", "-t", "mangle", "-S", "FORWARD"],
                    ],
                    match="sparkvm:",
                    log_lines=log_lines,
                )

        if config.guest_ipv6_cidr:
            ipv6_subnet = _network_from_cidr(config.guest_ipv6_cidr)
            if ipv6_subnet is not None:
                ipv6_comment_prefix = f"sparkvm:{config.network_name}:ipv6"
                ipv6_egress_iface = _default_route_interface(ip_version=6, log_lines=log_lines)
                if ipv6_egress_iface is None:
                    raise NetworkSetupError("Network setup failed. Could not resolve host IPv6 default egress interface.")
                if shutil.which("ip6tables") is None:
                    raise NetworkSetupError("Network setup failed. Required command not found for IPv6 NAT: ip6tables")
                # Keep RA-learned host IPv6 routes alive after forwarding is enabled.
                self._run_forwarding_command(_sysctl_set("net.ipv6.conf", ipv6_egress_iface, "accept_ra", "2"), log_lines=log_lines)
                self._run_forwarding_command(["sysctl", "-w", "net.ipv6.conf.all.forwarding=1"], log_lines=log_lines)
                self._run_forwarding_command(["sysctl", "-w", "net.ipv6.conf.default.forwarding=1"], log_lines=log_lines)
                self._run_forwarding_command(_sysctl_set("net.ipv6.conf", ipv6_egress_iface, "forwarding", "1"), log_lines=log_lines)
                self._remove_legacy_forwarding_rules(
                    commands=[
                        ["ip6tables", "-t", "nat", "-D", "POSTROUTING", "-s", ipv6_subnet, "!", "-d", ipv6_subnet, "-j", "MASQUERADE"],
                        ["ip6tables", "-D", "FORWARD", "-s", ipv6_subnet, "-j", "ACCEPT"],
                        [
                            "ip6tables",
                            "-D",
                            "FORWARD",
                            "-d",
                            ipv6_subnet,
                            "-m",
                            "conntrack",
                            "--ctstate",
                            "RELATED,ESTABLISHED",
                            "-j",
                            "ACCEPT",
                        ],
                    ],
                    log_lines=log_lines,
                )
                self._ensure_firewall_rule(
                    check_cmd=_with_optional_output_interface(
                        ["ip6tables", "-t", "nat", "-C", "POSTROUTING", "-s", ipv6_subnet, "!", "-d", ipv6_subnet],
                        ipv6_egress_iface,
                    )
                    + _comment_match(f"{ipv6_comment_prefix}:nat")
                    + ["-j", "MASQUERADE"],
                    add_cmd=_with_optional_output_interface(
                        ["ip6tables", "-t", "nat", "-I", "POSTROUTING", "1", "-s", ipv6_subnet, "!", "-d", ipv6_subnet],
                        ipv6_egress_iface,
                    )
                    + _comment_match(f"{ipv6_comment_prefix}:nat")
                    + ["-j", "MASQUERADE"],
                    label=f"IPv6 MASQUERADE for {ipv6_subnet}" + (f" via {ipv6_egress_iface}" if ipv6_egress_iface else ""),
                    log_lines=log_lines,
                )
                self._ensure_firewall_rule(
                    check_cmd=_with_optional_output_interface(["ip6tables", "-C", "FORWARD", "-s", ipv6_subnet], ipv6_egress_iface)
                    + _comment_match(f"{ipv6_comment_prefix}:egress")
                    + ["-j", "ACCEPT"],
                    add_cmd=_with_optional_output_interface(["ip6tables", "-I", "FORWARD", "1", "-s", ipv6_subnet], ipv6_egress_iface)
                    + _comment_match(f"{ipv6_comment_prefix}:egress")
                    + ["-j", "ACCEPT"],
                    label=f"IPv6 forward egress for {ipv6_subnet}" + (f" via {ipv6_egress_iface}" if ipv6_egress_iface else ""),
                    log_lines=log_lines,
                )
                self._ensure_firewall_rule(
                    check_cmd=_with_optional_input_interface(
                        [
                            "ip6tables",
                            "-C",
                            "FORWARD",
                            "-d",
                            ipv6_subnet,
                            "-m",
                            "conntrack",
                            "--ctstate",
                            "RELATED,ESTABLISHED",
                        ],
                        ipv6_egress_iface,
                    )
                    + _comment_match(f"{ipv6_comment_prefix}:return")
                    + ["-j", "ACCEPT"],
                    add_cmd=_with_optional_input_interface(
                        [
                            "ip6tables",
                            "-I",
                            "FORWARD",
                            "1",
                            "-d",
                            ipv6_subnet,
                            "-m",
                            "conntrack",
                            "--ctstate",
                            "RELATED,ESTABLISHED",
                        ],
                        ipv6_egress_iface,
                    )
                    + _comment_match(f"{ipv6_comment_prefix}:return")
                    + ["-j", "ACCEPT"],
                    label=f"IPv6 forward return for {ipv6_subnet}" + (f" via {ipv6_egress_iface}" if ipv6_egress_iface else ""),
                    log_lines=log_lines,
                )
                self._ensure_firewall_rule(
                    check_cmd=_with_optional_output_interface(
                        [
                            "ip6tables",
                            "-t",
                            "mangle",
                            "-C",
                            "FORWARD",
                            "-s",
                            ipv6_subnet,
                            "-p",
                            "tcp",
                            "--tcp-flags",
                            "SYN,RST",
                            "SYN",
                        ],
                        ipv6_egress_iface,
                    )
                    + _comment_match(f"{ipv6_comment_prefix}:tcpmss")
                    + ["-j", "TCPMSS", "--clamp-mss-to-pmtu"],
                    add_cmd=_with_optional_output_interface(
                        [
                            "ip6tables",
                            "-t",
                            "mangle",
                            "-I",
                            "FORWARD",
                            "1",
                            "-s",
                            ipv6_subnet,
                            "-p",
                            "tcp",
                            "--tcp-flags",
                            "SYN,RST",
                            "SYN",
                        ],
                        ipv6_egress_iface,
                    )
                    + _comment_match(f"{ipv6_comment_prefix}:tcpmss")
                    + ["-j", "TCPMSS", "--clamp-mss-to-pmtu"],
                    label=f"IPv6 TCPMSS clamp for {ipv6_subnet}" + (f" via {ipv6_egress_iface}" if ipv6_egress_iface else ""),
                    log_lines=log_lines,
                )
                self._ensure_firewall_rule(
                    check_cmd=_with_optional_input_interface(
                        [
                            "ip6tables",
                            "-t",
                            "mangle",
                            "-C",
                            "FORWARD",
                            "-d",
                            ipv6_subnet,
                            "-p",
                            "tcp",
                            "--tcp-flags",
                            "SYN,RST",
                            "SYN",
                        ],
                        ipv6_egress_iface,
                    )
                    + _comment_match(f"{ipv6_comment_prefix}:tcpmss-return")
                    + ["-j", "TCPMSS", "--clamp-mss-to-pmtu"],
                    add_cmd=_with_optional_input_interface(
                        [
                            "ip6tables",
                            "-t",
                            "mangle",
                            "-I",
                            "FORWARD",
                            "1",
                            "-d",
                            ipv6_subnet,
                            "-p",
                            "tcp",
                            "--tcp-flags",
                            "SYN,RST",
                            "SYN",
                        ],
                        ipv6_egress_iface,
                    )
                    + _comment_match(f"{ipv6_comment_prefix}:tcpmss-return")
                    + ["-j", "TCPMSS", "--clamp-mss-to-pmtu"],
                    label=f"IPv6 return TCPMSS clamp for {ipv6_subnet}" + (f" via {ipv6_egress_iface}" if ipv6_egress_iface else ""),
                    log_lines=log_lines,
                )
                self._record_firewall_snapshot(
                    commands=[
                        ["ip6tables", "-t", "nat", "-S"],
                        ["ip6tables", "-S", "FORWARD"],
                        ["ip6tables", "-t", "mangle", "-S", "FORWARD"],
                    ],
                    match="sparkvm:",
                    log_lines=log_lines,
                )

    def _remove_legacy_forwarding_rules(self, *, commands: list[list[str]], log_lines: list[str]) -> None:
        for cmd in commands:
            while True:
                try:
                    completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
                except FileNotFoundError:
                    return
                self._record_forwarding_command(
                    cmd=cmd,
                    completed=completed,
                    label="legacy forwarding rule cleanup",
                    log_lines=log_lines,
                )
                if completed.returncode != 0:
                    break

    def _run_forwarding_command(self, cmd: list[str], *, log_lines: list[str]) -> None:
        try:
            completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise NetworkSetupError(f"Network setup failed. Required host command not found: {cmd[0]}") from exc
        self._record_forwarding_command(cmd=cmd, completed=completed, label="host forwarding command", log_lines=log_lines)
        if completed.returncode != 0:
            raise NetworkSetupError(_format_forwarding_failure(cmd=cmd, completed=completed))

    def _record_firewall_snapshot(self, *, commands: list[list[str]], match: str, log_lines: list[str]) -> None:
        for cmd in commands:
            try:
                completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
            except FileNotFoundError:
                log_lines.append(f"[firewall snapshot] {' '.join(cmd)}")
                log_lines.append("error=command not found")
                continue
            log_lines.append(f"[firewall snapshot] {' '.join(cmd)}")
            log_lines.append(f"exit_code={completed.returncode}")
            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                stdout = (completed.stdout or "").strip()
                detail = stderr or stdout
                if detail:
                    log_lines.append(f"error={detail}")
                continue
            lines = [line for line in (completed.stdout or "").splitlines() if match in line]
            if lines:
                log_lines.extend(lines)
            else:
                log_lines.append(f"no rules matching {match!r}")

    def _ensure_firewall_rule(
        self,
        *,
        check_cmd: list[str],
        add_cmd: list[str],
        label: str,
        log_lines: list[str],
    ) -> None:
        try:
            check = subprocess.run(check_cmd, check=False, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise NetworkSetupError(f"Network setup failed. Required host command not found: {check_cmd[0]}") from exc
        self._record_forwarding_command(cmd=check_cmd, completed=check, label=f"{label} check", log_lines=log_lines)
        if check.returncode == 0:
            log_lines.append(f"{label}: already present")
            return

        try:
            add = subprocess.run(add_cmd, check=False, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise NetworkSetupError(f"Network setup failed. Required host command not found: {add_cmd[0]}") from exc
        self._record_forwarding_command(cmd=add_cmd, completed=add, label=f"{label} add", log_lines=log_lines)
        if add.returncode != 0:
            raise NetworkSetupError(_format_forwarding_failure(cmd=add_cmd, completed=add))

    def _record_forwarding_command(
        self,
        *,
        cmd: list[str],
        completed: subprocess.CompletedProcess[str],
        label: str,
        log_lines: list[str],
    ) -> None:
        log_lines.append(f"[{label}] {' '.join(cmd)}")
        log_lines.append(f"exit_code={completed.returncode}")
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if stdout:
            log_lines.append(f"stdout={stdout}")
        if stderr:
            log_lines.append(f"stderr={stderr}")

    def _persist_network_artifacts(self, *, worker_id: str, raw_result: dict[str, Any], diagnostics: NetworkDiagnostics) -> None:
        worker_dir = self.home_dir / "workers" / worker_id
        if not worker_dir.exists() or not worker_dir.is_dir():
            return
        write_json_atomic(worker_dir / "network-result.json", raw_result, pretty=True)
        payloads = diagnostics.to_payloads()
        for filename, content in payloads.items():
            write_text(worker_dir / filename, content, encoding="utf-8")

    def _persist_cleanup_logs(self, *, worker_id: str, diagnostics: NetworkDiagnostics) -> None:
        worker_dir = self.home_dir / "workers" / worker_id
        if not worker_dir.exists() or not worker_dir.is_dir():
            return
        payloads = diagnostics.to_payloads()
        write_text(worker_dir / NETWORK_DIAG_FILENAMES["del_stdout_log"], payloads[NETWORK_DIAG_FILENAMES["del_stdout_log"]], encoding="utf-8")
        write_text(worker_dir / NETWORK_DIAG_FILENAMES["del_stderr_log"], payloads[NETWORK_DIAG_FILENAMES["del_stderr_log"]], encoding="utf-8")

    def _attach_diagnostics(self, exc: NetworkSetupError, *, worker_id: str, diagnostics: NetworkDiagnostics) -> None:
        payloads = diagnostics.to_payloads()
        path_map = self._diagnostic_path_map(worker_id)
        setattr(exc, "sparkvm_network_diagnostics", payloads)
        setattr(exc, "sparkvm_network_diagnostic_paths", {name: str(path) for name, path in path_map.items()})

    def _diagnostic_path_map(self, worker_id: str) -> dict[str, Path]:
        worker_dir = self.home_dir / "workers" / worker_id
        return {filename: worker_dir / filename for filename in NETWORK_DIAG_FILENAMES.values()}

    def _build_cni_add_failure(
        self,
        *,
        cmd: list[str],
        env: dict[str, str],
        stdout: str | None,
        stderr: str | None,
    ) -> NetworkSetupError:
        config_path = self.netconf_path / f"{self.network_name}.conflist"
        detail = (
            "CNI ADD failed.\n"
            f"command: {' '.join(cmd)}\n"
            f"stdout: {(stdout or '').strip()}\n"
            f"stderr: {(stderr or '').strip()}\n"
            f"CNI_PATH: {env.get('CNI_PATH', '')}\n"
            f"NETCONFPATH: {env.get('NETCONFPATH', '')}\n"
            f"conflist: {config_path}"
        )
        return NetworkSetupError(detail)

    def _cleanup_after_setup_failure(
        self,
        *,
        namespace_name: str,
        namespace_path: str,
        cni_add_attempted: bool,
        diagnostics: NetworkDiagnostics,
    ) -> None:
        if cni_add_attempted:
            try:
                self._run_cni("del", namespace_path=namespace_path, diagnostics=diagnostics)
            except Exception:
                pass
        try:
            self._run_checked(["ip", "netns", "del", namespace_name])
        except Exception:
            pass

    def doctor_smoke(self) -> NetworkConfig:
        self._validate_requirements()
        diagnostics = NetworkDiagnostics()
        suffix = hashlib.sha256(os.urandom(16)).hexdigest()[:10]
        namespace_name = f"spk-smoke-{suffix}"
        namespace_path = f"/var/run/netns/{namespace_name}"
        namespace_created = False
        cni_add_attempted = False
        try:
            self._run_checked(["ip", "netns", "add", namespace_name])
            namespace_created = True
            cni_add = self._run_cni("add", namespace_path=namespace_path, diagnostics=diagnostics)
            cni_add_attempted = True
            raw_result = self._parse_json_result(cni_add.stdout)
            guest_ip, guest_cidr, guest_ipv6, guest_ipv6_cidr, gateway, gateway_ipv6, dns, ip_source = self._resolve_network_fields(
                worker_id="smoke",
                namespace_name=namespace_name,
                raw_result=raw_result,
                diagnostics=diagnostics,
            )
            return NetworkConfig(
                enabled=True,
                worker_id="smoke",
                namespace_name=namespace_name,
                namespace_path=namespace_path,
                network_name=self.network_name,
                ifname=self.ifname,
                tap_name=self.tap_name,
                guest_mac=guest_mac("smoke"),
                guest_ip=guest_ip,
                guest_cidr=guest_cidr,
                guest_ipv6=guest_ipv6,
                guest_ipv6_cidr=guest_ipv6_cidr,
                gateway=gateway,
                gateway_ipv6=gateway_ipv6,
                dns=dns,
                raw_result=raw_result,
                ip_source=ip_source,
                diagnostics=diagnostics.to_payloads(),
            )
        finally:
            if cni_add_attempted:
                try:
                    self._run_cni("del", namespace_path=namespace_path, diagnostics=diagnostics)
                except Exception:
                    pass
            if namespace_created:
                try:
                    self._run_checked(["ip", "netns", "del", namespace_name])
                except Exception:
                    pass

    def cleanup_stale(self) -> tuple[list[str], list[str]]:
        targets = self._stale_namespace_targets()
        warnings: list[str] = []
        cleaned: list[str] = []
        for namespace_name, network_name in targets:
            namespace_path = f"/var/run/netns/{namespace_name}"
            try:
                self._run_cni("del", namespace_path=namespace_path, network_name=network_name)
            except NetworkSetupError as exc:
                warnings.append(f"cnitool del {namespace_name}: {exc}")
            try:
                self._run_checked(["ip", "netns", "del", namespace_name])
                cleaned.append(namespace_name)
            except NetworkSetupError as exc:
                warnings.append(f"netns del {namespace_name}: {exc}")
        return cleaned, warnings

    def _stale_namespace_targets(self) -> list[tuple[str, str]]:
        lease_targets = self._stale_namespace_targets_from_db()
        if lease_targets is not None:
            return lease_targets
        names = self._list_netns_by_prefix(prefix="spk-")
        return [(name, self.network_name) for name in names]

    def _stale_namespace_targets_from_db(self) -> list[tuple[str, str]] | None:
        db_path = state_db_path(self.home_dir)
        if not db_path.exists():
            return None
        try:
            with sqlite3.connect(db_path) as conn:
                table_row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='network_leases'"
                ).fetchone()
                if table_row is None:
                    return None
                rows = conn.execute(
                    "SELECT namespace_name, network_name FROM network_leases WHERE namespace_name LIKE 'spk-%'"
                ).fetchall()
        except sqlite3.Error:
            return None

        targets: list[tuple[str, str]] = []
        for namespace_name, network_name in rows:
            ns = str(namespace_name or "").strip()
            if not ns:
                continue
            net_name = str(network_name or "").strip() or self.network_name
            targets.append((ns, net_name))
        deduped: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in targets:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    def _list_netns_by_prefix(self, *, prefix: str) -> list[str]:
        completed = self._run_checked(["ip", "netns", "list"])
        names: list[str] = []
        for line in (completed.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            namespace_name = line.split(" ", 1)[0].strip()
            if namespace_name.startswith(prefix):
                names.append(namespace_name)
        return names


def tap_name(vm_id: str) -> str:
    clean = "".join(ch for ch in vm_id if ch.isalnum()).lower()
    suffix = clean[:10] if clean else hashlib.sha256(vm_id.encode("utf-8")).hexdigest()[:10]
    return f"spk{suffix}"[:15]


def guest_mac(vm_id: str) -> str:
    digest = hashlib.sha256(vm_id.encode("utf-8")).digest()
    return ":".join(f"{byte:02x}" for byte in (0x02, 0xFC, digest[0], digest[1], digest[2], digest[3]))


def subnet_octet(vm_id: str) -> int:
    digest = hashlib.sha256(vm_id.encode("utf-8")).digest()
    return (digest[0] % 250) + 1


def looks_like_privilege_error(detail: str) -> bool:
    lowered = detail.lower()
    markers = ("operation not permitted", "permission denied", "must be root", "not permitted")
    return any(marker in lowered for marker in markers)


def _normalize_json_text(raw: str, *, fallback: str) -> str:
    text = raw or ""
    if not text.strip():
        text = fallback
    if not text.endswith("\n"):
        text = text + "\n"
    return text


def _network_from_cidr(cidr: str) -> str | None:
    try:
        return str(ipaddress.ip_interface(cidr).network)
    except ValueError:
        return None


def _format_forwarding_failure(*, cmd: list[str], completed: subprocess.CompletedProcess[str]) -> str:
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    detail = stderr or stdout or "command failed"
    return (
        "Network setup failed while configuring host forwarding/NAT.\n"
        f"command: {' '.join(cmd)}\n"
        f"exit_code: {completed.returncode}\n"
        f"detail: {detail}"
    )


def _with_optional_output_interface(cmd: list[str], iface: str | None) -> list[str]:
    if not iface:
        return list(cmd)
    return [*cmd, "-o", iface]


def _with_optional_input_interface(cmd: list[str], iface: str | None) -> list[str]:
    if not iface:
        return list(cmd)
    return [*cmd, "-i", iface]


def _sysctl_set(prefix: str, iface: str, key: str, value: str) -> list[str]:
    return ["sysctl", "-w", f"{prefix}.{iface}.{key}={value}"]


def _comment_match(comment: str) -> list[str]:
    return ["-m", "comment", "--comment", comment]


def _default_route_interface(*, ip_version: int, log_lines: list[str]) -> str | None:
    cmd = ["ip", "-j"]
    if ip_version == 6:
        cmd.append("-6")
    cmd.extend(["route", "show", "default"])

    try:
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        log_lines.append(f"[host default IPv{ip_version} route] ip command not found")
        return None

    log_lines.append(f"[host default IPv{ip_version} route] {' '.join(cmd)}")
    log_lines.append(f"exit_code={completed.returncode}")
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if stdout:
        log_lines.append(f"stdout={stdout}")
    if stderr:
        log_lines.append(f"stderr={stderr}")

    if completed.returncode != 0:
        return None
    try:
        routes = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(routes, list):
        return None
    for route in routes:
        if not isinstance(route, dict):
            continue
        dev = str(route.get("dev", "")).strip()
        if dev:
            return dev
    return None


def _is_usable_guest_dns_nameserver(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    if candidate.lower() == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        # resolv.conf nameserver entries should be IPs; ignore hostnames.
        return False
    if ip.version != 4:
        return False
    return not ip.is_loopback


def _first_usable_host_resolver() -> str | None:
    for resolv_conf in HOST_RESOLV_CONF_CANDIDATES:
        path = Path(resolv_conf)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.strip().split()
            if len(parts) < 2 or parts[0] != "nameserver":
                continue
            candidate = parts[1]
            if _is_usable_guest_dns_nameserver(candidate):
                return candidate.strip()
    return None


__all__ = [
    "NetworkConfig",
    "NetworkManager",
    "detect_default_iface",
    "render_network_env_file",
]
