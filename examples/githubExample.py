#!/usr/bin/env python3
from __future__ import annotations

"""
Run a rollout from a GitHub repository.

Usage:
  python3 examples/run_vm_with_github_repo.py

Edit REPO_URL / REF / SETUP_CMD / RUN_CMD below for your project.
"""

from sparkvm import SparkVM
from sparkvm.rollouts import Rollouts

# Example public repository URL.
REPO_URL = "/home/prasanna/coding/test-fastapi"
REF = "main"

# Optional setup command; set to None or "" to skip setup.sh generation.
# Note: VM networking is not implemented yet, so internet-dependent installs may fail.
SETUP_CMD = "pip install -r requirements.txt"
RUN_CMD = "uvicorn src.main:app --reload"


def main() -> int:
    manager = Rollouts()
    rollout = manager.create(
        name="github-repo-run",
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

    vm = SparkVM(vcpu=2, memory="4G", timeout=600.0)
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
