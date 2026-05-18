#!/usr/bin/env python3
from __future__ import annotations

from sparkvm.rollouts import Rollouts


def main() -> int:
    manager = Rollouts()

    rollout = manager.create(
        name="hello-script-",
        mode="script",
        files={"main.py": "print('hello from script rollout')\n"},
        run_cmd="python3 /job/main.py",
    )

    print("Created rollout")
    print(f"id: {rollout.id}")
    print(f"name: {rollout.name}")
    print(f"mode: {rollout.mode}")
    print(f"base_image: {rollout.base_image}")
    print(f"path: {rollout.path}")
    print(f"run_cmd: {rollout.run_cmd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
