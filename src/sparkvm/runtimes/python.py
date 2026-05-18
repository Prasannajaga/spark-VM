"""Guest init script template used by the managed Debian base image."""

from __future__ import annotations

DEBIAN_MINBASE_IMAGE_ID = "debian-minbase"

INIT_TEMPLATE = '''#!/bin/sh
set +e

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
  sync
  poweroff -f
fi

sh /job/run.sh > /job/results/run.stdout.log 2> /job/results/run.stderr.log
run_code=$?

echo "$run_code" > /job/results/run.exit_code
echo "$run_code" > /job/results/final_exit_code

sync
poweroff -f
'''


__all__ = ["DEBIAN_MINBASE_IMAGE_ID", "INIT_TEMPLATE"]
