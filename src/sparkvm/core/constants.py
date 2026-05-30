"""Centralized constants for the SparkVM project."""

import re
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
    "Please run the sparkvm client via sudo, or configure the host interfaces and iptables "
    "rules manually.\n\n"
    "For example:\n"
    "  sudo sparkvm run ...\n"
)
<<<<<<< Updated upstream
=======
DEFAULT_CNI_NETWORK_NAME = "sparkvm"
DEFAULT_CNI_VERSION = "0.4.0"
SUPPORTED_CNI_VERSIONS = ("0.3.0", "0.3.1", "0.4.0", "1.0.0", "1.1.0")
DEFAULT_CNI_SUBNET = "172.31.0.0/16"
DEFAULT_CNI_ROUTE = "0.0.0.0/0"
DEFAULT_CNI_IPV6_ROUTE = "::/0"
DEFAULT_CNI_RESOLV_CONF = "/etc/resolv.conf"
>>>>>>> Stashed changes

# --- Images ---
BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/init"
DEBIAN_BOOT_ARGS = BOOT_ARGS
DEBIAN_MINBASE_IMAGE_ID = "debian-minbase"

SPARKVM_INIT_TEMPLATE = '''#!/bin/sh
set +e

shutdown_vm() {
  sync

  # Try userland shutdown commands in background so we never block forever
  # on "System halted" states that don't fully terminate the microVM.
  if command -v poweroff >/dev/null 2>&1; then
    poweroff -f >/dev/null 2>&1 &
  fi
  if command -v halt >/dev/null 2>&1; then
    halt -f >/dev/null 2>&1 &
  fi
  if command -v reboot >/dev/null 2>&1; then
    reboot -f >/dev/null 2>&1 &
  fi
  if command -v busybox >/dev/null 2>&1; then
    busybox poweroff -f >/dev/null 2>&1 &
    busybox reboot -f >/dev/null 2>&1 &
  fi

  # Give userland commands a brief chance, then force-kernel shutdown/reset.
  sleep 1
  if [ -w /proc/sysrq-trigger ]; then
    echo s > /proc/sysrq-trigger || true
    echo u > /proc/sysrq-trigger || true
    echo o > /proc/sysrq-trigger || true
    sleep 1
    echo b > /proc/sysrq-trigger || true
  fi
  echo "SparkVM: no shutdown command succeeded" > /dev/console
  while true; do sleep 3600; done
}

prepare_linux_runtime() {
  mkdir -p /proc /sys /dev /dev/pts

  mountpoint -q /proc || mount -t proc proc /proc
  mountpoint -q /sys || mount -t sysfs sysfs /sys
  mountpoint -q /dev || mount -t devtmpfs devtmpfs /dev

  mkdir -p /dev/pts
  mountpoint -q /dev/pts || mount -t devpts devpts /dev/pts

  ln -sf /proc/self/fd /dev/fd
  ln -sf /proc/self/fd/0 /dev/stdin
  ln -sf /proc/self/fd/1 /dev/stdout
  ln -sf /proc/self/fd/2 /dev/stderr
  ln -sf /proc/kcore /dev/core 2>/dev/null || true

  mkdir -p /tmp /run /var/tmp
  mountpoint -q /tmp || mount -t tmpfs tmpfs /tmp || true
  mountpoint -q /run || mount -t tmpfs tmpfs /run || true
  mountpoint -q /var/tmp || mount -t tmpfs tmpfs /var/tmp || true

  export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  export DEBIAN_FRONTEND=noninteractive
  export TZ=Etc/UTC
}

mount_job_disk() {
  mkdir -p /job

  if ! mount /dev/vdb /job; then
    echo "SparkVM: failed to mount /dev/vdb at /job" > /dev/console
    shutdown_vm
  fi

  mkdir -p /job/results
}

configure_network() {
  if [ ! -f /job/.sparkvm/network.env ]; then
    return 0
  fi

  . /job/.sparkvm/network.env

  if [ "${SPARKVM_NET_ENABLED:-0}" != "1" ]; then
    return 0
  fi

  if ! command -v ip >/dev/null 2>&1; then
    echo "SparkVM: ip command missing; network unavailable" > /dev/console
    return 1
  fi

  if [ -z "${SPARKVM_GUEST_CIDR:-}" ]; then
    echo "SparkVM: guest CIDR missing; network unavailable" > /dev/console
    return 1
  fi

<<<<<<< Updated upstream
  ip link set eth0 up
  ip addr add "$SPARKVM_GUEST_CIDR" dev eth0
  ip route add default via "$SPARKVM_HOST_IP" dev eth0

  if [ -n "${SPARKVM_DNS:-}" ]; then
    echo "nameserver ${SPARKVM_DNS}" > /etc/resolv.conf 2>/dev/null || true
  else
    echo "nameserver 1.1.1.1" > /etc/resolv.conf 2>/dev/null || true
  fi
=======
  ip link set lo up || true
  if [ -w "/proc/sys/net/ipv6/conf/${SPARKVM_GUEST_IFACE:-eth0}/disable_ipv6" ]; then
    echo 0 > "/proc/sys/net/ipv6/conf/${SPARKVM_GUEST_IFACE:-eth0}/disable_ipv6" || true
  fi
  if [ -w "/proc/sys/net/ipv6/conf/${SPARKVM_GUEST_IFACE:-eth0}/accept_dad" ]; then
    echo 0 > "/proc/sys/net/ipv6/conf/${SPARKVM_GUEST_IFACE:-eth0}/accept_dad" || true
  fi
  if ! ip link set "${SPARKVM_GUEST_IFACE:-eth0}" up; then
    echo "SparkVM: failed to bring up ${SPARKVM_GUEST_IFACE:-eth0}" > /dev/console
    return 1
  fi
  ip addr flush dev "${SPARKVM_GUEST_IFACE:-eth0}" || true
  if ! ip addr add "$SPARKVM_GUEST_CIDR" dev "${SPARKVM_GUEST_IFACE:-eth0}"; then
    echo "SparkVM: failed to assign ${SPARKVM_GUEST_CIDR} to ${SPARKVM_GUEST_IFACE:-eth0}" > /dev/console
    return 1
  fi

  if [ -n "${SPARKVM_GATEWAY:-}" ]; then
    if ! ip route replace default via "$SPARKVM_GATEWAY" dev "${SPARKVM_GUEST_IFACE:-eth0}"; then
      echo "SparkVM: failed to configure default route via ${SPARKVM_GATEWAY}" > /dev/console
      return 1
    fi
  fi

  if [ -n "${SPARKVM_GUEST_IPV6_CIDR:-}" ]; then
    if ! ip -6 addr add "$SPARKVM_GUEST_IPV6_CIDR" dev "${SPARKVM_GUEST_IFACE:-eth0}"; then
      echo "SparkVM: failed to assign ${SPARKVM_GUEST_IPV6_CIDR} to ${SPARKVM_GUEST_IFACE:-eth0}" > /dev/console
      return 1
    fi
    if ! wait_ipv6_address_ready "${SPARKVM_GUEST_IFACE:-eth0}" "$SPARKVM_GUEST_IPV6_CIDR"; then
      return 1
    fi
  fi

  if [ -n "${SPARKVM_GATEWAY_IPV6:-}" ]; then
    if ! ip -6 route replace default via "$SPARKVM_GATEWAY_IPV6" dev "${SPARKVM_GUEST_IFACE:-eth0}"; then
      echo "SparkVM: failed to configure IPv6 default route via ${SPARKVM_GATEWAY_IPV6}" > /dev/console
      return 1
    fi
  fi

  mkdir -p /etc
  {
    echo "nameserver ${SPARKVM_DNS:-1.1.1.1}"
    # Keep DNS failure latency bounded for dynamic workloads.
    echo "options timeout:2 attempts:2"
  } > /etc/resolv.conf || return 1
}

wait_ipv6_address_ready() {
  iface="$1"
  cidr="$2"

  if ! command -v grep >/dev/null 2>&1; then
    return 0
  fi

  attempts=0
  while [ "$attempts" -lt 20 ]; do
    if ip -6 addr show dev "$iface" | grep "$cidr" | grep -q tentative; then
      sleep 0.1
      attempts=$((attempts + 1))
      continue
    fi
    return 0
  done

  echo "SparkVM: IPv6 address still tentative after wait: ${cidr}" > /dev/console
  return 1
>>>>>>> Stashed changes
}

load_runtime_env() {
  if [ -f /job/.sparkvm/runtime.env ]; then
    set -a
    . /job/.sparkvm/runtime.env
    set +a
  fi

  if [ -f /job/.sparkvm/env.sh ]; then
    set -a
    . /job/.sparkvm/env.sh
    set +a
  fi
}

run_with_timeout() {
  timeout_sec="$1"
  script="$2"
  out_file="$3"
  err_file="$4"

  if command -v timeout >/dev/null 2>&1; then
    timeout "$timeout_sec" sh "$script" > "$out_file" 2> "$err_file"
    return "$?"
  fi

  echo "SparkVM: timeout command missing; phase timeout disabled" > /dev/console
  sh "$script" > "$out_file" 2> "$err_file"
  return "$?"
}

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

run_phase() {
  phase="$1"
  script="$2"
  timeout_sec="$3"
  out_file="/job/results/${phase}.stdout.log"
  err_file="/job/results/${phase}.stderr.log"

  echo "SparkVM: ${phase} begin script=${script} timeout_sec=${timeout_sec}" > /dev/console
  run_with_timeout "$timeout_sec" "$script" "$out_file" "$err_file"
  code=$?
  if [ "$code" -eq 124 ]; then
    echo "SparkVM: ${phase} timed out after ${timeout_sec}s" > /dev/console
  fi
  echo "SparkVM: ${phase} exit code=${code}" > /dev/console
  echo "$code" > "/job/results/${phase}.exit_code"
  print_phase_logs "$phase"
  echo "SparkVM: ${phase} end" > /dev/console

  return "$code"
}

collect_network_probes() {
  if [ -n "${SPARKVM_GATEWAY:-}" ]; then
    echo "[network] route to gateway ${SPARKVM_GATEWAY}"
    ip route get "$SPARKVM_GATEWAY" 2>&1 || true
  fi

  if [ -n "${SPARKVM_DNS:-}" ]; then
    echo "[network] route to dns ${SPARKVM_DNS}"
    ip route get "$SPARKVM_DNS" 2>&1 || true
  fi

  if [ -n "${SPARKVM_GATEWAY_IPV6:-}" ]; then
    echo "[network] IPv6 route to gateway ${SPARKVM_GATEWAY_IPV6}"
    ip -6 route get "$SPARKVM_GATEWAY_IPV6" 2>&1 || true
  fi

  if command -v ping >/dev/null 2>&1; then
    if [ -n "${SPARKVM_GATEWAY:-}" ]; then
      echo "[network] ping gateway ${SPARKVM_GATEWAY}"
      ping -c 1 -W 2 "$SPARKVM_GATEWAY" 2>&1 || true
    fi
    if [ -n "${SPARKVM_DNS:-}" ]; then
      echo "[network] ping dns ${SPARKVM_DNS}"
      ping -c 1 -W 2 "$SPARKVM_DNS" 2>&1 || true
    fi
    if [ -n "${SPARKVM_GATEWAY_IPV6:-}" ]; then
      echo "[network] ping IPv6 gateway ${SPARKVM_GATEWAY_IPV6}"
      ping -6 -c 1 -W 2 "$SPARKVM_GATEWAY_IPV6" 2>&1 || true
    fi
  else
    echo "[network] ping command missing"
  fi

  if command -v getent >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
    echo "[network] resolve api.github.com"
    timeout 5 getent hosts api.github.com 2>&1 || true
  else
    echo "[network] getent or timeout command missing; DNS probe skipped"
  fi
}

collect_network_diagnostics() {
  if [ "${SPARKVM_NET_ENABLED:-0}" != "1" ]; then
    return 0
  fi

  out_file="/job/results/network.stdout.log"
  err_file="/job/results/network.stderr.log"
  : > "$out_file"
  : > "$err_file"

  echo "SparkVM: network diagnostics begin" > /dev/console
  if command -v ip >/dev/null 2>&1; then
    ip addr > /dev/console 2>&1 || true
    ip route > /dev/console 2>&1 || true
    ip -6 route > /dev/console 2>&1 || true
  else
    echo "SparkVM: ip command missing; network diagnostics limited" > /dev/console
  fi
  cat /etc/resolv.conf > /dev/console 2>&1 || true
  if command -v ip >/dev/null 2>&1; then
    collect_network_probes > /dev/console 2>&1 || true
  fi
  echo "SparkVM: network diagnostics end" > /dev/console

  {
    if command -v ip >/dev/null 2>&1; then
      echo "[network] ip addr"
      ip addr
      echo ""
      echo "[network] ip route"
      ip route
      echo ""
      echo "[network] ip -6 route"
      ip -6 route
      echo ""
      collect_network_probes
      echo ""
    else
      echo "[network] ip command missing"
    fi
    echo "[network] /etc/resolv.conf"
    cat /etc/resolv.conf
  } > "$out_file" 2> "$err_file" || true
}

prepare_linux_runtime
mount_job_disk
load_runtime_env
if ! configure_network; then
  collect_network_diagnostics
  echo 125 > /job/results/final_exit_code
  shutdown_vm
fi
collect_network_diagnostics

cd /job

if [ "${SPARKVM_RUN_SETUP_IN_GUEST:-0}" = "1" ] && [ -f /job/setup.sh ]; then
  run_phase "setup" "/job/setup.sh" "${SPARKVM_SETUP_TIMEOUT_SEC:-300}"
  setup_code=$?
else
  setup_code=0
  echo 0 > /job/results/setup.exit_code
fi

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

run_phase "run" "/job/run.sh" "${SPARKVM_RUN_TIMEOUT_SEC:-300}"
run_code=$?

echo "$run_code" > /job/results/final_exit_code

shutdown_vm
'''

INIT_TEMPLATE = SPARKVM_INIT_TEMPLATE

# --- CLI Setup ---
FIRECRACKER_VERSION = "v1.15.1"
KERNEL_FILENAME = "vmlinux"
SUPPORTED_ARCHES = {"x86_64", "aarch64"}
REQUIRED_SETUP_TOOLS = ("curl", "tar")
<<<<<<< Updated upstream
DOCTOR_TOOLS = ("docker", "dd", "mkfs.ext4", "mount", "umount", "debugfs", "ip", "iptables", "sysctl")
DOCTOR_NETWORK_TOOLS = ("ip", "iptables", "sysctl")
=======
DOCTOR_TOOLS = ("docker", "dd", "mkfs.ext4", "mount", "umount", "debugfs", "ip")
DOCTOR_NETWORK_TOOLS = ("ip", "iptables", "ip6tables", "sysctl")
>>>>>>> Stashed changes

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
