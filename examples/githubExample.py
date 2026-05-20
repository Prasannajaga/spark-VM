#!/usr/bin/env python3
from __future__ import annotations

"""
Run a rollout from a GitHub repository.

Usage:
  python3 examples/run_vm_with_github_repo.py

Edit REPO_URL / REF / DOCKERFILE / RUN_CMD_OVERRIDE below for your project.
"""

import os
from pathlib import Path

from sparkvm import SparkVM
from sparkvm.rollouts import Rollouts

# Example public repository URL.
REPO_URL = "/home/prasanna/coding/test-fastapi"
REF = "main"

# Dockerfile can now be provided as an explicit path (outside source repo).
DOCKERFILE = str((Path(__file__).resolve().parent / "dockerfile").resolve())

# Optional execution override. Keep as None to use Docker CMD/ENTRYPOINT.
RUN_CMD_OVERRIDE = None

# Example bounded network debug override:
# RUN_CMD_OVERRIDE = """
# set -eux
#
# echo "[net] ip addr"
# ip addr || true
#
# echo "[net] ip route"
# ip route || true
#
# echo "[net] resolv.conf"
# cat /etc/resolv.conf || true
#
# echo "[net] dns"
# timeout 5 getent hosts pypi.org || true
#
# echo "[net] curl"
# curl -Iv --connect-timeout 5 --max-time 10 https://pypi.org/simple/ || true
#
# echo "[net] done"
# """

def main() -> int:
    manager = Rollouts()
    rollout = manager.create(
        name="version-3",
        mode="repo",
        source=REPO_URL,
        ref=REF,
        dockerfile=DOCKERFILE,
        run_cmd=RUN_CMD_OVERRIDE,
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
        timeout=60.0,
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
