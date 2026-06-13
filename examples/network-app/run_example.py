#!/usr/bin/env python3
from __future__ import annotations

from sparkvm import Rollouts, SparkVM, VMConfig


VM_CONFIG = VMConfig(
    vcpu=1,
    memory="512M",
    disk="2G",
    timeout=120.0,
    network=True,
    env={
        "NETWORK_APP_URL": "https://api.github.com/repos/python/cpython",
        "NETWORK_APP_TIMEOUT": "30",
    },
)


def main() -> int:
    rollout = Rollouts().create(
        name="network-app-cni-example",
        runtime="Dockerfile",
        dockerfile="examples/network-app/Dockerfile",
        deleteOnSuccess=False,
        vm_config=VM_CONFIG,
    )

    vm = SparkVM(**VM_CONFIG.to_dict())
    result = vm.run(rollout.id)

    print("Created rollout:", rollout.id)
    print("VM status:", result.status)
    print("Exit code:", result.exit_code)
    print("Passed:", result.passed)
    if result.network is not None:
        print("--- network stdout ---")
        print(result.network.stdout.strip())
        print("--- network stderr ---")
        print(result.network.stderr.strip())
    print("--- stdout ---")
    print(result.stdout.strip())
    print("--- stderr ---")
    print(result.stderr.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
