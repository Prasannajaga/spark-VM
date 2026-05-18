#!/usr/bin/env python3
from __future__ import annotations

from sparkvm import SparkVM
from sparkvm.rollouts import Rollouts


def main() -> int:
    manager = Rollouts()
    rollout = manager.create(
        name="loop-run",
        mode="script",
        files={"main.py": "for _ in range(100):\n    print('hello from vm run example')\n"},
        run_cmd="python3 /job/main.py",
    )

    print("Created rollout")
    print(f"id: {rollout.id}")

    vm = SparkVM(vcpu=2, memory="512M", timeout=30.0)
    result = vm.run(rollout)

    print("\nVM run result")
    print(f"rollout_id: {result.rollout_id}")
    print(f"vm_id: {result.vm_id}")
    print(f"exit_code: {result.exit_code}")
    print(f"passed: {result.passed}")
    print(f"duration_ms: {result.duration_ms}")
    print(f"stdout: {result.stdout.strip()}")
    print(f"stderr: {result.stderr.strip()}")

    # manager.delete_by_id(rollout.id)
    # print("\nDeleted rollout")
    # print(f"id: {rollout.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
