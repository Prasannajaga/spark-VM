#!/usr/bin/env python3
from __future__ import annotations

"""
Run a rollout from a GitHub repository.

Usage:
  python3 examples/githubExample.py

Edit REPO_URL / REF / RUN_CMD below for your project.
"""

import os

from sparkvm import RunConfig, SparkVM
from sparkvm.rollouts import Rollouts

REPO_URL = "https://github.com/org/repo.git"
REF = "main"
RUN_CMD = "python3 main.py"

# Optional bounded network debug override:
# RUN_CMD = """
# set -eux
# echo "[net] ip addr"
# ip addr || true
# echo "[net] ip route"
# ip route || true
# echo "[net] resolv"
# cat /etc/resolv.conf || true
# echo "[net] dns"
# timeout 5 getent hosts pypi.org || true
# echo "[net] curl"
# curl -Iv --connect-timeout 5 --max-time 10 https://pypi.org/simple/ || true
# """


def main() -> int:
    manager = Rollouts()
    rollout = manager.create(
        name="version-4",
        source=REPO_URL,
        ref=REF,
        run_cmd=RUN_CMD,
        delete_on_success=False,
    )

    print("Created repo rollout")
    print(f"id: {rollout.id}")
    print(f"mode: {rollout.mode}")
    print(f"path: {rollout.path}")

    runtime_env = {}
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        runtime_env["OPENAI_API_KEY"] = api_key

    result = SparkVM().run(
        rollout.id,
        config=RunConfig(
            vcpu=2,
            memory="2G",
            disk="4G",
            timeout=60.0,
            runtime="sparkvm-debian-minbase",
            network=True,
            env=runtime_env,
        ),
    )

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
