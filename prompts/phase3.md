# SparkVM Phase 3

## 1. What was implemented

Phase 3 now supports rollout-mode execution through a single `Rollouts.create()` API.

Implemented components:

- `sparkvm/rollouts.py`
  - `Rollouts` manager with:
    - `create`
    - `list`
    - `get_by_id`
    - `delete_by_id`
    - `exists`
  - `Rollout` dataclass now stores rollout mode and execution fields:
    - `mode`
    - `command`
    - `setup_cmd`
    - `run_cmd`
    - `disk_mb`
  - Supported modes:
    - `script`
    - `repo`
  - Repo-mode source support:
    - local Git repositories (must contain `.git` directory)
    - Git URLs (`http://`, `https://`, `git@`, `ssh://`, or suffix `.git`)
  - Rollout persistence under `~/.sparkvm/rollouts/<rollout-id>/`.
  - Atomic metadata writes via `metadata.json.tmp` + rename to `metadata.json`.

- `sparkvm/vm.py`
  - `SparkVM.run(rollout: str | Rollout) -> VMResult`.
  - `SparkVM.run()` now uses `rollout.disk_mb` for execution-disk size.
  - Normal setup/run failures return `VMResult` and clean worker directories.
  - Infrastructure failures preserve workers and write `failure.json`.

- `sparkvm/runtimes/python.py`
  - Guest `/init` now runs phased execution:
    - optional `setup.sh`
    - mandatory `run.sh`
  - Writes phase result files under `/job/results`.

- `sparkvm/disk.py`
  - `ExecutionDisk.read_result()` now reads phased result files:
    - setup stdout/stderr/exit
    - run stdout/stderr/exit
    - final exit
  - Backward-compatible fallback to old files:
    - `/output.log`
    - `/error.log`
    - `/exit_code`

- `sparkvm/result.py`
  - Added:
    - `PhaseResult`
    - `VMResult.rollout_mode`
    - `VMResult.status`
    - `VMResult.setup`
    - `VMResult.run`
  - Backward compatibility kept for:
    - `result.stdout` (run phase stdout)
    - `result.stderr` (run phase stderr)
    - `result.exit_code` (final exit code)

- `sparkvm/errors.py`
  - Added rollout-specific errors:
    - `InvalidRepoError`
    - `InvalidRolloutModeError`

## 2. Rollout modes

### `mode="script"`

Use for simple file+command execution.

Required inputs:

- `files`
- `command`

Rollout contents:

- user files
- `run.sh`
- `rollout.json`

`run.sh` runs in `/job`.

Default disk size:

- 1024 MB

### `mode="repo"`

Use for repository execution.

Required inputs:

- `source`
- `run_cmd`

Optional inputs:

- `setup_cmd`
- `ref`

Behavior:

- local repo source must exist and contain `.git` directory
- Git URL source is cloned into `repo/`
- if `ref` is provided for Git URL source, checkout is performed
- commit hash is captured with `git rev-parse HEAD`
- cloned repo `.git` is removed for MVP size reduction
- `setup_cmd` (if provided) runs before `run_cmd`
- both run inside `/job/repo`

Rollout contents:

- `repo/`
- `setup.sh` (only when `setup_cmd` is provided)
- `run.sh`
- `rollout.json`

Default disk size:

- 4096 MB

User-provided `disk_mb` overrides defaults for both modes.

## 3. Why `instructions.md` is not included

`instructions.md` is intentionally not part of the core rollout contract.

Reasons:

- SparkVM should remain agent-agnostic and not enforce an instruction-file convention.
- Users can include any instruction files directly in their repo or script payload.
- Agent installation/execution is intentionally delegated to user commands (`setup_cmd` / `run_cmd`).
- SparkVM focuses on isolated execution, not tool-specific orchestration policy.

## 4. Rollout-first flow

1. User creates rollout via `Rollouts.create(...)` with `mode="script"` or `mode="repo"`.
2. SparkVM persists rollout data under `~/.sparkvm/rollouts/<rollout-id>/` and updates `metadata.json` atomically.
3. User calls `SparkVM.run(rollout_id)` (or passes a `Rollout` object).
4. SparkVM stages the entire rollout directory into an execution ext4 disk mounted at `/job`.
5. Guest `/init` runs:
   - optional setup phase (`/job/setup.sh`)
   - run phase (`/job/run.sh`)
6. Guest writes phased outputs to `/job/results/*` and powers off.
7. Host reads phase logs/exit codes and returns `VMResult`.
8. Worker cleanup policy is applied:
   - cleanup on success and normal command failures
   - preserve workers only for infrastructure failures.

## 5. Status model

`VMResult.status` values:

- `setup_failed`: setup phase exit code non-zero
- `run_failed`: run phase exit code non-zero
- `passed`: final exit code is zero
- `timeout`: host-side timeout
- `oom`: likely OOM condition
- infrastructure failures are surfaced as exceptions and preserved worker artifacts (`failure.json`, `firecracker.log`, `rollout.ext4` when present)

`VMResult.passed` remains:

- `exit_code == 0 and not timed_out and not oom_killed`

## 6. Tests added/updated

Unit tests were added/updated for:

- script rollout creation and metadata persistence
- repo rollout creation from local Git path
- repo rollout invalid local path
- repo rollout local path without `.git`
- repo rollout Git URL with mocked git subprocess calls
- unsafe script path rejection
- phased result parsing (`setup_failed`, `run_failed`, `passed`)
- execution disk staging copies full rollout directory
- `SparkVM.run()` uses `rollout.disk_mb`

Integration tests (guarded by `SPARKVM_RUN_INTEGRATION=1`) cover:

- script mode execution
- repo mode execution from local temporary Git repo

## 7. Acceptance criteria mapping

1. `Rollouts.create(..., mode="script")` works. ✅
2. `Rollouts.create(..., mode="repo", source="./repo")` requires `.git`. ✅
3. Local repo without `.git` raises `InvalidRepoError`. ✅
4. Git URL source clones repo. ✅
5. `setup.sh` is generated only when `setup_cmd` is provided. ✅
6. `run.sh` is always generated. ✅
7. `SparkVM.run()` executes `setup.sh` before `run.sh`. ✅
8. Result includes setup and run phase logs. ✅
9. `result.stdout` still works for run stdout. ✅
10. Normal setup/run command failures return `VMResult`, not infrastructure exception. ✅
11. Workers are preserved only for infrastructure failures. ✅
12. `phase3.md` explains the design and why `instructions.md` is not needed. ✅
