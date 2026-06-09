#!/usr/bin/env python3
from __future__ import annotations

"""Single-rollout SparkVM example using a fixed Dockerfile."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for extra_path in (
    PROJECT_ROOT / "src",
    PROJECT_ROOT / "external" / "cracker-sdk" / "src",
):
    resolved = str(extra_path)
    if resolved not in sys.path:
        sys.path.insert(0, resolved)

from sparkvm import Rollouts, SparkVM
from crackersdk import CrackerVM


TEMPLATE_DOCKERFILE = (Path(__file__).resolve().parent / "simple_app/simplegithub.Dockerfile").resolve()


def main() -> int:
    rollout = Rollouts().create(
        name="simplegithub-single-rollout",
        runtime="Dockerfile",
        dockerfile="examples/simple_app/simplegithub.Dockerfile",    
        deleteOnSuccess=False,
    )

    vm = SparkVM(
        vcpu=1,
        memory="512M",
        disk="2G",
        timeout=60.0,
        network=False,
        env={},
    )

    result = vm.run(rollout.id)

    print("Single rollout created:", rollout.id)
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
