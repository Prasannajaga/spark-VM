#!/usr/bin/env python3
from __future__ import annotations

import json

from sparkvm import RunConfig, SparkVM
from sparkvm.errors import SparkVMError
from sparkvm.rollouts import Rollouts
from sparkvm.workers import Workers

# Replace this with a local git repo path or git URL.
REPO_SOURCE = "/path/to/local/repo"


def main() -> int:
    manager = Rollouts()
    rollout = manager.create(
        name="example-failure-worker",
        source=REPO_SOURCE,
        run_cmd="python3 slow_script.py",
    )

    print("Created rollout")
    print(f"id: {rollout.id}")

    try:
        SparkVM().run(
            rollout.id,
            config=RunConfig(vcpu=1, memory="512M", disk="2G", timeout=3.0),
        )
        print("Unexpected success. This example is meant to fail.")
        return 1
    except SparkVMError as exc:
        print("\nExpected VM failure captured")
        print(f"type: {type(exc).__name__}")
        print(f"message: {exc}")

    workers = Workers()
    items = workers.list()
    if not items:
        print("\nNo preserved worker found.")
        return 1

    latest = items[-1]
    print("\nPreserved worker")
    print(f"vm_id: {latest.vm_id}")
    print(f"path: {latest.path}")
    print(f"log: {latest.firecracker_log_path}")

    if latest.failure_path is None:
        print("failure.json: missing")
        return 1

    failure = workers.failure_json(latest.vm_id)
    print("\nfailure.json")
    print(json.dumps(failure, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
