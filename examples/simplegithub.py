#!/usr/bin/env python3
from __future__ import annotations

"""Run the smallest Dockerfile-backed SparkVM rollout."""

from sparkvm import Rollouts, SparkVM, VMConfig


VM_CONFIG = VMConfig(vcpu=1, memory="512M", disk="1G", timeout=5.0, network=False, secure=False, env={})


def main() -> int:
    rollout = Rollouts().create(
        name="simplegithub-single-rollout-1",
        runtime="Dockerfile",
        dockerfile="examples/simple_app/simplegithub.Dockerfile",
        deleteOnSuccess=False,
        vm_config=VM_CONFIG,
    )

    vm = SparkVM(**VM_CONFIG.to_dict())
    result = vm.run(rollout.id)

    print("Created rollout:", rollout.id)
    print("VM status:", result.status)
    print("Exit code:", result.exit_code)
    print("Passed:", result.passed)
    print("--- stdout ---")
    print(result.stdout.strip())
    print("--- stderr ---")
    print(result.stderr.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
