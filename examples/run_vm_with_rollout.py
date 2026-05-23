#!/usr/bin/env python3
from __future__ import annotations

from sparkvm import RunConfig, SparkVM
from sparkvm.rollouts import Rollouts

# Replace this with a local git repo path or git URL.
REPO_SOURCE = "/path/to/local/repo"


def main() -> int:
    manager = Rollouts()
    rollout = manager.create(
        name="loop-run",
        source=REPO_SOURCE,
        run_cmd="python3 main.py",
    )

    print("Created rollout")
    print(f"id: {rollout.id}")

    result = SparkVM().run(
        rollout.id,
        config=RunConfig(
            vcpu=2,
            memory="2G",
            disk="4G",
            timeout=30,
            runtime="sparkvm-debian-minbase",
            network=False,
        ),
    )

    print("\nVM run result")
    print(f"rollout_id: {result.rollout_id}")
    print(f"vm_id: {result.vm_id}")
    print(f"exit_code: {result.exit_code}")
    print(f"passed: {result.passed}")
    print(f"duration_ms: {result.duration_ms}")
    print(f"stdout: {result.stdout.strip()}")
    print(f"stderr: {result.stderr.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
