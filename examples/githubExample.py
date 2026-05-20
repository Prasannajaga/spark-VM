#!/usr/bin/env python3
from __future__ import annotations

"""
Run a rollout from a GitHub repository.

Usage:
  python3 examples/run_vm_with_github_repo.py

Edit REPO_URL / REF / SETUP_CMD / RUN_CMD below for your project.
"""

import os

from sparkvm import SparkVM
from sparkvm.rollouts import Rollouts

# Example public repository URL.
REPO_URL = "/home/prasanna/coding/test-fastapi"
REF = "main"

# Optional setup command; set to None or "" to skip setup.sh generation.
# With network=True, internet-dependent installs (pip/apt/etc.) can run in-guest.
SETUP_CMD = """
set -eu

which python3
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
"""

RUN_CMD = """
set -eu

.venv/bin/uvicorn src.main:app --host 127.0.0.1 --port 8000 &
server_pid=$!

sleep 3

.venv/bin/python - <<'PY'
import urllib.request
resp = urllib.request.urlopen("http://127.0.0.1:8000")
print("status:", resp.status)
PY

kill "$server_pid"
wait "$server_pid" || true
"""

def main() -> int:
    manager = Rollouts()
    rollout = manager.create(
        name="version-3",
        mode="repo",
        source=REPO_URL,
        ref=REF,
        setup_cmd=SETUP_CMD,
        run_cmd=RUN_CMD,
        # You can override this based on your repo size/workload.
        disk_mb=4096,
    )

    print("Created repo rollout")
    print(f"id: {rollout.id}")
    print(f"mode: {rollout.mode}")
    print(f"path: {rollout.path}")

    runtime_env = {}
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        runtime_env["OPENAI_API_KEY"] = api_key

    vm = SparkVM(
        vcpu=2,
        memory="2G",
        timeout=500.0,
        runtime="sparkvm-ubuntu-base-24.04",
        network=True,
        # Optional: pass runtime-only env vars for setup.sh/run.sh.
        env=runtime_env,
    )
    result = vm.run(rollout.id)

    print("\nVM run result")
    print(f"rollout_id: {result.rollout_id}")
    print(f"rollout_mode: {result.rollout_mode}")
    print(f"status: {result.status}")
    print(f"exit_code: {result.exit_code}")
    print(f"passed: {result.passed}")
    print(f"duration_ms: {result.duration_ms}")
    print("\nstdout")
    print(result.stdout.strip())
    print("\nstderr")
    print(result.stderr.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
