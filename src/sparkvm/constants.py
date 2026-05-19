"""Centralized constants for the SparkVM project."""

import re
import shutil
from pathlib import Path

# --- Configuration Defaults ---
DEFAULT_VCPU = 1
DEFAULT_MEMORY = "512M"
DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_RUNTIME = "python-3.12-slim"
DEFAULT_BASE_IMAGE = DEFAULT_RUNTIME
DEFAULT_HOME_DIR = Path.home() / ".sparkvm"

# --- Regex Patterns ---
MEMORY_RE = re.compile(r"^(?P<amount>\d+)\s*(?P<unit>m|mb|mib|g|gb|gib)?$", re.IGNORECASE)
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WORKER_ID_RE = re.compile(r"^vm-[A-Za-z0-9]+$")
ROLLOUT_ID_RE = re.compile(r"^rollout-[A-Za-z0-9_-]+$")

# --- Rollouts ---
METADATA_VERSION = 1
SUPPORTED_MODES = {"script", "repo"}
SCRIPT_DEFAULT_DISK_MB = 1024
REPO_DEFAULT_DISK_MB = 4096
GIT_URL_PREFIXES = ("http://", "https://", "git@", "ssh://")
COPYTREE_IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".venv", "node_modules", "target", "dist", "build")
ROLLOUT_METADATA_VERSION = 1

# --- Network ---
NET_SETUP_PRIVILEGE_MESSAGE = (
    "Network setup failed. sparkvm network configuration requires CAP_NET_ADMIN privileges.\n"
    "Please run the sparkvm client via sudo, or configure the host interfaces and iptables "
    "rules manually.\n\n"
    "For example:\n"
    "  sudo sparkvm run ...\n"
)

# --- Images ---
BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/init"
DEBIAN_BOOT_ARGS = BOOT_ARGS
DEBIAN_MINBASE_IMAGE_ID = "debian-minbase"

SPARKVM_INIT_TEMPLATE = '''#!/bin/sh
set +e

shutdown_vm() {
  sync
  if command -v poweroff >/dev/null 2>&1; then
    poweroff -f
  fi
  if command -v halt >/dev/null 2>&1; then
    halt -f
  fi
  if command -v reboot >/dev/null 2>&1; then
    reboot -f
  fi
  if command -v busybox >/dev/null 2>&1; then
    busybox poweroff -f
    busybox reboot -f
  fi
  if [ -w /proc/sysrq-trigger ]; then
    echo s > /proc/sysrq-trigger || true
    echo u > /proc/sysrq-trigger || true
    echo o > /proc/sysrq-trigger || true
    sleep 1
    echo b > /proc/sysrq-trigger || true
  fi
  echo "SparkVM: no shutdown command found" > /dev/console
  while true; do sleep 3600; done
}

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev || true

mkdir -p /tmp /run /var/tmp
mount -t tmpfs tmpfs /tmp || true
mount -t tmpfs tmpfs /run || true
mount -t tmpfs tmpfs /var/tmp || true

mkdir -p /job
if ! mount /dev/vdb /job; then
  echo "SparkVM: failed to mount /dev/vdb at /job" > /dev/console
  shutdown_vm
fi

mkdir -p /job/results

redact_to_console() {
  file="$1"

  if [ ! -s "$file" ]; then
    return 0
  fi

  if [ -f /job/.sparkvm/redact.sed ] && command -v sed >/dev/null 2>&1; then
    sed -f /job/.sparkvm/redact.sed "$file" > /dev/console
    return 0
  fi

  if [ -f /job/.sparkvm/env.sh ]; then
    echo "SparkVM: log redaction unavailable; not printing raw logs because runtime env is present" > /dev/console
    return 0
  fi

  cat "$file" > /dev/console
}

print_phase_logs() {
  phase="$1"
  out_file="/job/results/${phase}.stdout.log"
  err_file="/job/results/${phase}.stderr.log"

  if [ -s "$out_file" ]; then
    echo "SparkVM: ${phase} stdout begin" > /dev/console
    redact_to_console "$out_file"
    echo "SparkVM: ${phase} stdout end" > /dev/console
  fi

  if [ -s "$err_file" ]; then
    echo "SparkVM: ${phase} stderr begin" > /dev/console
    redact_to_console "$err_file"
    echo "SparkVM: ${phase} stderr end" > /dev/console
  fi
}

if [ -f /job/.sparkvm/network.env ]; then
  . /job/.sparkvm/network.env

  if [ "${SPARKVM_NET_ENABLED:-0}" = "1" ]; then
    if ! command -v ip >/dev/null 2>&1; then
      echo "SparkVM: ip command missing; network unavailable" > /dev/console
    else
      ip link set eth0 up
      ip addr add "$SPARKVM_GUEST_CIDR" dev eth0
      ip route add default via "$SPARKVM_HOST_IP" dev eth0
      echo "nameserver ${SPARKVM_DNS:-1.1.1.1}" > /etc/resolv.conf
    fi
  fi
fi

if [ -f /job/.sparkvm/env.sh ]; then
  set -a
  . /job/.sparkvm/env.sh
  set +a
fi

cd /job

if [ -f /job/setup.sh ]; then
  echo "SparkVM: running setup.sh" > /dev/console
  sh /job/setup.sh > /job/results/setup.stdout.log 2> /job/results/setup.stderr.log
  setup_code=$?
  echo "SparkVM: setup.sh exit code=${setup_code}" > /dev/console
  print_phase_logs "setup"
else
  setup_code=0
fi

echo "$setup_code" > /job/results/setup.exit_code

if [ "$setup_code" -ne 0 ]; then
  echo "$setup_code" > /job/results/final_exit_code
  shutdown_vm
fi

if [ ! -f /job/run.sh ]; then
  echo "missing /job/run.sh" > /job/results/run.stderr.log
  echo 127 > /job/results/run.exit_code
  echo 127 > /job/results/final_exit_code
  shutdown_vm
fi

echo "SparkVM: running run.sh" > /dev/console
sh /job/run.sh > /job/results/run.stdout.log 2> /job/results/run.stderr.log
run_code=$?
echo "SparkVM: run.sh exit code=${run_code}" > /dev/console
print_phase_logs "run"

echo "$run_code" > /job/results/run.exit_code
echo "$run_code" > /job/results/final_exit_code

shutdown_vm
'''

