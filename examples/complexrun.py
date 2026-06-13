#!/usr/bin/env python3
from __future__ import annotations

"""Run a Dockerfile-backed SparkVM rollout that includes tests and package code."""

from sparkvm import Rollouts, SparkVM, VMConfig


VM_CONFIG = VMConfig(vcpu=2, memory="1G", disk="3G", timeout=120.0, network=False, env={})


def main() -> int:
    rollout = Rollouts().create(
        name="complexrun-example",
        runtime="Dockerfile",
        dockerfile="examples/complex_app/Dockerfile",
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
