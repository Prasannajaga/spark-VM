# SparkVM Phase 3

## 1. What was implemented

Phase 3 adds rollout-first execution and one-shot VM orchestration.

Implemented components:

- `sparkvm/rollouts.py`
  - `Rollout` manager with:
    - `create`
    - `list`
    - `get_by_id`
    - `delete_by_id`
    - `exists`
  - Rollout persistence under `~/.sparkvm/rollouts/<rollout-id>/`.
  - Atomic metadata writes to `~/.sparkvm/rollouts/metadata.json`.
- `sparkvm/vm.py`
  - `SparkVM.run(rollout: str | Rollout) -> VMResult`.
  - Automatic lifecycle orchestration:
    - managed binary/image resolution
    - KVM guard
    - execution disk creation
    - Firecracker boot/config/start
    - timeout handling
    - result collection
    - cleanup.
- `sparkvm/disk.py`
  - ext4 helpers and `ExecutionDisk` class:
    - `create`
    - `mount`
    - `copy_rollout`
    - `unmount`
    - `read_result`
    - `cleanup`.
- `sparkvm/process.py`
  - Firecracker process start/wait/stop implementation.
- `sparkvm/api.py`
  - Unix-socket Firecracker API client (`PUT`/`GET`).
- `sparkvm/result.py`
  - rollout-oriented `VMResult` fields:
    - `rollout_id`
    - `rollout_name`
    - `execution_disk_path`
  - existing `passed` behavior preserved.
- `sparkvm/errors.py`
  - rollout exceptions:
    - `RolloutError`
    - `RolloutNotFoundError`
    - `RolloutMetadataError`
  - `ExecutionDiskError` added under disk failures.
- `sparkvm/__init__.py`
  - exports updated to include:
    - `SparkVM`
    - `Rollout`
    - `VMResult`
    - `SparkVMError`
  - `VMJob` and `VMJobs` retained for compatibility.

## 2. Rollout-first flow

1. User creates a rollout with files + command.
2. SparkVM persists rollout files and metadata under `~/.sparkvm/rollouts`.
3. User calls `SparkVM.run(rollout_id)` (or passes a `Rollout` object).
4. SparkVM creates a temporary execution disk under `~/.sparkvm/work/<vm-id>/rollout.ext4`.
5. Rollout contents are copied into the execution disk.
6. Firecracker runs with:
   - managed rootfs as `/dev/vda`
   - execution disk as `/dev/vdb` mounted to `/job`.
7. Guest runtime writes `/job/output.log`, `/job/error.log`, `/job/exit_code`.
8. Host reads those outputs from the execution disk and returns `VMResult`.
9. Temporary execution disk/workdir is cleaned up on success.

## 3. Why this design

- Rollouts are immutable, reusable execution bundles.
- VM execution remains ephemeral and isolated.
- User-facing API stays high-level and avoids kernel/rootfs/firecracker path plumbing.
- Metadata is persisted safely via atomic replacement to reduce corruption risk.

## 4. Test coverage added

- New integration test:
  - `tests/test_rollouts_integration.py`
  - skipped unless `SPARKVM_RUN_INTEGRATION=1`
  - validates:
    - rollout create
    - rollout execution by id
    - expected stdout + exit code + `result.passed`
    - rollout cleanup by id.
