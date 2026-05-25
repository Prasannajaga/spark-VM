#!/usr/bin/env python3
from __future__ import annotations

"""Complex nested SparkVM run using a fixed Dockerfile example."""

from pathlib import Path

from sparkvm import SparkVM
from sparkvm.rollouts import Rollouts


TEMPLATE_DOCKERFILE = (Path(__file__).resolve().parent / "complexrun.Dockerfile").resolve()


def main() -> int:
    rollout = Rollouts().create(
        name="complexrun-example-sjhv",
        runtime="Dockerfile",
        dockerfile=str(TEMPLATE_DOCKERFILE),
        deleteOnSuccess=False,
    )

    vm = SparkVM(
        vcpu=2,
        memory="1G",
        disk="3G",
        timeout=120.0,
        network=False,
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
