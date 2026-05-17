#!/usr/bin/env python3
from __future__ import annotations

from sparkvm.rollouts import Rollout


def main() -> int:
    manager = Rollout()
    # rollout = manager.create(
    #     name="testing-rollouts",
    #     runtime="python-3.12",
    #     files={"main.py": "print('hello from static rollout example')"},
    #     command="python3 /job/main.py",
    # )

    # print("Created rollout")
    # print(f"id: {rollout.id}")
    # print(f"name: {rollout.name}")
    # print(f"runtime: {rollout.runtime}")
    # print(f"path: {rollout.path}")
    # print(f"command: {rollout.command}")

    fetched = manager.get_by_id("rollout-loop-run-d9ae58287b1502d6")
    print("\nFetched rollout by id")
    print(f"id: {fetched.id}")
    print(f"name: {fetched.name}")

    # all_rollouts = manager.list()
    # print("\nCurrent rollout ids")
    # for item in all_rollouts:
    #     print(f"- {item.id}")

    # manager.delete_by_id(rollout.id)
    # print("\nDeleted rollout")
    # print(f"id: {rollout.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
