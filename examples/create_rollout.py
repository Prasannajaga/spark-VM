#!/usr/bin/env python3
from __future__ import annotations

from sparkvm.rollouts import Rollouts

# Replace this with a local git repo path or git URL.
REPO_SOURCE = "/path/to/local/repo"


def main() -> int:
    manager = Rollouts()

    rollout = manager.create(
        name="hello-repo",
        source=REPO_SOURCE,
        run_cmd="python3 main.py",
        delete_on_success=False,
    )

    print("Created rollout")
    print(f"id: {rollout.id}")
    print(f"name: {rollout.name}")
    print(f"mode: {rollout.mode}")
    print(f"path: {rollout.path}")
    print(f"run_cmd: {rollout.run_cmd}")
    print(f"delete_on_success: {rollout.delete_on_success}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