INIT_TEMPLATE = SPARKVM_INIT_TEMPLATE

# --- CLI Setup ---
FIRECRACKER_VERSION = "v1.15.1"
KERNEL_FILENAME = "vmlinux"
SUPPORTED_ARCHES = {"x86_64", "aarch64"}
REQUIRED_SETUP_TOOLS = ("curl", "tar")
DOCTOR_TOOLS = ("docker", "dd", "mkfs.ext4", "mount", "umount", "debugfs", "ip", "iptables", "sysctl")
DOCTOR_NETWORK_TOOLS = ("ip", "iptables", "sysctl")

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

__all__ = [
    "DEFAULT_VCPU", "DEFAULT_MEMORY", "DEFAULT_TIMEOUT_SEC", "DEFAULT_RUNTIME", "DEFAULT_BASE_IMAGE", "DEFAULT_HOME_DIR",
    "MEMORY_RE", "ENV_KEY_RE", "WORKER_ID_RE", "ROLLOUT_ID_RE",
    "METADATA_VERSION", "SUPPORTED_MODES", "SCRIPT_DEFAULT_DISK_MB", "REPO_DEFAULT_DISK_MB", "GIT_URL_PREFIXES", "COPYTREE_IGNORE", "ROLLOUT_METADATA_VERSION",
    "NET_SETUP_PRIVILEGE_MESSAGE",
    "BOOT_ARGS", "DEBIAN_BOOT_ARGS", "DEBIAN_MINBASE_IMAGE_ID", "SPARKVM_INIT_TEMPLATE", "INIT_TEMPLATE",
    "FIRECRACKER_VERSION", "KERNEL_FILENAME", "SUPPORTED_ARCHES", "REQUIRED_SETUP_TOOLS", "DOCTOR_TOOLS", "DOCTOR_NETWORK_TOOLS", "ARCH_ALIASES", "KERNEL_URLS",
    "IP_CANDIDATE_PATHS", "SHUTDOWN_FALLBACK_PATHS",
]
