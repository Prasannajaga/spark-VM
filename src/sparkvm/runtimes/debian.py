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
  echo "SparkVM: no shutdown command found" > /dev/console
  while true; do sleep 3600; done
}

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev || true

mkdir -p /job
mount /dev/vdb /job

mkdir -p /job/results

cd /job

if [ -f /job/setup.sh ]; then
  sh /job/setup.sh > /job/results/setup.stdout.log 2> /job/results/setup.stderr.log
  setup_code=$?
else
  setup_code=0
fi

echo "$setup_code" > /job/results/setup.exit_code

if [ "$setup_code" -ne 0 ]; then
  echo "$setup_code" > /job/results/final_exit_code
  shutdown_vm
fi

sh /job/run.sh > /job/results/run.stdout.log 2> /job/results/run.stderr.log
run_code=$?

echo "$run_code" > /job/results/run.exit_code
echo "$run_code" > /job/results/final_exit_code

shutdown_vm
'''

# Backward compatibility aliases.
INIT_TEMPLATE = SPARKVM_INIT_TEMPLATE
DEBIAN_MINBASE_IMAGE_ID = "debian-minbase"


__all__ = ["SPARKVM_INIT_TEMPLATE", "INIT_TEMPLATE", "DEBIAN_MINBASE_IMAGE_ID"]
