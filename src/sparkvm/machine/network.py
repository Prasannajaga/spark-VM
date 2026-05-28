"""CNI-backed network management for SparkVM microVMs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from ..core.errors import CleanupError, NetworkSetupError
from ..core.fsops import write_json_atomic, write_text
from ..core.utils import has_network_privileges
from ..storage.db import state_db_path

from ..core.constants import NET_SETUP_PRIVILEGE_MESSAGE


DEFAULT_NETWORK_NAME = "sparkvm"
DEFAULT_IFNAME = "veth0"
DEFAULT_TAP_NAME = "tap0"
DEFAULT_DNS = "1.1.1.1"
CNI_BINARIES = ("cnitool", "ptp", "host-local", "firewall", "tc-redirect-tap")

NETWORK_DIAG_FILENAMES = {
    "add_stdout_json": "network-add.stdout.json",
    "add_stderr_log": "network-add.stderr.log",
    "netns_addr_json": "network-netns-addr.json",
    "netns_route_json": "network-netns-route.json",
    "del_stdout_log": "network-del.stdout.log",
    "del_stderr_log": "network-del.stderr.log",
}


@dataclass
class NetworkDiagnostics:
    add_stdout_json: str = ""
    add_stderr_log: str = ""
    netns_addr_json: str = ""
    netns_route_json: str = ""
    del_stdout_log: str = ""
    del_stderr_log: str = ""

    def to_payloads(self) -> dict[str, str]:
        return {
            NETWORK_DIAG_FILENAMES["add_stdout_json"]: _normalize_json_text(self.add_stdout_json, fallback="{}\n"),
            NETWORK_DIAG_FILENAMES["add_stderr_log"]: self.add_stderr_log,
            NETWORK_DIAG_FILENAMES["netns_addr_json"]: _normalize_json_text(self.netns_addr_json, fallback="[]\n"),
            NETWORK_DIAG_FILENAMES["netns_route_json"]: _normalize_json_text(self.netns_route_json, fallback="[]\n"),
            NETWORK_DIAG_FILENAMES["del_stdout_log"]: self.del_stdout_log,
            NETWORK_DIAG_FILENAMES["del_stderr_log"]: self.del_stderr_log,
        }


@dataclass(frozen=True)
class NetworkConfig:
    enabled: bool
    worker_id: str
    namespace_name: str
    namespace_path: str
    network_name: str
    ifname: str
    tap_name: str
    guest_mac: str
    guest_ip: str | None
    guest_cidr: str | None
    gateway: str | None
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
        f"SPARKVM_GATEWAY={config.gateway or ''}",
        f"SPARKVM_DNS={dns}",
        f"SPARKVM_NET_IP_SOURCE={config.ip_source}",
        "",
    ]
    return "\n".join(lines)


class NetworkManager:
    def __init__(
        self,
        *,
        home_dir: Path,
        network_name: str | None = None,
        ifname: str = DEFAULT_IFNAME,
        tap_name: str = DEFAULT_TAP_NAME,
    ) -> None:
        self.home_dir = Path(home_dir)
        self.cni_path = self.home_dir / "cni" / "bin"
        self.netconf_path = self.home_dir / "cni" / "conf"
        self.network_name = (network_name or os.getenv("SPARKVM_CNI_NETWORK_NAME", DEFAULT_NETWORK_NAME)).strip() or DEFAULT_NETWORK_NAME
        self.ifname = ifname
        self.tap_name = tap_name

    def setup(self, vm_id: str) -> NetworkConfig:
        self._validate_requirements()

        namespace_name = namespace_name_for(vm_id)
        namespace_path = f"/var/run/netns/{namespace_name}"
        guest_mac_addr = guest_mac(vm_id)
        diagnostics = NetworkDiagnostics()

        namespace_created = False
        cni_add_attempted = False

        try:
            self._run_checked(["ip", "netns", "add", namespace_name])
            namespace_created = True

            cni_add = self._run_cni("add", namespace_path=namespace_path, diagnostics=diagnostics)
            cni_add_attempted = True

            raw_result = self._parse_json_result(cni_add.stdout)
            guest_ip, guest_cidr, gateway, dns, ip_source = self._resolve_network_fields(
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
                gateway=gateway,
                dns=dns,
                raw_result=raw_result,
                ip_source=ip_source,
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
                        gateway=None,
                        dns=DEFAULT_DNS,
                        raw_result={},
                        ip_source="stdout",
                        diagnostics=diagnostics.to_payloads(),
                    )
                )
            raise
        except Exception as exc:
            if namespace_created:
                self._cleanup_after_setup_failure(
                    namespace_name=namespace_name,
                    namespace_path=namespace_path,
                    cni_add_attempted=cni_add_attempted,
                    diagnostics=diagnostics,
                )
            failure_result = self._parse_json_result_best_effort(diagnostics.add_stdout_json)
            self._persist_network_artifacts(worker_id=vm_id, raw_result=failure_result, diagnostics=diagnostics)
            wrapped = NetworkSetupError(str(exc))
            self._attach_diagnostics(wrapped, worker_id=vm_id, diagnostics=diagnostics)
            raise wrapped from exc

    def cleanup(self, config: NetworkConfig) -> None:
        errors: list[str] = []
        diagnostics = NetworkDiagnostics()

        try:
            self._run_cni("del", namespace_path=config.namespace_path, diagnostics=diagnostics)
        except NetworkSetupError as exc:
            if not looks_like_missing_resource_error(str(exc)):
                errors.append(str(exc))

        try:
            self._run_checked(["ip", "netns", "del", config.namespace_name])
        except NetworkSetupError as exc:
            if not looks_like_missing_resource_error(str(exc)):
                errors.append(str(exc))

        self._persist_cleanup_logs(worker_id=config.worker_id, diagnostics=diagnostics)

        if errors:
            raise CleanupError(f"Network cleanup failed for {config.namespace_name}: {errors[0]}")

    def cleanup_best_effort(self, config: NetworkConfig) -> None:
        try:
            self.cleanup(config)
        except Exception:
            return

    def _validate_requirements(self) -> None:
        if not has_network_privileges():
            raise NetworkSetupError(NET_SETUP_PRIVILEGE_MESSAGE)

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
    ) -> tuple[str, str, str | None, str, str]:
        dns = self._extract_dns(raw_result)
        from_stdout = self._extract_ipv4_from_stdout(raw_result)
        if from_stdout is not None:
            guest_ip, guest_cidr, gateway = from_stdout
            return guest_ip, guest_cidr, gateway, dns, "stdout"

        guest_ip, guest_cidr = self._resolve_ipv4_from_netns(namespace_name=namespace_name, diagnostics=diagnostics)
        gateway = self._resolve_gateway_from_netns(namespace_name=namespace_name, diagnostics=diagnostics)
        if not guest_ip or not guest_cidr:
            diag_paths = self._diagnostic_path_map(worker_id)
            raise NetworkSetupError(
                "CNI ADD completed but SparkVM could not resolve guest IPv4 from CNI stdout or netns inspection.\n"
                f"Diagnostics: {', '.join(f'{name}={path}' for name, path in diag_paths.items())}"
            )
        return guest_ip, guest_cidr, gateway, dns, "netns"

    def _extract_ipv4_from_stdout(self, payload: dict[str, Any]) -> tuple[str, str, str | None] | None:
        ips = payload.get("ips")
        ipv4_entry: dict[str, Any] | None = None
        if isinstance(ips, list):
            for entry in ips:
                if not isinstance(entry, dict):
                    continue
                version = str(entry.get("version", "")).strip()
                address = str(entry.get("address", "")).strip()
                if version == "4" and address and "/" in address:
                    ipv4_entry = entry
                    break

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
        return guest_ip, guest_cidr, gateway

    def _extract_dns(self, payload: dict[str, Any]) -> str:
        dns_value = DEFAULT_DNS
        dns = payload.get("dns")
        if isinstance(dns, dict):
            nameservers = dns.get("nameservers")
            if isinstance(nameservers, list):
                for item in nameservers:
                    if isinstance(item, str) and _is_usable_guest_dns_nameserver(item):
                        dns_value = item.strip()
                        break

        return dns_value

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
            guest_ip, guest_cidr, gateway, dns, ip_source = self._resolve_network_fields(
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
                gateway=gateway,
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


def namespace_name_for(vm_id: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", vm_id.lower())
    if not cleaned:
        cleaned = hashlib.sha256(vm_id.encode("utf-8")).hexdigest()
    return f"spk-{cleaned[:12]}"


def guest_mac(vm_id: str) -> str:
    digest = hashlib.sha256(vm_id.encode("utf-8")).digest()
    return ":".join(f"{byte:02x}" for byte in (0x02, 0xFC, digest[0], digest[1], digest[2], digest[3]))


def looks_like_missing_resource_error(detail: str) -> bool:
    lowered = detail.lower()
    markers = (
        "no such file or directory",
        "cannot find",
        "not found",
        "not exist",
        "cannot remove namespace file",
        "failed to open netns",
    )
    return any(marker in lowered for marker in markers)


def _normalize_json_text(raw: str, *, fallback: str) -> str:
    text = raw or ""
    if not text.strip():
        text = fallback
    if not text.endswith("\n"):
        text = text + "\n"
    return text


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


__all__ = [
    "NetworkConfig",
    "NetworkDiagnostics",
    "NetworkManager",
    "render_network_env_file",
    "namespace_name_for",
]
