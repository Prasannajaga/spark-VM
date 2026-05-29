"""Centralized constants for the SparkVM project."""

import re
from importlib import resources
from pathlib import Path

# --- Configuration Defaults ---
DEFAULT_VCPU = 1
DEFAULT_MEMORY = "512M"
DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_SETUP_TIMEOUT_SEC = 300
DEFAULT_RUN_TIMEOUT_SEC = 300
DEFAULT_RUNTIME = "Dockerfile"
DEFAULT_HOME_DIR = Path.home() / ".sparkvm"

# --- Regex Patterns ---
MEMORY_RE = re.compile(r"^(?P<amount>\d+)\s*(?P<unit>m|mb|mib|g|gb|gib)?$", re.IGNORECASE)
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WORKER_ID_RE = re.compile(r"^worker-[A-Za-z0-9]+$")
ROLLOUT_ID_RE = re.compile(r"^rollout-[A-Za-z0-9_-]+$")

# --- Machine Policy ---
DEFAULT_MACHINE_POLICY = {
    "host_reserved_memory": "2G",
    "host_reserved_memory_bytes": 2 * 1024 * 1024 * 1024,
    "host_reserved_disk": "20G",
    "host_reserved_disk_bytes": 20 * 1024 * 1024 * 1024,
    "max_memory_percent": 80,
    "max_disk_percent": 80,
    "max_concurrent_vms": 4,
    "vm_memory_overhead": "256M",
    "vm_memory_overhead_bytes": 256 * 1024 * 1024,
    "vm_disk_overhead": "2G",
    "vm_disk_overhead_bytes": 2 * 1024 * 1024 * 1024,
    "poll_interval": 5.0,
    "cooldown_after_vm": 5.0,
}

# --- Rollouts ---
METADATA_VERSION = 1
ROLLOUT_METADATA_VERSION = 1

# --- Network ---
NET_SETUP_PRIVILEGE_MESSAGE = (
    "Network setup failed. sparkvm network configuration requires CAP_NET_ADMIN privileges.\n"
    "Please run the sparkvm client via sudo so SparkVM can create network namespaces and CNI networking state.\n\n"
    "For example:\n"
    "  sudo sparkvm run ...\n"
)
DEFAULT_CNI_NETWORK_NAME = "sparkvm"
DEFAULT_CNI_VERSION = "0.4.0"
SUPPORTED_CNI_VERSIONS = ("0.3.0", "0.3.1", "0.4.0", "1.0.0", "1.1.0")
DEFAULT_CNI_SUBNET = "172.31.0.0/16"
DEFAULT_CNI_ROUTE = "0.0.0.0/0"
DEFAULT_CNI_RESOLV_CONF = "/etc/resolv.conf"

# --- Images ---
BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/init"
DEBIAN_BOOT_ARGS = BOOT_ARGS
DEBIAN_MINBASE_IMAGE_ID = "debian-minbase"

def _load_sparkvm_init_template() -> str:
    return resources.files("sparkvm.core").joinpath("sparkvm_init.sh").read_text(encoding="utf-8")


SPARKVM_INIT_TEMPLATE = _load_sparkvm_init_template()

INIT_TEMPLATE = SPARKVM_INIT_TEMPLATE

# --- CLI Setup ---
FIRECRACKER_VERSION = "v1.15.1"
KERNEL_FILENAME = "vmlinux"
SUPPORTED_ARCHES = {"x86_64", "aarch64"}
REQUIRED_SETUP_TOOLS = ("curl", "tar")
DOCTOR_TOOLS = ("docker", "dd", "mkfs.ext4", "mount", "umount", "debugfs", "ip")
DOCTOR_NETWORK_TOOLS = ("ip",)

ARCH_ALIASES = {
    "amd64": "x86_64",
    "arm64": "aarch64",
}

KERNEL_URLS = {
    "x86_64": "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/x86_64/kernels/vmlinux.bin",
    "aarch64": "https://s3.amazonaws.com/spec.ccfc.min/img/quickstart_guide/aarch64/kernels/vmlinux.bin",
}

# --- CLI Runtimes ---
IP_CANDIDATE_PATHS = ("/sbin/ip", "/bin/ip", "/usr/sbin/ip", "/usr/bin/ip")
SHUTDOWN_FALLBACK_PATHS = ("/sbin/poweroff", "/usr/sbin/poweroff", "/sbin/halt", "/usr/sbin/halt", "/sbin/reboot", "/usr/sbin/reboot")
BUSYBOX_CANDIDATE_PATHS = ("/bin/busybox", "/usr/bin/busybox", "/sbin/busybox", "/usr/sbin/busybox")

__all__ = [
    "DEFAULT_VCPU", "DEFAULT_MEMORY", "DEFAULT_TIMEOUT_SEC", "DEFAULT_RUNTIME", "DEFAULT_HOME_DIR",
    "DEFAULT_SETUP_TIMEOUT_SEC", "DEFAULT_RUN_TIMEOUT_SEC",
    "MEMORY_RE", "ENV_KEY_RE", "WORKER_ID_RE", "ROLLOUT_ID_RE",
    "METADATA_VERSION", "ROLLOUT_METADATA_VERSION",
    "NET_SETUP_PRIVILEGE_MESSAGE",
    "BOOT_ARGS", "DEBIAN_BOOT_ARGS", "DEBIAN_MINBASE_IMAGE_ID", "SPARKVM_INIT_TEMPLATE", "INIT_TEMPLATE",
    "FIRECRACKER_VERSION", "KERNEL_FILENAME", "SUPPORTED_ARCHES", "REQUIRED_SETUP_TOOLS", "DOCTOR_TOOLS", "DOCTOR_NETWORK_TOOLS", "ARCH_ALIASES", "KERNEL_URLS",
    "IP_CANDIDATE_PATHS", "SHUTDOWN_FALLBACK_PATHS", "BUSYBOX_CANDIDATE_PATHS",
]
