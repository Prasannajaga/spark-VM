"""Python runtime metadata and init script template."""

from __future__ import annotations

PYTHON_RUNTIME_ID = "python-3.12"

INIT_TEMPLATE = '''#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev || true

mkdir -p /job
mount /dev/vdb /job

cd /job

timeout "${SPARKVM_TIMEOUT_SEC:-30}" sh /job/run.sh > /job/output.log 2> /job/error.log
code=$?

echo "$code" > /job/exit_code

sync
poweroff -f
'''


__all__ = ["PYTHON_RUNTIME_ID", "INIT_TEMPLATE"]
