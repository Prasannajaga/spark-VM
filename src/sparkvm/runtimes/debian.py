"""Guest init script template injected into dockified runtime images."""

from __future__ import annotations

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
mount /dev/vdb /job

mkdir -p /job/results

print_phase_logs() {
  phase="$1"
  out_file="/job/results/${phase}.stdout.log"
  err_file="/job/results/${phase}.stderr.log"

  if [ -s "$out_file" ]; then
    echo "SparkVM: ${phase} stdout begin" > /dev/console
    cat "$out_file" > /dev/console
    echo "SparkVM: ${phase} stdout end" > /dev/console
  fi

  if [ -s "$err_file" ]; then
    echo "SparkVM: ${phase} stderr begin" > /dev/console
    cat "$err_file" > /dev/console
    echo "SparkVM: ${phase} stderr end" > /dev/console
  fi
}

if [ -f /job/.sparkvm/network.env ]; then
  . /job/.sparkvm/network.env

  if [ "$SPARKVM_NET_ENABLED" = "1" ]; then
    ip link set eth0 up
    ip addr add "$SPARKVM_GUEST_CIDR" dev eth0
    ip route add default via "$SPARKVM_HOST_IP" dev eth0
    echo "nameserver ${SPARKVM_DNS:-1.1.1.1}" > /etc/resolv.conf
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

echo "SparkVM: running run.sh" > /dev/console
sh /job/run.sh > /job/results/run.stdout.log 2> /job/results/run.stderr.log
run_code=$?
echo "SparkVM: run.sh exit code=${run_code}" > /dev/console
print_phase_logs "run"

echo "$run_code" > /job/results/run.exit_code
echo "$run_code" > /job/results/final_exit_code

shutdown_vm
'''

# Backward compatibility aliases.
INIT_TEMPLATE = SPARKVM_INIT_TEMPLATE
DEBIAN_MINBASE_IMAGE_ID = "debian-minbase"


__all__ = ["SPARKVM_INIT_TEMPLATE", "INIT_TEMPLATE", "DEBIAN_MINBASE_IMAGE_ID"]
