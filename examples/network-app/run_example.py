#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from sparkvm import Rollouts, SparkVM


def main() -> int:
    dockerfile = (Path(__file__).resolve().parent / "Dockerfile").resolve()

    rollout = Rollouts().create(
        name="network-app-cni-example",
        runtime="Dockerfile",
        dockerfile=str(dockerfile),
        deleteOnSuccess=False,
    )

    vm = SparkVM(
        vcpu=1,
        memory="512M",
        disk="2G",
        timeout=120.0,
        network=True,
        env={},
    )

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
