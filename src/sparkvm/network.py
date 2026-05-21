"""Host-side TAP/NAT network management for SparkVM microVMs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
from pathlib import Path

from .commands import run_checked
from .errors import CleanupError, NetworkSetupError
from .utils import has_cap_net_admin, has_network_privileges

from .constants import NET_SETUP_PRIVILEGE_MESSAGE


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
        if not has_network_privileges():
            raise NetworkSetupError(NET_SETUP_PRIVILEGE_MESSAGE)

        out_iface = self.detect_default_iface()
        config = build_network_config(vm_id=vm_id, out_iface=out_iface)

        try:
            self.run_checked(["ip", "tuntap", "add", "dev", config.tap_name, "mode", "tap"])
            self.run_checked(["ip", "addr", "add", f"{config.host_ip}/30", "dev", config.tap_name])
            self.run_checked(["ip", "link", "set", "dev", config.tap_name, "up"])
            self.run_checked(["sysctl", "-w", "net.ipv4.ip_forward=1"])
            self.run_checked(
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
            self.run_checked(["iptables", "-A", "FORWARD", "-i", config.tap_name, "-o", config.out_iface, "-j", "ACCEPT"])
            self.run_checked(
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
            self.run_checked(
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
            self.cleanup_best_effort(config)
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


def build_network_config(*, vm_id: str, out_iface: str) -> NetworkConfig:
    subnet_oct = subnet_octet(vm_id)
    subnet_cidr = f"172.30.{subnet_oct}.0/30"
    host_ip = f"172.30.{subnet_oct}.1"
    guest_ip = f"172.30.{subnet_oct}.2"
    return NetworkConfig(
        enabled=True,
        tap_name=tap_name(vm_id),
        guest_mac=guest_mac(vm_id),
        host_ip=host_ip,
        guest_ip=guest_ip,
        guest_cidr=f"{guest_ip}/30",
        subnet_cidr=subnet_cidr,
        out_iface=out_iface,
    )


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




__all__ = [
    "NetworkConfig",
    "NetworkManager",
    "detect_default_iface",
    "render_network_env_file",
]
