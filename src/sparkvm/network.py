"""Host-side TAP/NAT network management for SparkVM microVMs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
import subprocess
from pathlib import Path

from .errors import CleanupError, NetworkSetupError

_NET_SETUP_PRIVILEGE_MESSAGE = (
    "Network setup requires root/CAP_NET_ADMIN for TAP and iptables. "
    "Run with sudo or configure capabilities."
)


@dataclass(frozen=True)
class NetworkConfig:
    enabled: bool
    tap_name: str
    guest_mac: str
    host_ip: str
    guest_ip: str
    guest_cidr: str
    subnet_cidr: str
    out_iface: str
    dns: str = "1.1.1.1"


def detect_default_iface(route_output: str) -> str:
    match = re.search(r"\bdev\s+(?P<iface>[A-Za-z0-9_.:-]+)", route_output)
    if match is None:
        raise NetworkSetupError(f"Could not detect default outbound interface from route output: {route_output!r}")
    return match.group("iface")


def render_network_env_file(config: NetworkConfig) -> str:
    return "\n".join(
        [
            "SPARKVM_NET_ENABLED=1",
            f"SPARKVM_GUEST_CIDR={config.guest_cidr}",
            f"SPARKVM_HOST_IP={config.host_ip}",
            f"SPARKVM_DNS={config.dns}",
            "",
        ]
    )


class NetworkManager:
    def __init__(self, *, home_dir: Path) -> None:
        self.home_dir = Path(home_dir)

    def setup(self, vm_id: str) -> NetworkConfig:
        if not _has_network_privileges():
            raise NetworkSetupError(_NET_SETUP_PRIVILEGE_MESSAGE)

        out_iface = self._detect_default_iface()
        config = _build_network_config(vm_id=vm_id, out_iface=out_iface)

        try:
            self._run_checked(["ip", "tuntap", "add", "dev", config.tap_name, "mode", "tap"])
            self._run_checked(["ip", "addr", "add", f"{config.host_ip}/30", "dev", config.tap_name])
            self._run_checked(["ip", "link", "set", "dev", config.tap_name, "up"])
            self._run_checked(["sysctl", "-w", "net.ipv4.ip_forward=1"])
            self._run_checked(
                [
                    "iptables",
                    "-t",
                    "nat",
                    "-A",
                    "POSTROUTING",
                    "-s",
                    config.subnet_cidr,
                    "-o",
                    config.out_iface,
                    "-j",
                    "MASQUERADE",
                ]
            )
            self._run_checked(["iptables", "-A", "FORWARD", "-i", config.tap_name, "-o", config.out_iface, "-j", "ACCEPT"])
            self._run_checked(
                [
                    "iptables",
                    "-A",
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
                ]
            )
            self._run_checked(
                [
                    "iptables",
                    "-A",
                    "FORWARD",
                    "-i",
                    config.tap_name,
                    "-d",
                    "169.254.169.254",
                    "-j",
                    "REJECT",
                ]
            )
            return config
        except NetworkSetupError:
            self._cleanup_best_effort(config)
            raise
        except Exception as exc:
            self._cleanup_best_effort(config)
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
                self._run_checked(cmd)
            except Exception as exc:
                errors.append(exc)

        if errors:
            raise CleanupError(f"Network cleanup failed for {config.tap_name}: {errors[0]}")

    def _detect_default_iface(self) -> str:
        completed = self._run_raw(["ip", "route", "get", "1.1.1.1"])
        return detect_default_iface(completed.stdout)

    def _run_raw(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(cmd, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise NetworkSetupError(f"Required command not found: {cmd[0]}") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or "command failed"
            if _looks_like_privilege_error(detail):
                raise NetworkSetupError(_NET_SETUP_PRIVILEGE_MESSAGE) from exc
            raise NetworkSetupError(f"Command failed: {' '.join(cmd)}\\n{detail}") from exc

    def _run_checked(self, cmd: list[str]) -> None:
        self._run_raw(cmd)

    def _cleanup_best_effort(self, config: NetworkConfig) -> None:
        try:
            self.cleanup(config)
        except Exception:
            return


def _build_network_config(*, vm_id: str, out_iface: str) -> NetworkConfig:
    subnet_octet = _subnet_octet(vm_id)
    subnet_cidr = f"172.30.{subnet_octet}.0/30"
    host_ip = f"172.30.{subnet_octet}.1"
    guest_ip = f"172.30.{subnet_octet}.2"
    return NetworkConfig(
        enabled=True,
        tap_name=_tap_name(vm_id),
        guest_mac=_guest_mac(vm_id),
        host_ip=host_ip,
        guest_ip=guest_ip,
        guest_cidr=f"{guest_ip}/30",
        subnet_cidr=subnet_cidr,
        out_iface=out_iface,
    )


def _tap_name(vm_id: str) -> str:
    clean = "".join(ch for ch in vm_id if ch.isalnum()).lower()
    suffix = clean[:10] if clean else hashlib.sha256(vm_id.encode("utf-8")).hexdigest()[:10]
    return f"spk{suffix}"[:15]


def _guest_mac(vm_id: str) -> str:
    digest = hashlib.sha256(vm_id.encode("utf-8")).digest()
    return ":".join(f"{byte:02x}" for byte in (0x02, 0xFC, digest[0], digest[1], digest[2], digest[3]))


def _subnet_octet(vm_id: str) -> int:
    digest = hashlib.sha256(vm_id.encode("utf-8")).digest()
    return (digest[0] % 250) + 1


def _looks_like_privilege_error(detail: str) -> bool:
    lowered = detail.lower()
    markers = ("operation not permitted", "permission denied", "must be root", "not permitted")
    return any(marker in lowered for marker in markers)


def _has_network_privileges() -> bool:
    if os.geteuid() == 0:
        return True
    return _has_cap_net_admin()


def _has_cap_net_admin() -> bool:
    status_path = Path("/proc/self/status")
    if not status_path.exists():
        return False

    try:
        lines = status_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False

    raw_value = None
    for line in lines:
        if line.startswith("CapEff:"):
            raw_value = line.split(":", 1)[1].strip()
            break
    if raw_value is None:
        return False

    try:
        cap_eff = int(raw_value, 16)
    except ValueError:
        return False

    cap_net_admin_bit = 12
    return bool(cap_eff & (1 << cap_net_admin_bit))


__all__ = [
    "NetworkConfig",
    "NetworkManager",
    "detect_default_iface",
    "render_network_env_file",
]
