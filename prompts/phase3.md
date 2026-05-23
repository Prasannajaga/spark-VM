# SparkVM Phase 3

## 1. Rollouts are repo-only

`Rollouts.create(...)` is intentionally repo-only.

Supported inputs:

- `name` (required)
- `source` (required): local git repo path or git URL
- `run_cmd` (required)
- `setup_cmd` (optional)
- `ref` (optional)
- `delete_on_success` (optional, default `False`)

Unsupported:

- `mode`
- `files`
- `command`
- `disk_mb`
- script-mode rollouts

## 2. Direct script execution is intentionally not supported

SparkVM does not support direct script execution APIs in this phase.
All execution must go through persisted rollout IDs.

## 3. Execution resources are runtime-time only

CPU/RAM/DISK/network are execution-time concerns.
They are passed through `RunConfig` at `SparkVM.run(...)` time, not stored as rollout ownership.
